# -*- coding: utf-8 -*-
"""AC-5 完動ゲート dogfood: backend(tmux)のみ・renga 不使用で 委譲サイクルを**複数回**完走 +
障害系4種 (stall検出 / escalation / handover / resume) の broker 成立 + 課金中立 (対話 TUI argv) を
1 本の harness で実証する (Issue #5 / Epic #6 完動ゲート)。

設計ノート: spike/ac5-design-note.md (codex design review 1 周反映済み)。
方式は Phase 3/4 と同じ **方式 B** (FakeAdapter / 無課金・決定的・CI 可) を主とし、実 tmux smoke +
実 claude idle attestation (課金中立の実測) は `--real-tmux` 手動ランナーで行う (sandbox 無効が必要)。

Phase 1-4 既証分は焼き直さない。AC-5 が新規に足すのは:
  AC-5-multi:      単一 broker / adapter 上で 3 サイクル連続完走 + cross-cycle isolation
                   (inbox / token / handle / nudge dedup / event cursor がサイクル間で漏れない)。
                   native id 再利用を強制し、旧 handle が新 pane に誤対応しないことを assert。
  AC-5-stall:      連続 busy を dispatcher が独立観測 → stall 判定 → **broker 成立物 = secretary への
                   escalation enqueue** (観測だけでなく観測後の成立物まで)。idle/input_pending は誤検出なし。
  AC-5-escalation: 判断仰ぎが secretary busy 中 defer → idle 後配達 (token 由来 from) → 人間返答を
                   worker へ broker 経由で転送 → worker 側 at-most-once drain、の一連の障害系を固定。
  AC-5-handover:   secretary が ops tier inspect_pane + send_keys で dispatcher を引き継ぐ。ペインを
                   閉じない (pane_exited 非 emit) ため、handover 中の worker lifecycle イベントを
                   handover 前 cursor から取りこぼさない (監視 cursor 不喪失)。
  AC-5-resume:     suspend = 全 token revoke + 未読 queue 破棄 (既存方針) → resume = token 再発行
                   (別 token) → 新 lifecycle で送受信成立 + 旧 lifecycle 未読の非継承 (stale 非継承)。
  AC-5-billing:    spawn_agent の argv builder が対話 TUI (claude + --mcp-config のみ) で、
                   -p/--print/--headless/--output-format/Agent-SDK 系を含まない (ヘッドレス落ちの構造排除)。
                   worker/curator の各 spawnable role で禁止 flag 非混入を確認。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from broker import Broker, NUDGE_TEXT, SPAWNABLE_ROLES  # noqa: E402
from terminal_adapter import PaneRef  # noqa: E402
from run_ac4 import FakeAdapter, log  # noqa: E402 (Phase 1/2 較正済み FakeAdapter を再利用)

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

OUT = Path(__file__).parent / "broker-state" / "ac5"

# 対話 TUI に落ちていてはならない (ヘッドレス) flag 群。argv 構造 assert の禁止集合。
HEADLESS_FLAGS = ("-p", "--print", "--headless", "--output-format", "--input-format")


# ---------------------------------------------------------------------------
# ReuseFakeAdapter: native id 再利用を強制し、launch argv を記録する
# ---------------------------------------------------------------------------


class ReuseFakeAdapter(FakeAdapter):
    """kill された native id を次の split で**再利用**する FakeAdapter。

    既定 FakeAdapter は native id を単調採番するため stale handle が新 pane に誤対応する
    経路を踏めない。本実装は freed native を再利用し「旧 handle が新 lifecycle の pane に
    再対応しない (broker が exit 時に handle 対応を掃除する)」ことを cross-cycle で実証する。
    併せて split に渡る launch argv を記録し、AC-5-billing の構造 assert に供する。
    """

    def __init__(self, geom_keys: str = "xy") -> None:
        super().__init__(geom_keys=geom_keys)
        self._free: list[str] = []
        self.split_argv: list[list[str]] = []

    def kill_pane(self, pane_id: str) -> None:
        super().kill_pane(pane_id)
        if pane_id not in self._free:
            self._free.append(pane_id)

    def split(self, target, argv, cwd=None, direction="vertical") -> PaneRef:
        self.split_argv.append(list(argv))
        if not self._free:
            return super().split(target, argv, cwd=cwd, direction=direction)
        t = self._panes[target]
        if not t["alive"]:
            raise RuntimeError(f"split target {target} not alive")
        nid = self._free.pop(0)  # 直近 kill された native を再利用 (stale handle を踏む)
        if direction == "vertical":
            half = t["width"] // 2
            self.add_pane(nid, x=t["x"] + (t["width"] - half), y=t["y"],
                          width=half, height=t["height"])
            t["width"] -= half
        else:
            half = t["height"] // 2
            self.add_pane(nid, x=t["x"], y=t["y"] + (t["height"] - half),
                          width=t["width"], height=half)
            t["height"] -= half
        return PaneRef(pane_id=nid)


# ---------------------------------------------------------------------------
# Cycle: broker + adapter + 役割 token 結線 (run_ac4.Cycle 同型・ac5 OUT)
# ---------------------------------------------------------------------------


_CYCLE_SEQ = [0]  # Cycle ごとに一意な state_dir を割り当てる (test 間の journal 衝突を断つ)


class Cycle:
    def __init__(self, adapter=None) -> None:
        self.adapter = adapter if adapter is not None else ReuseFakeAdapter()
        _CYCLE_SEQ[0] += 1
        # 各 Cycle に専用 state_dir を与え、queue.jsonl を test/check 間で完全分離する
        # (共有 dir だと先行 test の lingering nudge thread が journal を汚し escalation 系が
        #  flaky になりうる。codex Minor 対応)。
        self.state_dir = OUT / f"state-{_CYCLE_SEQ[0]}"
        self.broker = Broker(
            state_dir=self.state_dir, adapter=self.adapter,
            nudge_defer_interval=0.01, nudge_defer_max_tries=200,
            event_poll_interval=0.01,
        )
        self.tokens: dict[str, str] = {}

    def add_role_pane(self, agent_id, role, x, y, w, h, name=None) -> None:
        name = name or agent_id
        self.adapter.add_pane(agent_id, x=x, y=y, width=w, height=h)
        tok = self.broker.issue_token(agent_id, name, role, pane_id=agent_id)
        self.broker.register_local(tok)
        self.tokens[agent_id] = tok

    def bind(self, agent_id):
        b = self.broker.get_bind(self.tokens[agent_id])
        assert b is not None, f"no active bind for {agent_id}"
        return b

    def handle_of(self, name) -> int | None:
        for rec in self.broker.mcp_list_panes():
            if rec["name"] == name:
                return rec["id"]
        return None

    def journal(self) -> list[dict]:
        path = self.state_dir / "queue.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def wait_nudge(self, agent_id, baseline: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.adapter.nudges_for(agent_id)) > baseline:
                return True
            time.sleep(0.01)
        return False

    def wait_journal(self, event: str, agent_id: str, n0: int, timeout: float = 5.0) -> bool:
        """event/agent_id に一致する journal 行が n0 件より増えるまで待つ。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            n = sum(1 for e in self.journal()
                    if e.get("event") == event and e.get("agent_id") == agent_id)
            if n > n0:
                return True
            time.sleep(0.01)
        return False

    def teardown(self):
        self.broker.stop()


# ---------------------------------------------------------------------------
# AC-5-multi: 3 サイクル連続完走 + cross-cycle isolation
# ---------------------------------------------------------------------------


def check_multi(c: Cycle) -> tuple[bool, str]:
    f = []
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    base = c.broker.poll_events(since=None, timeout_ms=0)
    cursor = base["next_since"]
    prev_seq = int(cursor)
    prev_worker_handle = None
    prev_native = None

    N = 3
    for k in range(1, N + 1):
        wid = f"worker-{k}"
        # (1) delegate: secretary → dispatcher
        c.broker.enqueue(c.bind("secretary"), "dispatcher",
                         f"DELEGATE cycle{k}: {wid} を派遣してください")
        deleg = c.broker.drain(c.bind("dispatcher"))
        if not (len(deleg) == 1 and deleg[0]["from_id"] == "secretary"):
            f.append(f"cycle{k}: delegate が dispatcher に 1 通届かない: {deleg}")

        # (2) spawn: dispatcher が balanced split で worker を spawn
        sp = c.broker.spawn_agent(wid, wid, "worker", ["claude"])
        if not sp.get("ok"):
            f.append(f"cycle{k}: spawn 失敗: {sp}")
            break
        c.broker.register_local(sp["token"])
        c.tokens[wid] = sp["token"]
        wh = sp["handle"]
        native = sp["pane_id"]
        # native id 再利用の強制が効いているか (2 サイクル目以降は同一 native を期待)
        if k >= 2 and prev_native is not None and native != prev_native:
            f.append(f"cycle{k}: native 再利用が起きていない (isolation 試験が無効): "
                     f"{native} != {prev_native}")
        # handle はサイクル毎に必ず新しい (native 再利用でも handle は別採番)
        if prev_worker_handle is not None and wh == prev_worker_handle:
            f.append(f"cycle{k}: handle が前サイクルと重複 (cross-cycle 漏れ): {wh}")
        # **native 再利用後**も前サイクルの旧 handle は新 pane に誤対応しない (codex Major 対応)。
        # 同一 native を新 pane が掴んだ今この瞬間に、旧 handle が解決しない/新 pane を指さないことを assる。
        if k >= 2 and prev_worker_handle is not None:
            old_insp = c.broker.inspect_pane(prev_worker_handle)
            if "[pane_not_found]" not in old_insp.get("error", ""):
                f.append(f"cycle{k}: native 再利用後に前サイクル旧 handle が新 pane に誤対応: {old_insp}")
            if c.handle_of(wid) == prev_worker_handle:
                f.append(f"cycle{k}: 新 worker の handle が旧 handle を再利用している (誤対応)")

        # pane_started 観測 (このサイクルの cursor 起点で worker のみ)
        ev = c.broker.poll_events(since=cursor, timeout_ms=0)
        cursor = ev["next_since"]
        started = [e for e in ev["events"]
                   if e["type"] == "pane_started" and e["name"] == wid]
        if not started:
            f.append(f"cycle{k}: spawn の pane_started 未観測: {ev}")
        if int(cursor) < prev_seq:
            f.append(f"cycle{k}: event cursor が後退 (単調前進破れ): {cursor} < {prev_seq}")
        prev_seq = int(cursor)

        # (3) 初期指示: dispatcher → worker (worker inbox 分離: 自分宛のみ受ける)
        c.broker.enqueue(c.bind("dispatcher"), wid, f"brief cycle{k}: スコープ内で作業せよ")
        wbrief = c.broker.drain(c.bind(wid))
        if not (len(wbrief) == 1 and wbrief[0]["from_id"] == "dispatcher"):
            f.append(f"cycle{k}: worker 初期指示が 1 通届かない (inbox 漏れ): {wbrief}")
        if c.broker.drain(c.bind(wid)):
            f.append(f"cycle{k}: worker inbox に残留 (at-most-once 破れ)")

        # (4) 監視: dispatcher が inspect_pane で独立観測 (承認待ち / stall)
        c.adapter.set_state(native, "input_pending")
        if c.broker.inspect_pane(wh).get("state") != "input_pending":
            f.append(f"cycle{k}: 承認待ち (input_pending) を独立観測できない")
        c.adapter.set_state(native, "busy")
        if [c.broker.inspect_pane(wh)["state"] for _ in range(3)] != ["busy"] * 3:
            f.append(f"cycle{k}: stall (連続 busy) を観測できない")

        # (5) 完了報告: worker → secretary (idle 静止後)
        c.adapter.set_state(native, "idle")
        c.broker.enqueue(c.bind(wid), "secretary", f"完了報告 cycle{k}: commit 済み")
        rep = c.broker.drain(c.bind("secretary"))
        if not (len(rep) == 1 and rep[0]["from_id"] == wid):
            f.append(f"cycle{k}: 完了報告が token 由来 from で 1 通届かない: {rep}")

        # (6) CLOSE_PANE: dispatcher が worker を close → token revoke + pane_exited
        closed = c.broker.close_pane_target(wh)
        if not (closed.get("ok") and wid in closed.get("closed", [])):
            f.append(f"cycle{k}: close_pane が revoke を誘発しない: {closed}")
        if c.broker.authorize(sp["token"])[1] != "token_revoked":
            f.append(f"cycle{k}: close 後に token_revoked にならない")
        # pane_exited 観測 (この reconcile で旧 handle 対応が掃除される)
        ev2 = c.broker.poll_events(since=cursor, timeout_ms=0)
        cursor = ev2["next_since"]
        if not any(e["type"] == "pane_exited" and e["name"] == wid for e in ev2["events"]):
            f.append(f"cycle{k}: CLOSE_PANE の pane_exited 未観測: {ev2}")
        prev_seq = int(cursor)
        # stale handle: 掃除後の旧 handle は pane_not_found (native 再利用でも新 pane に誤対応しない)
        stale_insp = c.broker.inspect_pane(wh)
        if "[pane_not_found]" not in stale_insp.get("error", ""):
            f.append(f"cycle{k}: 旧 handle inspect が pane_not_found にならない: {stale_insp}")
        stale_close = c.broker.close_pane_target(wh)
        if "[pane_not_found]" not in stale_close.get("error", ""):
            f.append(f"cycle{k}: 旧 handle close が pane_not_found にならない: {stale_close}")

        # (7) retro gate: dispatcher → secretary
        c.broker.enqueue(c.bind("dispatcher"), "secretary", f"retro gate cycle{k}: 起動可否?")
        rg = c.broker.drain(c.bind("secretary"))
        if not (len(rg) == 1 and rg[0]["from_id"] == "dispatcher"):
            f.append(f"cycle{k}: retro gate が 1 通届かない: {rg}")

        # サイクル終了時に全関係 inbox が empty (残留・先取りなし)
        for who in ("secretary", "dispatcher"):
            if c.broker.drain(c.bind(who)):
                f.append(f"cycle{k}: {who} inbox に残留 (cross-cycle 漏れ)")

        prev_worker_handle = wh
        prev_native = native

    # 全サイクルの worker token が revoke のまま (後サイクルで蘇生しない)
    for k in range(1, N + 1):
        wid = f"worker-{k}"
        if wid in c.tokens and c.broker.authorize(c.tokens[wid])[1] != "token_revoked":
            f.append(f"{wid} の token が最終的に revoke されていない")

    # 二重 spawn 規律: 同一 active agent_id の二重 spawn は [name_in_use]
    s1 = c.broker.spawn_agent("worker-dup", "worker-dup", "worker", ["claude"])
    if s1.get("ok"):
        c.broker.register_local(s1["token"])
        s2 = c.broker.spawn_agent("worker-dup", "worker-dup", "worker", ["claude"])
        if s2.get("ok") or "[name_in_use]" not in s2.get("error", ""):
            f.append(f"二重 spawn が name_in_use で拒否されない: {s2}")
        c.broker.close_pane_target(s1["handle"])

    go = not f
    detail = (f"{N} サイクル連続完走 (delegate→spawn→監視→完了報告→CLOSE_PANE→retro)。native 再利用下でも "
              "handle 別採番・旧 handle は pane_not_found・inbox/token/event cursor がサイクル間で漏れない・"
              "二重 spawn は name_in_use"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# AC-5-stall: 連続 busy 独立観測 → stall 判定 → escalation enqueue (成立物)
# ---------------------------------------------------------------------------


def _detect_stall(broker, handle, threshold: int, samples: int) -> bool:
    """inspect_pane を samples 回観測し、busy が threshold 回連続したら stall 判定。
    自己申告に依らず scrape 状態のみで判定する (dispatcher 監視ループ相当)。"""
    streak = 0
    for _ in range(samples):
        st = broker.inspect_pane(handle).get("state")
        streak = streak + 1 if st == "busy" else 0
        if streak >= threshold:
            return True
    return False


def check_stall(c: Cycle) -> tuple[bool, str]:
    f = []
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    sp = c.broker.spawn_agent("worker-stall", "worker-stall", "worker", ["claude"])
    if not sp.get("ok"):
        return False, f"spawn 失敗: {sp}"
    c.broker.register_local(sp["token"])
    native, wh = sp["pane_id"], sp["handle"]

    THRESHOLD = 3
    # stall: 連続 busy → 判定 true
    c.adapter.set_state(native, "busy")
    if not _detect_stall(c.broker, wh, THRESHOLD, samples=5):
        f.append("連続 busy で stall 判定が立たない")
    # 誤検出なし: idle / input_pending では stall 判定 false
    for st in ("idle", "input_pending"):
        c.adapter.set_state(native, st)
        if _detect_stall(c.broker, wh, THRESHOLD, samples=5):
            f.append(f"{st} で stall 誤検出")

    # 観測後の broker 成立物: dispatcher → secretary に escalation enqueue
    c.adapter.set_state(native, "busy")
    stalled = _detect_stall(c.broker, wh, THRESHOLD, samples=5)
    if stalled:
        r = c.broker.enqueue(c.bind("dispatcher"), "secretary",
                             "worker-stall が stall (連続 busy 3 回)。介入要否を判断ください")
        if not r.get("ok"):
            f.append(f"stall escalation の enqueue 失敗: {r}")
    esc = c.broker.drain(c.bind("secretary"))
    if not (len(esc) == 1 and esc[0]["from_id"] == "dispatcher" and "stall" in esc[0]["message"]):
        f.append(f"stall escalation が secretary に token 由来 from で届かない: {esc}")

    go = not f
    detail = ("連続 busy を inspect_pane で独立観測 → stall 判定 (idle/input_pending は誤検出なし) → "
              "dispatcher→secretary に escalation を broker 経由 enqueue (観測後の成立物)"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# AC-5-escalation: defer-then-deliver + 帰属 + 人間返答の worker 転送 (at-most-once)
# ---------------------------------------------------------------------------


def check_escalation(c: Cycle) -> tuple[bool, str]:
    f = []
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    sp = c.broker.spawn_agent("worker-esc", "worker-esc", "worker", ["claude"])
    if not sp.get("ok"):
        return False, f"spawn 失敗: {sp}"
    c.broker.register_local(sp["token"])
    c.tokens["worker-esc"] = sp["token"]

    # secretary を busy にして判断仰ぎを送る → nudge defer (打鍵されない)
    c.adapter.set_state("secretary", "busy")
    base_nudge = len(c.adapter.nudges_for("secretary"))
    n0_def = sum(1 for e in c.journal()
                 if e.get("event") == "nudge_deferred" and e.get("agent_id") == "secretary")
    # なりすまし: args の from_id/from_name 偽装は broker が token 由来で上書きするため、
    # enqueue 署名は from_bind のみを採る (自己申告フィールド経路が存在しない)。
    c.broker.enqueue(c.bind("worker-esc"), "secretary",
                     "判断仰ぎ: スコープ外の修正が必要そうです。続行可否を確認ください")
    if not c.wait_journal("nudge_deferred", "secretary", n0_def, timeout=3.0):
        f.append("secretary busy 中に nudge defer が記録されない")
    if len(c.adapter.nudges_for("secretary")) != base_nudge:
        f.append("secretary busy 中にナッジが打鍵された (defer 破れ)")

    # idle 復帰 → defer 解除で配達
    c.adapter.set_state("secretary", "idle")
    if not c.wait_nudge("secretary", base_nudge, timeout=5.0):
        f.append("idle 復帰後にナッジが配達されない (defer-then-deliver 破れ)")
    got = c.broker.drain(c.bind("secretary"))
    if not (len(got) == 1 and got[0]["from_id"] == "worker-esc"):
        f.append(f"判断仰ぎが token 由来 from (worker) で届かない: {got}")
    # 判断仰ぎ自体も at-most-once (secretary 側 2 回目 drain は空)。codex Major 対応
    if c.broker.drain(c.bind("secretary")):
        f.append("判断仰ぎの secretary 側 2 回目 drain が空でない (at-most-once 破れ)")

    # 人間返答を worker へ broker 経由で転送 → worker 側 at-most-once drain
    c.broker.enqueue(c.bind("secretary"), "worker-esc",
                     "人間判断: そのスコープ拡張は不可。元スコープ内で進めてください")
    reply = c.broker.drain(c.bind("worker-esc"))
    if not (len(reply) == 1 and reply[0]["from_id"] == "secretary"):
        f.append(f"人間返答が worker に token 由来 from で届かない: {reply}")
    if c.broker.drain(c.bind("worker-esc")):
        f.append("worker 側 2 回目 drain が空でない (at-most-once 破れ)")

    go = not f
    detail = ("判断仰ぎが secretary busy 中 defer → idle 後配達 (from=worker, token 由来) → "
              "人間返答を secretary→worker へ broker 転送 → worker at-most-once drain"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# AC-5-handover: ops tier inspect+send_keys で dispatcher 引き継ぎ + 監視 cursor 不喪失
# ---------------------------------------------------------------------------


def check_handover(c: Cycle) -> tuple[bool, str]:
    f = []
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    # 監視対象 worker を spawn (handover 中も監視が続く前提)
    sp = c.broker.spawn_agent("worker-ho", "worker-ho", "worker", ["claude"])
    if not sp.get("ok"):
        return False, f"spawn 失敗: {sp}"
    c.broker.register_local(sp["token"])
    base = c.broker.poll_events(since=None, timeout_ms=0)
    cursor_before = base["next_since"]  # handover 前 cursor

    dh = c.handle_of("dispatcher")
    # secretary token の **ops tier MCP surface (call_tool)** 経由で引き継ぐ (tier 認可も exercise)。
    # codex Minor 対応: 直呼びではなく secretary bind の call_tool で tier 通過を実証する。
    sec_bind = c.bind("secretary")

    def _sec_call(tool, args):
        r = c.broker.call_tool(sec_bind, tool, args)
        if r.get("isError"):
            return {"_isError": True, "text": r["content"][0]["text"]}
        return json.loads(r["content"][0]["text"])

    insp = _sec_call("inspect_pane", {"target": dh})
    if insp.get("_isError") or insp.get("state") not in ("idle", "input_pending", "busy"):
        f.append(f"secretary が ops tier で dispatcher を inspect_pane できない: {insp}")
    r1 = _sec_call("send_keys", {"target": dh, "text": "/clear", "enter": True})
    r2 = _sec_call("send_keys", {"target": dh, "text": "/dispatcher-resume", "enter": True})
    if not (r1.get("ok") and r2.get("ok")):
        f.append(f"ops tier send_keys (call_tool) 失敗: {r1} / {r2}")
    keys = c.adapter.keys_for("dispatcher")
    if not (any(k["text"] == "/clear" for k in keys)
            and any(k["text"] == "/dispatcher-resume" for k in keys)):
        f.append(f"send_keys 記録に /clear・/dispatcher-resume が無い: {keys}")

    # handover 中に worker の lifecycle イベント (close) を発生させる
    wh = c.handle_of("worker-ho")
    c.broker.close_pane_target(wh)

    # dispatcher ペインは生存 (handover で閉じていない): pane_exited を emit していない
    ev = c.broker.poll_events(since=cursor_before, timeout_ms=0)
    disp_exited = [e for e in ev["events"]
                   if e["type"] == "pane_exited" and e["name"] == "dispatcher"]
    if disp_exited:
        f.append(f"handover で dispatcher ペインが閉じた (pane_exited emit): {disp_exited}")
    # handover 前 cursor から worker の pane_exited を取りこぼさない (監視 cursor 不喪失)
    worker_exited = [e for e in ev["events"]
                     if e["type"] == "pane_exited" and e["name"] == "worker-ho"]
    if not worker_exited:
        f.append(f"handover 前 cursor から worker lifecycle を取りこぼした: {ev}")
    if c.handle_of("dispatcher") is None:
        f.append("handover 後に dispatcher pane が list から消えた (ペイン保持破れ)")

    go = not f
    detail = ("secretary が ops tier inspect_pane + send_keys(/clear・/dispatcher-resume) で dispatcher を "
              "ペインを閉じず引き継ぎ、handover 中の worker lifecycle を handover 前 cursor から取りこぼさない"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# AC-5-resume: suspend (全 revoke + 未読破棄) → token 再発行 → 新 lifecycle 成立 + stale 非継承
# ---------------------------------------------------------------------------


def check_resume(c: Cycle) -> tuple[bool, str]:
    f = []
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    sec_tok_old = c.tokens["secretary"]
    disp_tok_old = c.tokens["dispatcher"]

    # 稼働中に secretary 宛へ未配達を 1 通積む (suspend で破棄される想定)
    c.adapter.set_state("secretary", "busy")  # nudge は defer、queue には載る
    c.broker.enqueue(c.bind("dispatcher"), "secretary", "suspend 前の未配達メッセージ")

    # suspend: 全 token revoke (戻り値 = revoke 数)
    n = c.broker.suspend()
    if n < 2:
        f.append(f"suspend が全 token を revoke しない (revoked={n})")
    # 旧 token は token_revoked で拒否
    if c.broker.authorize(sec_tok_old)[1] != "token_revoked":
        f.append("suspend 後に secretary 旧 token が revoke されていない")
    if c.broker.get_bind(disp_tok_old) is not None:
        f.append("suspend 後に dispatcher 旧 token が有効なまま")
    # 旧 token での送信は拒否される (失効送信者)
    revoked_bind = c.broker._binds.get(disp_tok_old)
    snd = c.broker.enqueue(revoked_bind, "secretary", "失効 token からの送信")
    if snd.get("ok"):
        f.append("失効 token からの enqueue が拒否されない")

    # resume: token 再発行 (別 token) → register
    sec_tok_new = c.broker.issue_token("secretary", "secretary", "secretary", pane_id="secretary")
    disp_tok_new = c.broker.issue_token("dispatcher", "dispatcher", "dispatcher", pane_id="dispatcher")
    if sec_tok_new == sec_tok_old or disp_tok_new == disp_tok_old:
        f.append("resume で同一 token が再利用された (別 token であるべき)")
    c.broker.register_local(sec_tok_new)
    c.broker.register_local(disp_tok_new)
    c.tokens["secretary"] = sec_tok_new
    c.tokens["dispatcher"] = disp_tok_new
    c.adapter.set_state("secretary", "idle")

    # stale 非継承: suspend 前の未読は新 bind に漏れない (新 queue は空)
    leftover = c.broker.drain(c.bind("secretary"))
    if leftover:
        f.append(f"suspend 前の未読が新 lifecycle に継承された (stale 漏れ): {leftover}")

    # 新 lifecycle で送受信成立。さらに「旧 token は新 lifecycle の queue を読めない」を
    # authorized 経路 (get_bind / authorize) で固定する。secretary は resume 後も同一 agent_id
    # のため queue は agent_id 共有だが、旧 token は revoke 済みで authorized 経路に載らない
    # (MCP の check_messages は call_tool→authorize で弾かれる)。direct broker.drain() は
    # server-side 合成経路で auth を見ない設計のため、ここでは認可境界を assert する。codex Major 対応。
    c.broker.enqueue(c.bind("dispatcher"), "secretary", "resume 後の新規メッセージ")
    if c.broker.get_bind(sec_tok_old) is not None:
        f.append("旧 token が新 lifecycle で authorized 経路に復活した (stale 読み取り可能)")
    if c.broker.authorize(sec_tok_old)[0] is not None:
        f.append("旧 token が authorize を通過する (新 queue への読み取り経路が残る)")
    fresh = c.broker.drain(c.bind("secretary"))
    if not (len(fresh) == 1 and fresh[0]["from_id"] == "dispatcher"
            and "resume 後" in fresh[0]["message"]):
        f.append(f"resume 後に新 token で送受信が成立しない: {fresh}")

    go = not f
    detail = ("suspend で全 token revoke + 未読破棄 (旧 token は token_revoked・失効送信も拒否) → "
              "token 再発行 (別 token) → 旧 lifecycle 未読は非継承 (新 queue 空) → 新 token で送受信成立"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# AC-5-billing: spawn argv builder が対話 TUI (headless 落ちなし) の構造保証
# ---------------------------------------------------------------------------


def check_billing(c: Cycle) -> tuple[bool, str]:
    f = []
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)

    # spawnable role (worker / curator) ごとに argv builder を検査する
    for role in SPAWNABLE_ROLES:
        wid = f"agent-{role}"
        before = len(c.adapter.split_argv)
        sp = c.broker.spawn_agent(wid, wid, role, ["claude"])
        if not sp.get("ok"):
            f.append(f"{role}: spawn 失敗: {sp}")
            continue
        c.broker.register_local(sp["token"])
        if len(c.adapter.split_argv) <= before:
            f.append(f"{role}: launch argv が記録されていない")
            continue
        argv = c.adapter.split_argv[-1]
        # 対話 TUI: claude 本体 + --mcp-config のみ。ヘッドレス系 flag を含まない。
        if argv[0] != "claude":
            f.append(f"{role}: argv[0] が claude でない: {argv}")
        if "--mcp-config" not in argv:
            f.append(f"{role}: --mcp-config 注入が無い (接続経路欠落): {argv}")
        for bad in HEADLESS_FLAGS:
            if bad in argv:
                f.append(f"{role}: ヘッドレス flag {bad} が混入 (課金中立破れ): {argv}")
        # MCP 応答に token を露出しない (Phase 4 既証の再確認)。spawn_agent 内部の dict は
        # token を持つが、MCP 面 (call_tool) では除去される。ここでは config ファイルに
        # token が 0600 で隔離され、argv には平文 token が無い (path 参照のみ) ことを確認。
        cfg_idx = argv.index("--mcp-config")
        cfg_path = Path(argv[cfg_idx + 1])
        if sp["token"] in argv:
            f.append(f"{role}: argv に平文 token が露出 (path 経由であるべき): {argv}")
        if not cfg_path.exists():
            f.append(f"{role}: --mcp-config の指す 0600 config が存在しない: {cfg_path}")
        c.broker.close_pane_target(sp["handle"])

    # 課金中立の **構造強制 (allowlist / default-deny)**: token 注入 spawn は対話 claude TUI の
    # 正規 flag のみ許可し、headless 系・flag 無しラッパー・非 TUI サブコマンド・flag 後サブコマンド・
    # `--`・未知 flag・値位置の headless flag・空 argv を一律拒否する (人間判断で allowlist 化を選択)。
    bad_cases = [
        (["claude", "-p", "x"], "[headless_forbidden]"),
        (["claude", "--print"], "[headless_forbidden]"),
        (["claude", "--headless"], "[headless_forbidden]"),
        (["claude", "--output-format", "json"], "[headless_forbidden]"),
        (["claude", "--output-format=json"], "[headless_forbidden]"),
        (["python", "agent_sdk_worker.py"], "[headless_forbidden]"),  # flag 無し headless ラッパー
        (["node", "agent.js"], "[headless_forbidden]"),
        (["claude", "mcp", "serve"], "[headless_forbidden]"),         # 非 TUI サブコマンド
        (["claude", "doctor"], "[headless_forbidden]"),
        (["claude", "--strict-mcp-config", "mcp", "serve"], "[headless_forbidden]"),  # flag 後サブコマンド
        (["claude", "--", "mcp", "serve"], "[headless_forbidden]"),   # `--` バイパス
        (["claude", "--unknown-flag"], "[headless_forbidden]"),       # allowlist 外 flag
        (["claude", "--model", "-p"], "[headless_forbidden]"),        # 値位置の headless flag (blacklist)
        ([], "[invalid-params]"),                                      # 空 argv
    ]
    for bad_argv, want in bad_cases:
        r = c.broker.spawn_agent("agent-bad", "agent-bad", "worker", bad_argv)
        if r.get("ok") or want not in r.get("error", ""):
            f.append(f"危険 argv {bad_argv} が {want} で拒否されない: {r}")
            if r.get("ok"):
                c.broker.close_pane_target(r["handle"])

    # allowlist 適合の対話 flag は許可される (false-reject が無いこと)。
    for good_argv in (["claude", "--model", "sonnet", "--strict-mcp-config"],
                      ["claude", "--allowedTools", "mcp__org-broker__send_message"]):
        r = c.broker.spawn_agent("agent-ok", "agent-ok", "worker", good_argv)
        if not r.get("ok"):
            f.append(f"allowlist 適合 argv {good_argv} が誤って拒否された: {r}")
        else:
            c.broker.close_pane_target(r["handle"])

    go = not f
    detail = ("spawnable 各 role の spawn argv が 'claude --mcp-config <0600 path>' の対話 TUI で、"
              "-p/--print/--headless/--output-format 等のヘッドレス flag を構造的に含まない "
              "(平文 token も argv 非露出)"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# 実 tmux 手動ランナー (sandbox 無効 / 無課金) — §4 の実機 attestation
# ---------------------------------------------------------------------------


def _real_tmux_cat_smoke() -> tuple[bool, list[str]]:
    """実 tmux で cat プローブ 2 サイクル smoke (無課金)。基準1 の backend 実在性証跡。"""
    from tmux_adapter import TmuxAdapter
    f: list[str] = []
    adapter = TmuxAdapter(width=240, height=50)
    broker = Broker(state_dir=OUT / "smoke", adapter=adapter, event_poll_interval=0.05)
    try:
        base_ref = adapter.spawn(["cat"])
        dtok = broker.issue_token("dispatcher", "dispatcher", "dispatcher", pane_id=base_ref.pane_id)
        broker.register_local(dtok)
        time.sleep(0.3)
        cursor = broker.poll_events(since=None, timeout_ms=0)["next_since"]
        for k in range(1, 3):  # 2 サイクル連続
            wid = f"worker-smoke-{k}"
            sp = broker.spawn_agent(wid, wid, "worker", ["cat"], inject_mcp_config=False)
            if not sp.get("ok"):
                sp = broker.spawn_agent(wid, wid, "worker", ["cat"],
                                        target=broker.mcp_list_panes()[0]["id"],
                                        inject_mcp_config=False)
            if not sp.get("ok"):
                f.append(f"cycle{k}: 実 tmux split spawn 失敗: {sp}")
                break
            broker.register_local(sp["token"])
            time.sleep(0.3)
            wh = sp["handle"]
            ev = broker.poll_events(since=cursor, timeout_ms=0)
            cursor = ev["next_since"]
            if not any(e["type"] == "pane_started" for e in ev["events"]):
                f.append(f"cycle{k}: 実 tmux pane_started 未観測")
            broker.send_keys_op(wh, text=f"hello-cycle{k}", enter=True)
            time.sleep(0.3)
            if f"hello-cycle{k}" not in broker.inspect_pane(wh).get("text", ""):
                f.append(f"cycle{k}: 実 tmux send_keys/inspect 往復不成立")
            broker.close_pane_target(wh)
            time.sleep(0.3)
            ev2 = broker.poll_events(since=cursor, timeout_ms=0)
            cursor = ev2["next_since"]
            if not any(e["type"] == "pane_exited" for e in ev2["events"]):
                f.append(f"cycle{k}: 実 tmux pane_exited 未観測")
            if broker.authorize(sp["token"])[1] != "token_revoked":
                f.append(f"cycle{k}: 実 tmux close 後に token_revoked にならない")
    except Exception as e:  # pragma: no cover
        f.append(f"実 tmux smoke 例外: {e}")
    finally:
        try:
            adapter.kill_server()
        except Exception:
            pass
        broker.stop()
    return (not f), f


def _claude_argv_via_ps() -> list[str]:
    """実行中の claude プロセスの実 argv を ps で取得する (課金中立の実測証跡)。"""
    import subprocess
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines()
            if "--mcp-config" in ln and "claude" in ln.lower()]


def real_claude_active_cycle() -> tuple[bool, str, dict]:
    """**実 Claude を active で 1 サイクル回す** 真の end-to-end dogfood (人間承認済み)。

    委託 (broker 経由 / renga 不使用) → 実 Claude が実作業 → 完了報告 (broker 経由 / token 由来 from)
    → クローズ (token revoke) の 1 サイクルを実 tmux + 実 claude で完走させる。1 回のみ。
    併せて (a) 起動直後 idle ❯ の対話 TUI 描画 (turn 未投入 = active 推論なし) と (b) 実 argv (ps)
    が headless/print 系を含まないこと を課金中立の実測 attestation として記録する。

    AC5_REAL_CLAUDE=1 のときだけ起動する (CI / sandbox では決して走らない)。
    """
    import os
    import shutil
    if shutil.which("tmux") is None or shutil.which("claude") is None:
        return True, "SKIP (tmux/claude 不在)", {}
    if os.environ.get("AC5_REAL_CLAUDE") != "1":
        return True, "SKIP (実 claude active 1 サイクルは AC5_REAL_CLAUDE=1 + 人間承認で enable)", {}

    from harness import SpikeSession, AGENT_ID  # 実 Claude 起動チェーンの proven 実装を再利用
    f: list[str] = []
    ev: dict = {}
    s = SpikeSession(state_dir=OUT / "active", model="sonnet", backend="tmux")
    try:
        s.start()
        # 委譲元 dispatcher (synthetic) と 完了報告先 observer(=secretary 相当) を bind。
        disp_tok = s.broker.issue_token("dispatcher", "dispatcher", "dispatcher")
        s.broker.register_local(disp_tok)
        s.spawn_claude()
        # --- 課金中立 実測 (b): 実 argv (ps) が headless 系を含まない ---
        time.sleep(1.0)
        argv_lines = _claude_argv_via_ps()
        ev["claude_argv_ps"] = argv_lines
        joined = " ".join(argv_lines)
        # ps で実 argv を捕捉できなければ attestation 失敗 (空を素通しさせない。codex Major 対応)
        if not argv_lines:
            f.append("実 claude プロセスの argv を ps で捕捉できない (課金中立 attestation 不成立)")
        else:
            for bad in HEADLESS_FLAGS:
                if f" {bad}" in joined or joined.endswith(bad):
                    f.append(f"実 claude argv にヘッドレス flag {bad} 混入: {joined}")
            if "--mcp-config" not in joined:
                f.append("実 claude argv に --mcp-config が無い")
        # --- 起動 (対話 TUI) + 登録 (broker 接続) ---
        ready = s.wait_ready(timeout=150)
        registered = s.wait_registered(timeout=30)
        ev["ready_seconds"] = s.obs.ready_seconds
        ev["registered_seconds"] = s.obs.registered_seconds
        ev["folder_trust_prompt"] = s.obs.folder_trust_prompt
        # --- 課金中立 実測 (a): idle ❯ 到達時の対話 TUI 描画 (turn 未投入) ---
        from terminal_adapter import classify_pane_state
        idle_full = s.screen()
        ev["idle_screen"] = idle_full[-1500:]
        ev["idle_screen_state"] = classify_pane_state(idle_full)
        if not ready:
            f.append("実 claude が idle (対話 TUI) に到達しない (150s timeout / ヘッドレス疑い)")
        elif ev["idle_screen_state"] != "idle":
            # 保存する証跡自体を機械判定する (wait_ready の過去判定に依存しない。codex Minor 対応)
            f.append(f"idle attestation 証跡が idle 描画でない: state={ev['idle_screen_state']}")
        if not registered:
            f.append("実 claude が broker に登録しない (30s timeout)")
        if ready and registered:
            # --- 委託 (broker 経由 / renga 不使用): dispatcher → worker ---
            task = (
                "これは renga 不使用・org-broker 経由の委譲です (dogfood)。"
                "簡単な実作業: 2 + 2 を計算してください。"
                "完了したら org-broker の send_message を to_id='observer'、"
                "message='完了報告: 2+2=4 / dogfood active cycle 完走' で 1 回だけ呼んでください。"
            )
            s.broker.enqueue(s.broker.get_bind(disp_tok), AGENT_ID, task)
            # 運用 CLAUDE.md 相当の初手指示: check_messages で broker から指示を取得させる
            s.prompt(
                "org-broker の check_messages を呼んで届いている指示を読み、その作業を実施し、"
                "指示どおり send_message で完了報告してください。"
            )
            # --- 完了報告 (broker 経由 / token 由来 from) を observer inbox で待つ ---
            got = None
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                msgs = s.broker.drain(s.broker.get_bind(s.observer_token))
                if msgs:
                    got = msgs[0]
                    break
                time.sleep(3)
            ev["final_screen"] = s.screen()[-2000:]
            if not (got and got["from_id"] == AGENT_ID):
                f.append(f"完了報告が broker 経由で届かない or 帰属不正: {got}")
            else:
                ev["completion"] = {"from_id": got["from_id"], "message": got["message"]}
            # --- クローズ: token revoke + pane 退役 ---
            revoked = s.broker.close_pane(s.pane.pane_id)
            ev["closed_agents"] = revoked
            if s.broker.authorize(s.token)[1] != "token_revoked":
                f.append("close 後に worker token が revoke されない")
    except Exception as e:  # pragma: no cover
        f.append(f"実 claude active cycle 例外: {e!r}")
        try:
            ev["exception_screen"] = s.screen()[-2000:]
        except Exception:
            pass
    finally:
        try:
            s.teardown(kill_pane=True)
        except Exception:
            pass
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "active-evidence.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    go = not f
    detail = (
        "実 Claude (sonnet) を broker 経由で委譲 → 実作業(2+2) → broker 経由で完了報告 "
        f"(from='{ev.get('completion', {}).get('from_id')}' token 由来) → close/revoke の 1 サイクル完走。"
        f"対話 TUI idle 到達 {ev.get('ready_seconds')}s / 登録 {ev.get('registered_seconds')}s / "
        "実 argv に headless flag なし (課金中立 実測)"
        if go else "; ".join(f)
    )
    return go, detail, ev


def real_tmux_dogfood() -> tuple[bool, str]:
    """実機 dogfood: cat smoke 2 サイクル (無課金) + 実 Claude active 1 サイクル (人間承認・要 AC5_REAL_CLAUDE=1)。

    sandbox 無効が必要 (tmux unix socket)。tmux/claude 不在は skip。
    """
    import shutil
    if shutil.which("tmux") is None:
        return True, "SKIP (tmux 不在: 実機 smoke 省略、FakeAdapter で代替検証済み)"
    notes: list[str] = []
    fails: list[str] = []

    smoke_go, smoke_f = _real_tmux_cat_smoke()
    if smoke_go:
        notes.append("cat プローブ 2 サイクル連続 (spawn/send_keys/inspect/poll_events/close) 往復")
    else:
        fails.extend(smoke_f)

    active_go, active_detail, _ = real_claude_active_cycle()
    if active_go:
        notes.append(active_detail)
    else:
        fails.append(f"実 claude active cycle: {active_detail}")

    go = not fails
    return go, ("; ".join(notes) if go else "; ".join(fails))


# ---------------------------------------------------------------------------
# ランナー
# ---------------------------------------------------------------------------

CHECKS = [
    ("AC-5-multi", check_multi),
    ("AC-5-stall", check_stall),
    ("AC-5-escalation", check_escalation),
    ("AC-5-handover", check_handover),
    ("AC-5-resume", check_resume),
    ("AC-5-billing", check_billing),
]


def run(real_tmux: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    for name, fn in CHECKS:
        c = Cycle()  # 各 Cycle は専用 state_dir を持つ (journal 分離)
        try:
            go, detail = fn(c)
        finally:
            c.teardown()
        results[name] = {"go": go, "detail": detail}
        log(f"{'GO   ' if go else 'NO-GO'} {name}: {detail}")
    if real_tmux:
        go, detail = real_tmux_dogfood()
        results["AC-5-real-tmux"] = {"go": go, "detail": detail}
        log(f"{'GO   ' if go else 'NO-GO'} AC-5-real-tmux: {detail}")
    return results


def main() -> int:
    real = "--real-tmux" in sys.argv
    results = run(real_tmux=real)
    go = bool(results) and all(r["go"] for r in results.values())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(
        json.dumps(
            {
                "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": "B (broker queue 統合ハーネス / FakeAdapter) + 実 tmux dogfood (--real-tmux)",
                "results": results,
                "go": go,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print("AC-5 完動ゲート dogfood: " + ("GO (全項目 green)" if go else "NO-GO"))
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())
