# -*- coding: utf-8 -*-
"""AC-4 統合検証: ペイン操作 6 面の broker 配線替え + 監視 1 サイクル完走 (設計書 §7.4)。

検証方式は Phase 3 と同じ **方式 B**（FakeAdapter / 無課金・決定的・CI 可）を主とし、
窓口経由のユーザー判断 (2026-06-10) により **実 tmux smoke** を追加する（SoT §7.4 の
「該当 backend 実機で 1 サイクル完走」を、本 Linux/WSL2 環境では正準 backend の tmux に
読み替え。WezTerm は Windows 専用のため別途。Phase 2 の tmux 実機 AC 前例に沿う）。

検証項目:
  AC-4-surface:  6 面 (spawn_agent/close_pane/list_panes/inspect_pane/send_keys/poll_events)
                 + set_pane_identity が ops tier で往復し、worker token では tools/list にも
                 出ず call_tool が [tool_forbidden] で弾く（権限分離の構造的担保、item 4）。
  AC-4-events:   poll_events 合成 — 初回 since=None は baseline（履歴 replay 無し）、
                 baseline→spawn→since 付き poll で pane_started 観測、broker 非経由の直 kill
                 取りこぼしが list_panes reconcile で pane_exited 回復、events_dropped は count 付き。
                 pane_exited payload に name/role/agent_id（item 2）。
  AC-4-split:    balanced split が claude_org_runtime.choose_split 再利用で現行同等。geometry
                 正規化（left/top↔x/y・active↔focused）後の判定が SoT と一致、候補空で
                 split_capacity_exceeded（item 3）。
  AC-4-cycle:    delegate → spawn（balanced）→ 監視（inspect_pane で stall / 承認待ち観測）→
                 完了報告 → CLOSE_PANE（pane_exited + token revoke）→ retro の 1 サイクル完走。
  AC-4-cadence:  3 分 cadence を縮約した反復 reconcile で pane_exited 取りこぼしが次サイクルまでに
                 回復し、監視ループの正しさを損なわない（item 2）。

FakeAdapter で geometry / pane 生死 / 画面状態 / split / send_keys を決定的に駆動する。
classify_pane_state は実 Claude TUI と同一描画を前提に Phase 1/2 で実測較正済み。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from broker import Broker, role_tier, tools_for_role  # noqa: E402
from terminal_adapter import PaneRef  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

OUT = Path(__file__).parent / "broker-state" / "ac4"

# 実 Claude TUI (claude 2.1.168) 描画再現の固定スクリーン (Phase 1/2 較正済み)。
_SCREENS = {
    "idle": "──────────────────────────\n❯ \n──────────────────────────\n",
    "input_pending": (
        "──────────────────────────\n❯ 未送信の承認応答が入力欄にあります\n"
        "──────────────────────────\n"
    ),
    "busy": "応答を生成中…\n  (esc to interrupt)\n",
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# FakeAdapter: geometry / 生死 / 画面状態 / split / send_keys を決定的に駆動
# ---------------------------------------------------------------------------


class FakeAdapter:
    """TerminalAdapter Protocol の Phase 4 検証用実装。

    list_panes は geometry 付きで **生存 pane のみ** を返す（kill された pane は
    消える = poll_events 合成が pane_exited を導ける）。geometry キーは broker の
    正規化を検証するため、生成時に指定したキー様式（x/y か left/top）で返す。
    """

    def __init__(self, geom_keys: str = "xy") -> None:
        self._panes: dict[str, dict] = {}
        self._counter = 0
        self._geom_keys = geom_keys  # "xy" (Fake 既定) / "lefttop" (tmux 様式)

    # -- 検証ドライバ ------------------------------------------------------
    def add_pane(self, pane_id: str, x=0, y=0, width=100, height=40,
                 state="idle", focused=False) -> None:
        self._panes[pane_id] = {
            "x": x, "y": y, "width": width, "height": height,
            "state": state, "alive": True, "focused": focused,
            "nudges": [], "keys": [], "polls": 0,
            "flip_after": None, "flip_to": None, "cursor_x": 1, "cursor_y": 1,
        }

    def set_state(self, pane_id: str, state: str) -> None:
        self._panes[pane_id]["state"] = state

    def schedule_flip(self, pane_id: str, after_polls: int, to_state: str) -> None:
        p = self._panes[pane_id]
        p["flip_after"] = after_polls
        p["flip_to"] = to_state

    def nudges_for(self, pane_id: str) -> list[str]:
        return list(self._panes[pane_id]["nudges"])

    def keys_for(self, pane_id: str) -> list[dict]:
        return list(self._panes[pane_id]["keys"])

    # -- TerminalAdapter 面 ------------------------------------------------
    def list_panes(self) -> list[dict]:
        out = []
        for pid, p in self._panes.items():
            if not p["alive"]:
                continue  # kill された pane は list から消える (実 backend と同型)
            rec = {"pane_id": pid, "width": p["width"], "height": p["height"],
                   "cursor_x": p["cursor_x"], "cursor_y": p["cursor_y"]}
            if self._geom_keys == "lefttop":
                rec["left"] = p["x"]; rec["top"] = p["y"]; rec["active"] = p["focused"]
            else:
                rec["x"] = p["x"]; rec["y"] = p["y"]; rec["focused"] = p["focused"]
            out.append(rec)
        return out

    def pane_exists(self, pane_id: str) -> bool:
        return self._panes.get(pane_id, {}).get("alive", False)

    def get_text(self, pane_id: str, escapes: bool = False) -> str:
        p = self._panes[pane_id]
        p["polls"] += 1
        if p["flip_after"] is not None and p["polls"] > p["flip_after"]:
            p["state"] = p["flip_to"]
        return _SCREENS[p["state"]]

    def split(self, target, argv, cwd=None, direction="vertical") -> PaneRef:
        t = self._panes[target]
        if not t["alive"]:
            raise RuntimeError(f"split target {target} not alive")
        self._counter += 1
        nid = f"w{self._counter}"
        if direction == "vertical":  # 左右: 既存=左 / 新=右
            half = t["width"] // 2
            self.add_pane(nid, x=t["x"] + (t["width"] - half), y=t["y"],
                          width=half, height=t["height"])
            t["width"] -= half
        else:  # 上下: 既存=上 / 新=下
            half = t["height"] // 2
            self.add_pane(nid, x=t["x"], y=t["y"] + (t["height"] - half),
                          width=t["width"], height=half)
            t["height"] -= half
        return PaneRef(pane_id=nid)

    def send_keys(self, pane_id, text=None, keys=None, enter=False) -> None:
        self._panes[pane_id]["keys"].append(
            {"text": text, "keys": list(keys or []), "enter": enter}
        )

    def send_line(self, pane_id, text, settle=0.0) -> None:
        self._panes[pane_id]["nudges"].append(text)

    def kill_pane(self, pane_id) -> None:
        if pane_id in self._panes:
            self._panes[pane_id]["alive"] = False

    # -- Protocol スタブ (本ハーネス未使用) --------------------------------
    def spawn(self, argv, cwd=None, new_window=True) -> PaneRef:  # pragma: no cover
        self._counter += 1
        nid = f"s{self._counter}"
        self.add_pane(nid)
        return PaneRef(pane_id=nid)

    def type_text(self, pane_id, text) -> None:  # pragma: no cover
        pass

    def send_enter(self, pane_id) -> None:  # pragma: no cover
        pass

    def send_interrupt(self, pane_id) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Cycle: broker + FakeAdapter + 役割 token の結線
# ---------------------------------------------------------------------------


class Cycle:
    def __init__(self, geom_keys: str = "xy", event_cap: int = 1000) -> None:
        self.adapter = FakeAdapter(geom_keys=geom_keys)
        self.broker = Broker(
            state_dir=OUT / "state", adapter=self.adapter,
            nudge_defer_interval=0.01, nudge_defer_max_tries=200,
            event_cap=event_cap, event_poll_interval=0.01,
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

    def call(self, agent_id, tool, args=None):
        """call_tool 経由 (tier 判定込み) でツールを叩く。"""
        return self.broker.call_tool(self.bind(agent_id), tool, args or {})

    def call_result(self, agent_id, tool, args=None):
        """call_tool の content text を JSON parse して返す (isError も判別可)。"""
        r = self.call(agent_id, tool, args)
        if r.get("isError"):
            return {"_isError": True, "text": r["content"][0]["text"]}
        return json.loads(r["content"][0]["text"])

    def handle_of(self, name) -> int | None:
        for rec in self.broker.mcp_list_panes():
            if rec["name"] == name:
                return rec["id"]
        return None

    def teardown(self):
        self.broker.stop()


# ---------------------------------------------------------------------------
# 検証本体
# ---------------------------------------------------------------------------


def check_surface(c: Cycle) -> tuple[bool, str]:
    """AC-4-surface: 6 面 + identity が ops tier で往復、worker は構造的遮断。"""
    f = []
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("worker-x", "worker", 140, 43, 140, 43)

    # tools/list フィルタ: worker=4 / dispatcher=11
    if len(tools_for_role("worker")) != 4:
        f.append(f"worker tools/list={len(tools_for_role('worker'))} != 4")
    if len(tools_for_role("dispatcher")) != 11:
        f.append(f"dispatcher tools/list={len(tools_for_role('dispatcher'))} != 11")

    # worker token で pane 操作 → [tool_forbidden]
    for tool in ("list_panes", "inspect_pane", "send_keys", "poll_events",
                 "close_pane", "spawn_agent", "set_pane_identity"):
        r = c.call("worker-x", tool, {"target": 1})
        if not (r.get("isError") and "[tool_forbidden]" in r["content"][0]["text"]):
            f.append(f"worker が {tool} を呼べた (権限分離破れ): {r}")

    # dispatcher token で 6 面が往復する
    panes = c.call_result("dispatcher", "list_panes")
    if "_isError" in panes or not panes.get("panes"):
        f.append(f"dispatcher list_panes 失敗: {panes}")
    disp_h = c.handle_of("dispatcher")
    insp = c.call_result("dispatcher", "inspect_pane", {"target": disp_h})
    if insp.get("_isError") or "state" not in insp:
        f.append(f"inspect_pane 失敗: {insp}")
    sk = c.call_result("dispatcher", "send_keys",
                       {"target": disp_h, "keys": ["Shift+Tab"], "enter": False})
    if not sk.get("ok"):
        f.append(f"send_keys 失敗: {sk}")
    if c.adapter.keys_for("dispatcher") != [{"text": None, "keys": ["Shift+Tab"], "enter": False}]:
        f.append(f"send_keys 記録不一致: {c.adapter.keys_for('dispatcher')}")
    # 未知キーは invalid-params
    bad = c.call_result("dispatcher", "send_keys", {"target": disp_h, "keys": ["Nope"]})
    if not (not bad.get("ok") and "[invalid-params]" in bad.get("error", "")):
        f.append(f"未知キーが invalid-params にならない: {bad}")
    pe = c.call_result("dispatcher", "poll_events", {"timeout_ms": 0})
    if "next_since" not in pe:
        f.append(f"poll_events 形不正: {pe}")
    spm = c.call_result("dispatcher", "spawn_agent",
                        {"agent_id": "worker-s", "name": "worker-s", "role": "worker",
                         "argv": ["claude"]})
    if not spm.get("ok") or "token" in spm:  # token は MCP 応答に載らない
        f.append(f"spawn_agent (MCP) 失敗 or token 漏洩: {spm}")

    go = not f
    detail = ("6 面 + identity が ops tier で往復。worker token は tools/list に pane 操作が"
              "出ず call_tool も [tool_forbidden]。spawn_agent の MCP 応答に token 非露出"
              if go else "; ".join(f))
    return go, detail


def check_events(c: Cycle) -> tuple[bool, str]:
    """AC-4-events: baseline / pane_started / 取りこぼし回復 / events_dropped(count)。"""
    f = []
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)

    # 初回 since=None = baseline (履歴 replay 無し)
    base = c.broker.poll_events(since=None, timeout_ms=0)
    if base["events"] or "next_since" not in base:
        f.append(f"baseline が空でない/形不正: {base}")
    s0 = base["next_since"]

    # spawn → since=s0 poll で pane_started (name/role/agent_id/handle 付き)
    sp = c.broker.spawn_agent("worker-1", "worker-1", "worker", ["claude"])
    if not sp.get("ok"):
        f.append(f"spawn_agent 失敗: {sp}")
    ev1 = c.broker.poll_events(since=s0, timeout_ms=0)
    started = [e for e in ev1["events"] if e["type"] == "pane_started"]
    if not started:
        f.append(f"pane_started 未観測: {ev1}")
    elif not (started[-1]["name"] == "worker-1" and started[-1]["role"] == "worker"
              and started[-1]["agent_id"] == "worker-1" and "id" in started[-1]):
        f.append(f"pane_started payload 不足: {started[-1]}")
    s1 = ev1["next_since"]

    # broker 非経由の直 kill (取りこぼし) → 次の reconcile で pane_exited 回復
    c.adapter.kill_pane(sp["pane_id"])
    ev2 = c.broker.poll_events(since=s1, timeout_ms=0)
    exited = [e for e in ev2["events"] if e["type"] == "pane_exited"]
    if not exited:
        f.append(f"直 kill 取りこぼしが reconcile で回復しない: {ev2}")
    elif not (exited[-1]["name"] == "worker-1" and exited[-1]["role"] == "worker"):
        f.append(f"pane_exited payload に meta 無し (exit 後復元不可のはず): {exited[-1]}")

    # events_dropped: ring を小さくして溢れさせ、古い since で poll
    c2 = Cycle(event_cap=3)
    try:
        c2.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
        b2 = c2.broker.poll_events(since=None, timeout_ms=0)
        old = b2["next_since"]
        # adapter に直接 pane を足しては消し、3 超のイベントを生成 (ring trim 発生)。
        # spawn_agent の split-capacity に依らず確実に多数イベントを作る。
        for i in range(6):
            c2.adapter.add_pane(f"d{i}", x=0, y=0, width=40, height=10)
            c2.broker._reconcile()              # pane_started 合成
            c2.adapter.kill_pane(f"d{i}")
            c2.broker._reconcile()              # pane_exited 合成
        dropped = c2.broker.poll_events(since=old, timeout_ms=0)
        de = [e for e in dropped["events"] if e["type"] == "events_dropped"]
        if not de:
            f.append(f"events_dropped 未発火 (ring trim 後): {dropped}")
        elif not (isinstance(de[0].get("count"), int) and de[0]["count"] > 0):
            f.append(f"events_dropped に count 無し: {de[0]}")
    finally:
        c2.teardown()

    go = not f
    detail = ("baseline は履歴 replay 無し、spawn→pane_started 観測 (name/role/agent_id/handle)、"
              "直 kill 取りこぼしが list_panes reconcile で pane_exited 回復 (meta 保持)、"
              "ring trim で count 付き events_dropped"
              if go else "; ".join(f))
    return go, detail


def check_split(c: Cycle) -> tuple[bool, str]:
    """AC-4-split: choose_split 再利用で現行同等 + geometry 正規化 + capacity 検出。"""
    from claude_org_runtime.dispatcher import runner
    f = []

    # geometry キーを tmux 様式 (left/top/active) にして broker 正規化を検証する
    cc = Cycle(geom_keys="lefttop")
    try:
        cc.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
        cc.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
        records = cc.broker.mcp_list_panes()
        # broker 正規化が left/top→x/y を埋めていること
        if not all("x" in r and "y" in r for r in records):
            f.append(f"left/top→x/y 正規化漏れ: {records}")
        # broker の balanced split が SoT choose_split と一致 (現行同等の構造的根拠)
        broker_choice = cc.broker.resolve_balanced_split(records)
        sot_panes = [runner.Pane(id=r["id"], name=r["name"], role=r["role"],
                                 focused=r["focused"], x=r["x"], y=r["y"],
                                 width=r["width"], height=r["height"]) for r in records]
        sot_choice = runner.choose_split(sot_panes)
        if (broker_choice is None) != (sot_choice is None):
            f.append("broker と SoT choose_split の None 一致せず")
        elif broker_choice is not None and (
            broker_choice.target_id != sot_choice.target_id
            or broker_choice.direction != sot_choice.direction
        ):
            f.append(f"broker {broker_choice} != SoT {sot_choice}")
    finally:
        cc.teardown()

    # capacity 検出: 全 pane が MIN_PANE 下限割れ → choose_split None → split_capacity_exceeded
    tiny = Cycle()
    try:
        tiny.add_role_pane("dispatcher", "dispatcher", 0, 0, 10, 4)  # < MIN_PANE 20/5
        res = tiny.broker.spawn_agent("w", "w", "worker", ["claude"])
        if res.get("ok") or "[split_capacity_exceeded]" not in res.get("error", ""):
            f.append(f"候補空で split_capacity_exceeded にならない: {res}")
    finally:
        tiny.teardown()

    go = not f
    detail = ("balanced split は claude_org_runtime.choose_split 再利用で現行同等 "
              "(geometry 正規化後の判定が SoT と一致)。候補空で split_capacity_exceeded"
              if go else "; ".join(f))
    return go, detail


def check_cycle(c: Cycle) -> tuple[bool, str]:
    """AC-4-cycle: delegate→spawn→監視→完了報告→CLOSE_PANE→retro の 1 サイクル完走。"""
    f = []
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)

    # baseline poll (3 分 cadence の監視開始点)
    base = c.broker.poll_events(since=None, timeout_ms=0)
    cursor = base["next_since"]

    # (1) delegate: secretary → dispatcher
    r = c.broker.enqueue(c.bind("secretary"), "dispatcher",
                         "DELEGATE: phase4-pane-monitoring を派遣してください")
    if not r.get("ok"):
        f.append(f"delegate 送信失敗: {r}")

    # (2) spawn: dispatcher が balanced split で worker pane を spawn
    sp = c.broker.spawn_agent("worker-phase4", "worker-phase4", "worker", ["claude"])
    if not sp.get("ok"):
        f.append(f"spawn 失敗: {sp}")
    worker_handle = sp.get("handle")
    # 実運用では worker の MCP handshake で registered になる。本ハーネスは合成役割。
    c.broker.register_local(sp["token"])
    c.tokens["worker-phase4"] = sp["token"]  # 後段の bind() 用に追跡

    # spawn が pane_started として観測される
    ev = c.broker.poll_events(since=cursor, timeout_ms=0)
    cursor = ev["next_since"]
    if not any(e["type"] == "pane_started" and e["name"] == "worker-phase4"
               for e in ev["events"]):
        f.append(f"spawn の pane_started 未観測: {ev}")

    # (3) 監視: dispatcher が inspect_pane で worker 画面を独立観測する
    #   (a) 承認待ち観測: worker を input_pending にし、自己申告に依らず scrape で検知
    c.adapter.set_state(sp["pane_id"], "input_pending")
    obs = c.broker.inspect_pane(worker_handle)
    if obs.get("state") != "input_pending":
        f.append(f"承認待ち (input_pending) を独立観測できない: {obs}")
    #   (b) stall 検出: busy が連続観測される (応答生成が長期化 = stall 候補)
    c.adapter.set_state(sp["pane_id"], "busy")
    busy_obs = [c.broker.inspect_pane(worker_handle)["state"] for _ in range(3)]
    if busy_obs != ["busy", "busy", "busy"]:
        f.append(f"stall (連続 busy) を観測できない: {busy_obs}")

    # (4) 完了報告: worker → secretary (busy → idle 静止後にナッジ配達)
    c.adapter.set_state(sp["pane_id"], "idle")
    rep = c.broker.enqueue(c.bind("worker-phase4"), "secretary",
                           "完了報告: ペイン操作 6 面の broker 配線替え一式 commit 済み")
    if not rep.get("ok"):
        f.append(f"完了報告送信失敗: {rep}")
    got = c.broker.drain(c.bind("secretary"))
    if not (len(got) == 1 and got[0]["from_id"] == "worker-phase4"):
        f.append(f"完了報告が token 由来 from で届かない: {got}")

    # (5) CLOSE_PANE: dispatcher が worker を close → token revoke + pane_exited
    wtok = sp["token"]
    closed = c.broker.close_pane_target(worker_handle)
    if not (closed.get("ok") and "worker-phase4" in closed.get("closed", [])):
        f.append(f"close_pane が revoke を誘発しない: {closed}")
    if c.broker.authorize(wtok)[1] != "token_revoked":
        f.append("close 後に token_revoked にならない")
    ev2 = c.broker.poll_events(since=cursor, timeout_ms=0)
    cursor = ev2["next_since"]
    pe = [e for e in ev2["events"] if e["type"] == "pane_exited" and e["name"] == "worker-phase4"]
    if not pe:
        f.append(f"CLOSE_PANE の pane_exited 未観測: {ev2}")

    # (6) retro gate: dispatcher → secretary
    rg = c.broker.enqueue(c.bind("dispatcher"), "secretary",
                          "retro gate: worker クローズ条件を満たしました。retro 起動可否?")
    if not rg.get("ok"):
        f.append(f"retro gate 送信失敗: {rg}")

    go = not f
    detail = ("delegate→spawn(balanced)→監視(inspect_pane で承認待ち/stall を独立観測)→"
              "完了報告(token 由来 from)→CLOSE_PANE(token revoke + pane_exited)→retro gate の"
              "1 サイクルが renga 不使用で完走"
              if go else "; ".join(f))
    return go, detail


def check_cadence(c: Cycle) -> tuple[bool, str]:
    """AC-4-cadence: 反復 reconcile で pane_exited 取りこぼしが次サイクルまでに回復。"""
    f = []
    c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
    c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
    base = c.broker.poll_events(since=None, timeout_ms=0)
    cursor = base["next_since"]
    sp = c.broker.spawn_agent("worker-crash", "worker-crash", "worker", ["claude"])
    c.broker.register_local(sp["token"])
    # cadence 1: pane_started を消化
    ev = c.broker.poll_events(since=cursor, timeout_ms=0)
    cursor = ev["next_since"]
    # worker が「クラッシュ」(broker 非経由・イベント直接喪失を模す)
    c.adapter.kill_pane(sp["pane_id"])
    # cadence 2 (次の 3 分 poll に相当): list_panes reconcile で pane_exited 回復
    ev2 = c.broker.poll_events(since=cursor, timeout_ms=0)
    if not any(e["type"] == "pane_exited" and e["name"] == "worker-crash"
               for e in ev2["events"]):
        f.append(f"クラッシュの pane_exited が次 cadence で回復しない: {ev2}")
    # token も pane_exited 経路で revoke される (reap_exited_panes)
    reaped = c.broker.reap_exited_panes()
    if "worker-crash" not in reaped and c.broker.authorize(sp["token"])[1] != "token_revoked":
        f.append("クラッシュ pane の token が revoke されない")
    go = not f
    detail = ("pane_exited 取りこぼし (クラッシュ) が次 cadence の list_panes reconcile で回復し、"
              "reap で token も revoke (監視ループの正しさを損なわない)"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# 実 tmux smoke (窓口/人間判断で承認: SoT §7.4 を tmux に読み替え)
# ---------------------------------------------------------------------------


def real_tmux_smoke() -> tuple[bool, str]:
    """実 tmux で 6 面 (cat プロセス・無課金) を往復実証する。tmux 不在は skip。"""
    import shutil
    if shutil.which("tmux") is None:
        return True, "SKIP (tmux 不在: 実機 smoke 省略、FakeAdapter で代替検証済み)"
    from tmux_adapter import TmuxAdapter
    adapter = TmuxAdapter(width=240, height=50)
    broker = Broker(state_dir=OUT / "smoke", adapter=adapter, event_poll_interval=0.05)
    f = []
    base_ref = None
    try:
        # dispatcher 相当の base pane を spawn (cat) し、role 付きで bind する
        base_ref = adapter.spawn(["cat"])
        dtok = broker.issue_token("dispatcher", "dispatcher", "dispatcher",
                                  pane_id=base_ref.pane_id)
        broker.register_local(dtok)
        time.sleep(0.3)
        # list_panes が geometry を返す
        panes = broker.mcp_list_panes()
        if not panes or not all(k in panes[0] for k in ("x", "y", "width", "height")):
            f.append(f"list_panes geometry 不足: {panes}")
        # baseline poll
        cursor = broker.poll_events(since=None, timeout_ms=0)["next_since"]
        # balanced split で worker pane を spawn (choose_split → adapter.split)。
        # cat は --mcp-config を消費できないプローブのため inject_mcp_config=False
        # (token→worker の config 注入経路は Phase 1/2 AC-2 で実証済み)。
        sp = broker.spawn_agent("worker-smoke", "worker-smoke", "worker", ["cat"],
                                inject_mcp_config=False)
        if not sp.get("ok"):
            # capacity 等で None の場合は明示 target で split を実証する
            sp = broker.spawn_agent("worker-smoke", "worker-smoke", "worker", ["cat"],
                                    target=broker.mcp_list_panes()[0]["id"],
                                    inject_mcp_config=False)
        if not sp.get("ok"):
            f.append(f"実 tmux split spawn 失敗: {sp}")
        else:
            broker.register_local(sp["token"])
            time.sleep(0.3)
            wh = sp["handle"]
            # pane_started 観測
            ev = broker.poll_events(since=cursor, timeout_ms=0)
            cursor = ev["next_since"]
            if not any(e["type"] == "pane_started" for e in ev["events"]):
                f.append(f"実 tmux pane_started 未観測: {ev}")
            # send_keys (literal + enter) → inspect で反映確認
            broker.send_keys_op(wh, text="hello-phase4-smoke", enter=True)
            time.sleep(0.3)
            insp = broker.inspect_pane(wh)
            if "hello-phase4-smoke" not in insp.get("text", ""):
                f.append(f"実 tmux send_keys/inspect 往復不成立: {insp.get('text','')!r}")
            # close → pane_exited reconcile + token revoke
            broker.close_pane_target(wh)
            time.sleep(0.3)
            ev2 = broker.poll_events(since=cursor, timeout_ms=0)
            if not any(e["type"] == "pane_exited" for e in ev2["events"]):
                f.append(f"実 tmux pane_exited 未観測: {ev2}")
            if broker.authorize(sp["token"])[1] != "token_revoked":
                f.append("実 tmux close 後に token_revoked にならない")
    except Exception as e:  # pragma: no cover
        f.append(f"実 tmux smoke 例外: {e}")
    finally:
        try:
            adapter.kill_server()
        except Exception:
            pass
        broker.stop()
    go = not f
    detail = ("実 tmux で spawn/split/list_panes(geometry)/send_keys/inspect/poll_events"
              "(pane_started+pane_exited)/close を cat プロセスで往復実証 (無課金)"
              if go else "; ".join(f))
    return go, detail


# ---------------------------------------------------------------------------
# ランナー
# ---------------------------------------------------------------------------

CHECKS = [
    ("AC-4-surface", check_surface),
    ("AC-4-events", check_events),
    ("AC-4-split", check_split),
    ("AC-4-cycle", check_cycle),
    ("AC-4-cadence", check_cadence),
]


def run(real_tmux: bool = True) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    qpath = OUT / "state" / "queue.jsonl"
    if qpath.exists():
        qpath.unlink()
    results: dict[str, dict] = {}
    for name, fn in CHECKS:
        c = Cycle()
        try:
            go, detail = fn(c)
        finally:
            c.teardown()
        results[name] = {"go": go, "detail": detail}
        log(f"{'GO   ' if go else 'NO-GO'} {name}: {detail}")
    if real_tmux:
        go, detail = real_tmux_smoke()
        results["AC-4-real-tmux"] = {"go": go, "detail": detail}
        log(f"{'GO   ' if go else 'NO-GO'} AC-4-real-tmux: {detail}")
    return results


def main() -> int:
    real = "--no-real-tmux" not in sys.argv
    results = run(real_tmux=real)
    go = bool(results) and all(r["go"] for r in results.values())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(
        json.dumps(
            {
                "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": "B (broker queue 統合ハーネス / FakeAdapter) + 実 tmux smoke",
                "results": results,
                "go": go,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print("AC-4 統合検証: " + ("GO (全項目 green)" if go else "NO-GO"))
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())
