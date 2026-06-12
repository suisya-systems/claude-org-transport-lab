# -*- coding: utf-8 -*-
"""AC-9: WezTerm backend 実機 AC (Windows) — backend のみ (renga 不使用) の組織運用 1 サイクル完走。

位置付け (Issue #9): Phase 4 (run_ac4.py) は「該当 backend 実機で 1 サイクル完走」を
Linux/WSL2 環境では正準 backend の **tmux** に読み替えて完走済みだが、WezTerm (Windows 専用)
側の実機担保が未取得だった。本スクリプトはその WezTerm 側を埋める:
**実 WezTerm の pane に対して** broker のペイン操作 6 面 + ライフサイクル + イベント合成 +
画面状態観測 + メッセージング 1 サイクルを往復実証する。

検証方式:
- FakeAdapter ではなく **実 WezTermAdapter** を使い、実ウィンドウ/ペインを spawn/split/kill する。
- spawn される全プロセスは **無課金 probe** (wezterm_probe.py。実 Claude を起動しない)。
  課金中立スコープは窓口/ユーザー判断 (2026-06-13) で probe-only AC を承認 (実 Claude TUI は
  AC-2 Phase 1 で既証明 + #515 本番サイクルに委譲)。spawn する実 argv は attestation として記録する。
- 画面状態 (idle / 承認待ち=input_pending / stall=busy) は probe が実描画し、broker.inspect_pane →
  classify_pane_state が **実 WezTerm get-text** に対して判定する (Fake の合成画面ではない)。

前提となった修正 (lab#9 で実機発見した defect の A 案修正):
  実 WezTerm `cli list` は geometry を size:{cols,rows}/left_col/top_row/is_active で返すため、
  WezTermAdapter.list_panes() を tmux と対称に flat 正規化するまで broker.mcp_list_panes() が
  KeyError('width') で落ち、spawn_agent / pane-ops 面が WezTerm 実機で全滅していた。
  修正前の再現ログ: broker-state/ac9/defect-geometry.json。

検証項目 (GO/NO-GO は broker-state/ac9/result.json + RESULTS.md に転記):
  AC-9-geometry : 正規化後 mcp_list_panes() が実 WezTerm で例外なく geometry を返す (defect 修正の通過証跡)。
  AC-9-surface  : 実 2 pane の geometry + resolve_balanced_split(choose_split 再利用) + worker tier 遮断。
  AC-9-cycle    : delegate→spawn(実 split)→監視(inspect で承認待ち/stall を実 get-text 観測)→
                  完了報告(token 由来 from)→CLOSE_PANE(pane_exited + token revoke)→retro の 1 サイクル完走。
  AC-9-events   : baseline / spawn=pane_started / close=pane_exited / 直 kill 取りこぼしが reconcile で回復。
  AC-9-roundtrip: send_keys の literal 文字が inspect で無傷に往復する (実 ConPTY)。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from broker import Broker  # noqa: E402
from wezterm_adapter import WezTermAdapter  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

OUT = Path(__file__).parent / "broker-state" / "ac9"
SCREENS = OUT / "screens"
PROBE = str((Path(__file__).parent / "wezterm_probe.py").resolve())
SPIKE_DIR = str(Path(__file__).parent.resolve())
# 無課金 probe の実 argv (attestation 用に固定し、spawn/split/spawn_agent 全てで使う)。
PROBE_ARGV = ["py", "-3", PROBE]

# 実機タイミング: 実 WezTerm の pane spawn + py 起動 + grid 反映の待ち。
STARTUP = 2.0   # spawn/split 後、probe が初期描画するまで
SETTLE = 0.8    # send_keys 後、probe 再描画 + get-text 反映まで


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class WezSession:
    """実 WezTerm 上の broker + WezTermAdapter + 役割 token の結線 (1 ウィンドウ)。"""

    def __init__(self) -> None:
        self.adapter = WezTermAdapter()
        self.broker = Broker(
            state_dir=OUT / "state", adapter=self.adapter,
            nudge_defer_interval=0.05, nudge_defer_max_tries=40,
            event_poll_interval=0.05,
        )
        self.tokens: dict[str, str] = {}
        self.panes: dict[str, object] = {}     # role/agent_id -> PaneRef
        self.attest: list[dict] = []           # 実 argv attestation

    # -- helpers ----------------------------------------------------------
    def bind(self, agent_id: str):
        b = self.broker.get_bind(self.tokens[agent_id])
        assert b is not None, f"no active bind for {agent_id}"
        return b

    def handle_of(self, name: str):
        for rec in self.broker.mcp_list_panes():
            if rec["name"] == name:
                return rec["id"]
        return None

    def snapshot(self, label: str, pane_id) -> str:
        txt = self.adapter.get_text(pane_id)
        SCREENS.mkdir(parents=True, exist_ok=True)
        (SCREENS / f"{label}.txt").write_text(txt, encoding="utf-8")
        return txt

    # -- setup ------------------------------------------------------------
    def setup_orchestrators(self) -> None:
        """secretary + dispatcher を実 WezTerm pane (probe) として立てる。

        spawn_agent は worker/curator のみ spawn 可 (SPAWNABLE_ROLES) のため、
        orchestrator 2 役は adapter.spawn(new_window) + adapter.split で直接立て、
        role token を bind する。これで実 2 pane が 1 ウィンドウに並ぶ。
        """
        sec = self.adapter.spawn(PROBE_ARGV, cwd=SPIKE_DIR, new_window=True)
        self.panes["secretary"] = sec
        self.tokens["secretary"] = self.broker.issue_token(
            "secretary", "secretary", "secretary", pane_id=sec.pane_id)
        self.broker.register_local(self.tokens["secretary"])
        self.attest.append({"role": "secretary", "argv": PROBE_ARGV,
                            "pane_id": sec.pane_id, "window_id": sec.window_id,
                            "method": "adapter.spawn(new_window=True)"})

        disp = self.adapter.split(sec.pane_id, PROBE_ARGV, cwd=SPIKE_DIR)
        self.panes["dispatcher"] = disp
        self.tokens["dispatcher"] = self.broker.issue_token(
            "dispatcher", "dispatcher", "dispatcher", pane_id=disp.pane_id)
        self.broker.register_local(self.tokens["dispatcher"])
        self.attest.append({"role": "dispatcher", "argv": PROBE_ARGV,
                            "pane_id": disp.pane_id, "window_id": disp.window_id,
                            "method": "adapter.split(secretary)"})
        time.sleep(STARTUP)
        log(f"orchestrators up: secretary pane={sec.pane_id} dispatcher pane={disp.pane_id}")

    def teardown(self) -> None:
        for ref in self.panes.values():
            try:
                self.adapter.kill_pane(ref.pane_id)
            except Exception:
                pass
        self.broker.stop()


# ---------------------------------------------------------------------------
# 検証本体
# ---------------------------------------------------------------------------


def check_geometry(s: WezSession) -> tuple[bool, str]:
    """AC-9-geometry: 正規化後 mcp_list_panes() が実 WezTerm で geometry を返す (defect 修正証跡)。"""
    f = []
    try:
        recs = s.broker.mcp_list_panes()
    except Exception as e:
        return False, f"mcp_list_panes が実 WezTerm で例外 (defect 未修正?): {e!r}"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mcp_list_panes-after-fix.json").write_text(
        json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    names = {r["name"] for r in recs}
    if not ({"secretary", "dispatcher"} <= names):
        f.append(f"secretary/dispatcher が name 付きで現れない: {names}")
    for r in recs:
        if r["name"] in ("secretary", "dispatcher"):
            if not all(k in r and isinstance(r[k], int)
                       for k in ("x", "y", "width", "height")):
                f.append(f"geometry 欠落/型不正: {r}")
            if r["width"] <= 0 or r["height"] <= 0:
                f.append(f"geometry が非正: {r}")
    go = not f
    detail = ("正規化後 mcp_list_panes() が実 WezTerm cli list (size:{cols,rows}/left_col/"
              "top_row/is_active) を flat な x/y/width/height/focused へ写し、例外なく "
              f"{len(recs)} pane の geometry を返す (修正前は KeyError('width'))"
              if go else "; ".join(f))
    return go, detail


def check_surface(s: WezSession) -> tuple[bool, str]:
    """AC-9-surface: 実 geometry + balanced split 解決 + worker tier の構造的遮断。"""
    f = []
    recs = s.broker.mcp_list_panes()
    choice = s.broker.resolve_balanced_split(recs)
    if choice is None:
        f.append("実 geometry で balanced split 候補が解決できない (choose_split None)")
    else:
        log(f"resolve_balanced_split -> target={choice.target_id} dir={choice.direction}")

    # worker token は pane 操作面が tools/list に出ず call_tool も [tool_forbidden]
    wtok = s.broker.issue_token("probe-worker", "probe-worker", "worker")
    s.broker.register_local(wtok)
    wbind = s.broker.get_bind(wtok)
    for tool in ("list_panes", "inspect_pane", "send_keys", "poll_events",
                 "close_pane", "spawn_agent"):
        r = s.broker.call_tool(wbind, tool, {"target": 1})
        if not (r.get("isError") and "[tool_forbidden]" in r["content"][0]["text"]):
            f.append(f"worker が {tool} を呼べた (権限分離破れ): {r}")
    s.broker.revoke_token(wtok, reason="surface-check-done")

    go = not f
    detail = ("実 2 pane の geometry で resolve_balanced_split が choose_split 再利用で "
              "split 対象/方向を解決。worker token は pane 操作 6 面を call_tool しても "
              "[tool_forbidden] で構造的に弾かれる"
              if go else "; ".join(f))
    return go, detail


def check_cycle(s: WezSession) -> tuple[bool, str]:
    """AC-9-cycle: delegate→spawn→監視→完了報告→CLOSE_PANE→retro の 1 サイクル完走 (実 WezTerm)。"""
    f = []
    base = s.broker.poll_events(since=None, timeout_ms=0)
    cursor = base["next_since"]

    # (1) delegate: secretary → dispatcher (queue + ナッジ配達 trigger)
    r = s.broker.enqueue(s.bind("secretary"), "dispatcher",
                         "DELEGATE: lab9-wezterm-backend-ac を派遣してください")
    if not r.get("ok"):
        f.append(f"delegate 送信失敗: {r}")
    got_disp = s.broker.drain(s.bind("dispatcher"))
    if not (len(got_disp) == 1 and got_disp[0]["from_id"] == "secretary"):
        f.append(f"delegate が token 由来 from で dispatcher に届かない: {got_disp}")

    # (2) spawn: dispatcher が balanced split で worker pane を実 WezTerm に spawn
    # balanced split を **必須** にする: 本 AC の主眼は実 WezTerm で壊れていた
    # resolve_balanced_split → adapter.split の経路そのものの担保。明示 target への
    # fallback は balanced 経路の regression を隠して GO にし得るため使わない
    # (codex Major 対応。AC-9-surface で choose_split の解決自体は別途実証済み)。
    sp = s.broker.spawn_agent("worker-ac9", "worker-ac9", "worker", PROBE_ARGV,
                              cwd=SPIKE_DIR, inject_mcp_config=False)
    if not sp.get("ok"):
        return False, f"balanced split spawn_agent 失敗 (本 AC は balanced 必須・fallback 不使用): {sp}"
    worker_handle = sp["handle"]
    worker_pane = sp["pane_id"]
    s.broker.register_local(sp["token"])
    s.tokens["worker-ac9"] = sp["token"]
    s.attest.append({"role": "worker", "agent_id": "worker-ac9", "argv": PROBE_ARGV,
                    "pane_id": worker_pane, "handle": worker_handle,
                    "method": "broker.spawn_agent(balanced split, inject_mcp_config=False)"})
    time.sleep(STARTUP)
    # 実 argv attestation: 起動した pane の get-text に probe バナーが出ること
    banner = s.snapshot("worker-spawned", worker_pane)
    if "[lab9-probe]" not in banner:
        f.append("spawn した worker pane に probe バナーが出ない (argv 実行未確認)")

    # spawn が pane_started として観測される
    ev = s.broker.poll_events(since=cursor, timeout_ms=0)
    cursor = ev["next_since"]
    if not any(e["type"] == "pane_started" and e["name"] == "worker-ac9"
               for e in ev["events"]):
        f.append(f"spawn の pane_started 未観測: {ev['events']}")

    # (3) 監視: dispatcher が inspect_pane で worker 画面を独立観測 (実 get-text → classify)
    #   (a) 承認待ち観測: worker を input_pending にし、自己申告に依らず実描画 scrape で検知
    s.broker.send_keys_op(worker_handle, text="input", enter=True)
    time.sleep(SETTLE)
    obs = s.broker.inspect_pane(worker_handle)
    s.snapshot("worker-input_pending", worker_pane)
    if obs.get("state") != "input_pending":
        f.append(f"承認待ち (input_pending) を実 get-text で観測できない: state={obs.get('state')}")
    #   (b) stall 検出: busy が連続観測される (応答生成が長期化 = stall 候補)
    s.broker.send_keys_op(worker_handle, text="busy", enter=True)
    time.sleep(SETTLE)
    busy_obs = [s.broker.inspect_pane(worker_handle)["state"] for _ in range(3)]
    s.snapshot("worker-busy", worker_pane)
    if busy_obs != ["busy", "busy", "busy"]:
        f.append(f"stall (連続 busy) を実 get-text で観測できない: {busy_obs}")
    s.broker.send_keys_op(worker_handle, text="idle", enter=True)
    time.sleep(SETTLE)

    # (3c) send_keys literal 往復 (実 ConPTY): 文字が無傷で inspect に出る
    s.broker.send_keys_op(worker_handle, text="hello-lab9-smoke", enter=True)
    time.sleep(SETTLE)
    rt = s.broker.inspect_pane(worker_handle)
    s.snapshot("worker-roundtrip", worker_pane)
    if "hello-lab9-smoke" not in rt.get("text", ""):
        f.append("send_keys literal が inspect 往復で無傷に出ない")
    s.broker.send_keys_op(worker_handle, text="idle", enter=True)
    time.sleep(SETTLE)

    # (4) 完了報告: worker → secretary (token 由来 from で到達)
    rep = s.broker.enqueue(s.bind("worker-ac9"), "secretary",
                           "完了報告: WezTerm backend 実機 AC 一式 commit 済み")
    if not rep.get("ok"):
        f.append(f"完了報告送信失敗: {rep}")
    got = s.broker.drain(s.bind("secretary"))
    if not (len(got) == 1 and got[0]["from_id"] == "worker-ac9"):
        f.append(f"完了報告が token 由来 from で届かない: {got}")

    # (5) CLOSE_PANE: dispatcher が worker を close → token revoke + pane_exited
    wtok = sp["token"]
    closed = s.broker.close_pane_target(worker_handle)
    if not (closed.get("ok") and "worker-ac9" in closed.get("closed", [])):
        f.append(f"close_pane が revoke を誘発しない: {closed}")
    if s.broker.authorize(wtok)[1] != "token_revoked":
        f.append("close 後に token_revoked にならない")
    ev2 = s.broker.poll_events(since=cursor, timeout_ms=0)
    cursor = ev2["next_since"]
    if not any(e["type"] == "pane_exited" and e["name"] == "worker-ac9"
               for e in ev2["events"]):
        f.append(f"CLOSE_PANE の pane_exited 未観測: {ev2['events']}")

    # (6) retro gate: dispatcher → secretary
    rg = s.broker.enqueue(s.bind("dispatcher"), "secretary",
                          "retro gate: worker クローズ条件を満たしました。retro 起動可否?")
    if not rg.get("ok"):
        f.append(f"retro gate 送信失敗: {rg}")

    go = not f
    detail = ("delegate→spawn(実 WezTerm balanced split)→監視(inspect_pane で承認待ち/stall を "
              "実 get-text→classify で独立観測)→完了報告(token 由来 from)→"
              "CLOSE_PANE(token revoke + pane_exited)→retro gate の 1 サイクルが renga 不使用で完走"
              if go else "; ".join(f))
    return go, detail


def check_events_crash(s: WezSession) -> tuple[bool, str]:
    """AC-9-events: broker 非経由の直 kill (クラッシュ) が次の reconcile で pane_exited 回復 + reap revoke。

    注意: dispatcher pane を kill するため最後に実行する (retro 送信後)。
    """
    f = []
    base = s.broker.poll_events(since=None, timeout_ms=0)
    cursor = base["next_since"]
    disp_ref = s.panes["dispatcher"]
    # broker を経由せず直接 kill (イベント取りこぼしを模す)
    s.adapter.kill_pane(disp_ref.pane_id)
    time.sleep(STARTUP)
    ev = s.broker.poll_events(since=cursor, timeout_ms=0)
    if not any(e["type"] == "pane_exited" and e["name"] == "dispatcher"
               for e in ev["events"]):
        f.append(f"直 kill (クラッシュ) の pane_exited が reconcile で回復しない: {ev['events']}")
    reaped = s.broker.reap_exited_panes()
    if ("dispatcher" not in reaped
            and s.broker.authorize(s.tokens["dispatcher"])[1] != "token_revoked"):
        f.append("クラッシュ pane の token が revoke されない")
    # dispatcher は kill 済みなので teardown の二重 kill を避ける
    s.panes.pop("dispatcher", None)
    go = not f
    detail = ("broker 非経由の直 kill (クラッシュ) が次 poll の list_panes reconcile で "
              "pane_exited として回復し、reap で token も revoke (監視ループの正しさを損なわない)"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# tmux 実機との差分記録 (Issue #9 goal 3。事実のみ)
# ---------------------------------------------------------------------------

TMUX_WEZTERM_DIFF = {
    "geometry_keys": {
        "tmux": "list-panes -F が flat な left/top/width/height/active を直接出す (adapter で int 化)",
        "wezterm": "cli list が size:{cols,rows} (ネスト) + left_col/top_row + is_active を出す。"
                   "broker は flat を期待するため adapter.list_panes() で正規化が必須 (lab#9 defect)",
    },
    "pane_id_type": {
        "tmux": "文字列 '%N' (例 '%3')。専用 socket -L claude-org-spike で既存サーバーと分離",
        "wezterm": "整数 (例 2)。native int と broker handle int の取り違え回避のため MCP 面は handle のみ露出",
    },
    "pane_lifecycle": {
        "tmux": "spawn=new-session -d (detached、GUI 不要)。kill-pane / kill-server。CI/WSL2 で無頭運用可",
        "wezterm": "spawn=cli spawn --new-window (GUI mux 必須、実ウィンドウが出る)。split-pane / kill-pane。"
                   "mux 未起動時は cli が auto-start (--no-auto-start だと接続不可)",
    },
    "events": {
        "tmux": "list-panes 差分から pane_started/pane_exited を合成 (backend 非依存の broker._reconcile)",
        "wezterm": "同一経路。cli list の差分から同じく合成。直 kill 取りこぼしも list 反映で回復 (同型)",
    },
    "focus_model": {
        "tmux": "pane_active は session 内で単一 (active pane は 1 つ)",
        "wezterm": "is_active は tab/window ごと。複数ウィンドウ/タブ構成では複数 pane が同時に "
                   "is_active:true になる (初回 cli list で window0/window1 が両方 true を実測)。"
                   "is_active→focused 正規化のためマルチウィンドウ org 配置で focused が複数 true になり、"
                   "tmux 単一 active / renga 単一フォーカス前提と乖離。本 AC は 1 ウィンドウのため非顕在 (GO)。"
                   "runtime 取り込み時に choose_split の focused 依存を要確認 (記録のみ・本 AC では自己修正せず)",
    },
    "get_text_screen": {
        "tmux": "capture-pane -p は描画済み行のみ返す傾向",
        "wezterm": "cli get-text は viewport 全高 (空行込み) を返すため、busy ヒントを下部へ寄せないと "
                   "classify の tail-20 走査から外れる (lab#9 probe 較正で確認。実 Claude は元々下部描画)",
    },
    "key_input": {
        "tmux": "send-keys が一級 (Enter/C-c/-l literal)。素直",
        "wezterm": "send-text の小細工 (--no-paste + CR で Enter、paste で未送信置き)。adapter が吸収",
    },
}


# ---------------------------------------------------------------------------
# ランナー
# ---------------------------------------------------------------------------

CHECKS = [
    ("AC-9-geometry", check_geometry),
    ("AC-9-surface", check_surface),
    ("AC-9-cycle", check_cycle),
    ("AC-9-events", check_events_crash),  # dispatcher を kill するため最後
]


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    SCREENS.mkdir(parents=True, exist_ok=True)
    qpath = OUT / "state" / "queue.jsonl"
    if qpath.exists():
        qpath.unlink()
    s = WezSession()
    results: dict[str, dict] = {}
    try:
        s.setup_orchestrators()
        # AC-9-attestation: orchestrator 各 pane の get-text に probe バナーが出ること。
        # 実 argv (py -3 wezterm_probe.py) が実プロセスとして起動したことの裏取りで、
        # 課金中立 attestation の核。WARN ではなく **AC 判定に反映** する (codex Major 対応:
        # py 起動失敗や別プロセス化を GO で見逃さない)。worker は AC-9-cycle で別途確認。
        att_fail = [role for role in ("secretary", "dispatcher")
                    if "[lab9-probe]" not in s.snapshot(f"{role}-banner", s.panes[role].pane_id)]
        att_go = not att_fail
        results["AC-9-attestation"] = {
            "go": att_go,
            "detail": ("secretary/dispatcher pane の get-text に probe バナー [lab9-probe] を確認 "
                       "(実 argv=py -3 wezterm_probe.py の実プロセス起動を裏取り)"
                       if att_go else
                       f"probe バナー未確認の pane: {att_fail} (実 argv 起動未確認 = attestation 不成立)"),
        }
        log(f"{'GO   ' if att_go else 'NO-GO'} AC-9-attestation: {results['AC-9-attestation']['detail']}")
        for name, fn in CHECKS:
            try:
                go, detail = fn(s)
            except Exception as e:
                go, detail = False, f"例外: {e!r}"
            results[name] = {"go": go, "detail": detail}
            log(f"{'GO   ' if go else 'NO-GO'} {name}: {detail}")
    finally:
        s.teardown()
    # 成果物: result.json (機械可読) + attestation + tmux 差分
    go_all = bool(results) and all(r["go"] for r in results.values())
    (OUT / "result.json").write_text(
        json.dumps({
            "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backend": "WezTermAdapter (実機)",
            "wezterm_version": s.adapter.exe,
            "method": "実 WezTerm pane (無課金 probe) + broker 直結。FakeAdapter 不使用",
            "charging_neutral": "spawn 全プロセスは wezterm_probe.py (実 Claude 不起動)。実 argv は attestation に記録",
            "results": results,
            "argv_attestation": s.attest,
            "tmux_wezterm_diff": TMUX_WEZTERM_DIFF,
            "go": go_all,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"results": results, "go": go_all}


def main() -> int:
    out = run()
    print()
    print("AC-9 WezTerm backend 実機 AC: " + ("GO (全項目 green)" if out["go"] else "NO-GO"))
    return 0 if out["go"] else 1


if __name__ == "__main__":
    sys.exit(main())
