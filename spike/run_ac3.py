# -*- coding: utf-8 -*-
"""AC-3 統合検証: メッセージング移行の 1 委譲サイクル完走 (設計書 §7.3)。

検証方式 B (窓口経由のユーザー判断 2026-06-09): 実 Claude 4 ペインの課金実証や
本体取り込みスコープの prose 書き換えは行わず、broker queue レベルの統合ハーネスで
4 役割 token を bind して 6 経路を全数往復させ、機械判定する。実セッション往復の
リアルさ (PTY ナッジ打鍵・起動チェーン・文字化けなし) は Phase 1/2 の AC-1 / AC-2 が
実 Claude TUI で既証 (spike/RESULTS.md)。本ハーネスは「broker 側が full cycle を
構造的に支えられること」を無課金・決定的・CI 可で実証する。

検証項目:
  AC-3-cycle:      6 経路 (DELEGATE / ack / 完了報告 / 判断仰ぎ / CURATE_* / retro gate)
                   が 4 役割 token bind で全数往復し、各 from が token 由来で正しい。
  AC-3-nudge:      静止確認 defer が busy / input_pending と共存し、idle 復帰後に配達
                   (defer-then-deliver)。idle 宛は即時配達。
  AC-3-spoof:      なりすまし送信が構造的に不可能 (from は token 固定、自己申告フィールド
                   を broker が一切採らない)。
  AC-3-lifecycle:  token ライフサイクル — pane_exited revoke / close revoke / TTL 失効 /
                   suspend-resume 再発行。

FakeAdapter で受信側ペインの画面状態 (idle/busy/input_pending) と pane 生死を決定的に
駆動する。classify_pane_state は実 Claude TUI と同一描画を前提に Phase 1/2 で実測較正済み
であり、本ハーネスはその描画を再現した固定スクリーンで状態遷移を駆動する。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from broker import Broker  # noqa: E402
from terminal_adapter import NUDGE_TEXT, PaneRef  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

OUT = Path(__file__).parent / "broker-state" / "ac3"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# FakeAdapter: 受信側ペインの画面状態と pane 生死を決定的に駆動する
# ---------------------------------------------------------------------------

# 実 Claude TUI (claude 2.1.168) の描画を再現した固定スクリーン。
# classify_pane_state はこの描画から idle/busy/input_pending を判定する
# (Phase 1/2 で実測較正済みのヒューリスティックを、ここでは固定入力で駆動する)。
_SCREENS = {
    "idle": "──────────────────────────\n❯ \n──────────────────────────\n",
    "input_pending": (
        "──────────────────────────\n❯ 未送信の長文テキストが入力欄にあります\n"
        "──────────────────────────\n"
    ),
    "busy": "応答を生成中…\n  (esc to interrupt)\n",
}


class FakeAdapter:
    """TerminalAdapter Protocol の検証用実装 (broker が使う面のみ実装)。

    broker のナッジ機構が触るのは get_text / send_line、ライフサイクルが触るのは
    pane_exists / kill_pane。残りの Protocol メソッドは本ハーネスでは未使用だが、
    構造的型を満たすためスタブを置く。
    """

    def __init__(self) -> None:
        self._panes: dict[object, dict] = {}

    # -- 検証ドライバ ------------------------------------------------------
    def add_pane(self, pane_id: object, state: str = "idle") -> None:
        self._panes[pane_id] = {
            "state": state, "alive": True, "nudges": [],
            "polls": 0, "flip_after": None, "flip_to": None,
        }

    def set_state(self, pane_id: object, state: str) -> None:
        self._panes[pane_id]["state"] = state

    def schedule_flip(self, pane_id: object, after_polls: int, to_state: str) -> None:
        """get_text が after_polls 回呼ばれた後に to_state へ遷移させる
        (busy/input_pending → idle の「静止」を決定的に再現する)。"""
        p = self._panes[pane_id]
        p["flip_after"] = after_polls
        p["flip_to"] = to_state

    def nudges_for(self, pane_id: object) -> list[str]:
        return list(self._panes[pane_id]["nudges"])

    # -- broker が使う面 ---------------------------------------------------
    def get_text(self, pane_id: object, escapes: bool = False) -> str:
        p = self._panes[pane_id]
        p["polls"] += 1
        if p["flip_after"] is not None and p["polls"] > p["flip_after"]:
            p["state"] = p["flip_to"]
        return _SCREENS[p["state"]]

    def send_line(self, pane_id: object, text: str, settle: float = 0.0) -> None:
        self._panes[pane_id]["nudges"].append(text)

    def pane_exists(self, pane_id: object) -> bool:
        return self._panes.get(pane_id, {}).get("alive", False)

    def kill_pane(self, pane_id: object) -> None:
        if pane_id in self._panes:
            self._panes[pane_id]["alive"] = False

    # -- Protocol スタブ (本ハーネス未使用) --------------------------------
    def spawn(self, argv, cwd=None, new_window=True) -> PaneRef:  # pragma: no cover
        raise NotImplementedError("FakeAdapter does not spawn")

    def list_panes(self) -> list[dict]:  # pragma: no cover
        return [{"pane_id": pid, "alive": p["alive"]} for pid, p in self._panes.items()]

    def type_text(self, pane_id, text) -> None:  # pragma: no cover
        pass

    def send_enter(self, pane_id) -> None:  # pragma: no cover
        pass

    def send_interrupt(self, pane_id) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# 役割 (4 token) と 6 経路の定義
# ---------------------------------------------------------------------------

ROLES = [
    ("secretary", "secretary"),
    ("dispatcher", "dispatcher"),
    ("curator", "curator"),
    ("worker-phase3", "worker"),
]

# (label, from_agent, to_agent, message) — 設計書 §3.1 の呼出主体マトリクスに対応。
DELEGATION_PATHS = [
    ("DELEGATE",   "secretary",     "dispatcher",    "DELEGATE: phase3-messaging-broker を派遣してください"),
    ("ack",        "secretary",     "worker-phase3", "ack: 完了報告を受領しました。PR 承認待ちに入ってください"),
    ("完了報告",     "worker-phase3", "secretary",     "完了報告: broker 配線替え一式 commit 済み。検証 green"),
    ("判断仰ぎ",     "worker-phase3", "secretary",     "判断仰ぎ: スコープ拡張提案あり。続行可否を仰ぎます"),
    ("CURATE_DONE", "curator",       "dispatcher",    "CURATE_DONE: 生の学び 3 件を統合しました"),
    ("retro_gate",  "dispatcher",    "secretary",     "retro gate: worker クローズ条件を満たしました。retro 起動可否?"),
]


class Cycle:
    """broker + FakeAdapter + 4 役割 token の結線 (1 委譲サイクル)。"""

    def __init__(self, ttl: float | None = None) -> None:
        self.adapter = FakeAdapter()
        self.broker = Broker(
            state_dir=OUT / "state",
            adapter=self.adapter,
            # 決定的検証のためナッジ defer 間隔を詰める (実運用は 2.0s)。
            nudge_defer_interval=0.01,
            nudge_defer_max_tries=200,
            default_token_ttl=ttl,
        )
        self.tokens: dict[str, str] = {}
        self.panes: dict[str, object] = {}

    def setup(self) -> None:
        """4 役割を spawn 済み・registered 済み相当にする。

        実運用では各役割の Claude が initialize handshake で registered になる
        (AC-2-3)。本ハーネスは MCP を経由しない合成役割なので、token 発行 + pane
        bind + register を直接行う (harness.register_local と同型)。
        """
        for i, (agent_id, role) in enumerate(ROLES):
            pane_id = f"%{i + 100}"  # tmux 風の不透明 pane id
            self.adapter.add_pane(pane_id, state="idle")
            tok = self.broker.issue_token(agent_id, agent_id, role, pane_id=pane_id)
            self.broker.register_local(tok)
            self.tokens[agent_id] = tok
            self.panes[agent_id] = pane_id

    def bind(self, agent_id: str):
        b = self.broker.get_bind(self.tokens[agent_id])
        assert b is not None, f"no active bind for {agent_id}"
        return b

    def send(self, from_agent: str, to_agent: str, message: str) -> dict:
        """from_agent の token bind で送信 (from 帰属は broker が token から付与)。"""
        return self.broker.enqueue(self.bind(from_agent), to_agent, message)

    def wait_nudge(self, agent_id: str, timeout: float = 5.0) -> bool:
        """ナッジ配達 (send_line 打鍵) が宛先ペインに届くまで待つ。"""
        pane = self.panes[agent_id]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.adapter.nudges_for(pane):
                return True
            time.sleep(0.01)
        return False

    def drain(self, agent_id: str) -> list[dict]:
        return self.broker.drain(self.bind(agent_id))

    def teardown(self) -> None:
        self.broker.stop()


# ---------------------------------------------------------------------------
# 検証本体 (各 check_* は (go: bool, detail: str) を返す)
# ---------------------------------------------------------------------------

def check_cycle(c: Cycle) -> tuple[bool, str]:
    """AC-3-cycle: 6 経路全数往復 + token 由来 from 帰属。"""
    failures = []
    for label, frm, to, msg in DELEGATION_PATHS:
        res = c.send(frm, to, msg)
        if not res.get("ok"):
            failures.append(f"{label}: enqueue 失敗 {res}")
            continue
        if not c.wait_nudge(to):
            failures.append(f"{label}: ナッジ未配達 ({to})")
        got = c.drain(to)
        if len(got) != 1:
            failures.append(f"{label}: 配達数 {len(got)} != 1")
            continue
        m = got[0]
        if m["from_id"] != frm:
            failures.append(f"{label}: from 帰属不正 {m['from_id']} != {frm}")
        if m["message"] != msg:
            failures.append(f"{label}: 本文破損")
        # at-most-once: 2 回目は空
        if c.drain(to):
            failures.append(f"{label}: at-most-once 違反 (2 回目に再配達)")
    go = not failures
    detail = (
        f"6 経路全数 GO (DELEGATE/ack/完了報告/判断仰ぎ/CURATE_DONE/retro gate)。"
        f"各 from は token 由来で正しく、ナッジ配達 + at-most-once drain 成立"
        if go else "; ".join(failures)
    )
    return go, detail


def check_nudge_defer(c: Cycle) -> tuple[bool, str]:
    """AC-3-nudge: busy / input_pending では defer、静止後に配達 (defer-then-deliver)。"""
    notes = []
    failures = []
    for state in ("busy", "input_pending"):
        # 専用の受信ペイン (registered worker) を 1 本用意する
        pane = f"%defer-{state}"
        c.adapter.add_pane(pane, state=state)
        agent = f"defer-{state}"
        tok = c.broker.issue_token(agent, agent, "worker", pane_id=pane)
        c.broker.register_local(tok)
        # 3 回 poll の間は state 維持、その後 idle へ遷移 (静止) させる
        c.adapter.schedule_flip(pane, after_polls=3, to_state="idle")

        n0 = len(_journal(c))
        c.broker.enqueue(c.bind("secretary"), agent, f"{state} 中の宛先へ割り込み")
        # nudge_sent の journal 書込みまで待つ (send_line 記録は journal 追記より
        # 先に起きるため、adapter の nudge だけ待つと nudge_sent を取りこぼす race)。
        _wait_event(c, n0, "nudge_sent")
        ev = [e for e in _journal(c)[n0:] if e["event"].startswith("nudge")]
        deferred = [e for e in ev if e["event"] == "nudge_deferred"]
        sent = [e for e in ev if e["event"] == "nudge_sent"]
        nud = c.adapter.nudges_for(pane)
        # 合格条件: 静止前に defer が 1 回以上記録され、静止後に nudge_sent が
        # ちょうど 1 回、打鍵された定型行が NUDGE_TEXT であること (本文は PTY 非経由)。
        # 早漏配達は構造的に起きない: _nudge_worker は classify==idle のときのみ
        # send_line する (busy/input_pending では defer)。よって sent==1 はすべて
        # 静止後の配達であり、deferred>=1 がその前段の defer を実証する。
        if not (len(deferred) >= 1 and len(sent) == 1 and nud == [NUDGE_TEXT]):
            failures.append(
                f"{state}: deferred={len(deferred)} sent={len(sent)} nudges={nud}"
            )
        else:
            states = {e["state"] for e in deferred}
            notes.append(f"{state}: defer {len(deferred)} 回 (states={states}) → 静止後配達")

    # idle 宛は即時 (defer 0 回) かつ実際にナッジが配達されることを確認
    # (defer が無いことだけでなく nudge_sent + NUDGE_TEXT 打鍵まで検証する。
    #  「idle 宛で nudge が全く送られない」退行も検出する。codex Minor 対応)
    pane_idle = "%defer-idle"
    c.adapter.add_pane(pane_idle, state="idle")
    c.broker.register_local(c.broker.issue_token("defer-idle", "defer-idle", "worker", pane_id=pane_idle))
    n0 = len(_journal(c))
    res = c.broker.enqueue(c.bind("secretary"), "defer-idle", "idle 宛")
    _wait_event(c, n0, "nudge_sent")
    idle_ev = [e for e in _journal(c)[n0:] if e["event"].startswith("nudge")]
    idle_deferred = [e for e in idle_ev if e["event"] == "nudge_deferred"]
    idle_sent = [e for e in idle_ev if e["event"] == "nudge_sent"]
    idle_nud = c.adapter.nudges_for(pane_idle)
    if not res.get("ok"):
        failures.append(f"idle 宛 enqueue 失敗 {res}")
    elif idle_deferred:
        failures.append(f"idle 宛で defer 発生 {len(idle_deferred)} 回 (即時配達のはず)")
    elif len(idle_sent) != 1 or idle_nud != [NUDGE_TEXT]:
        failures.append(f"idle 宛で即時配達されない (sent={len(idle_sent)} nudges={idle_nud})")
    else:
        notes.append("idle 宛: defer 0 回で即時配達 (nudge_sent 1 回・NUDGE_TEXT 打鍵)")

    go = not failures
    return go, ("; ".join(notes) if go else "; ".join(failures))


def check_spoof(c: Cycle) -> tuple[bool, str]:
    """AC-3-spoof: なりすまし送信が構造的に不可能。

    worker が (a) arguments に from_id / from_name を仕込んでも broker は採らず、
    (b) 他 agent になりすました from を付けられない。from は送信者 token に固定。
    """
    failures = []
    # (a) call_tool 経由で from_id/from_name を偽装注入しても無視される
    worker_bind = c.bind("worker-phase3")
    c.broker.call_tool(
        worker_bind, "send_message",
        {"to_id": "secretary", "message": "なりすまし試行",
         "from_id": "secretary", "from_name": "secretary"},  # ← 偽装フィールド
    )
    got = c.drain("secretary")
    if len(got) != 1:
        failures.append(f"配達数 {len(got)}")
    elif got[0]["from_id"] != "worker-phase3":
        failures.append(f"偽装 from が通った: {got[0]['from_id']}")
    elif got[0]["from_name"] != "worker-phase3":
        failures.append(f"偽装 from_name が通った: {got[0]['from_name']}")
    # (b) entry に from を自己申告する API 自体が存在しない (構造的担保)
    #     enqueue の署名は from_bind (token 由来) のみで、文字列 from を受けない。
    import inspect
    sig = inspect.signature(c.broker.enqueue)
    if "from_id" in sig.parameters or "from_name" in sig.parameters:
        failures.append("enqueue が文字列 from を受ける署名になっている (自己申告余地)")
    go = not failures
    detail = (
        "from_id/from_name は token bind 由来固定。call_tool に偽装フィールドを"
        "注入しても無視され、enqueue 署名は自己申告 from を受けない (構造的不可)"
        if go else "; ".join(failures)
    )
    return go, detail


def check_lifecycle(c: Cycle) -> tuple[bool, str]:
    """AC-3-lifecycle: pane_exited revoke / close revoke / TTL 失効 / suspend-resume 再発行。"""
    failures = []

    # (1) pane_exited revoke: worker pane を kill → reap で revoke → 送信不可
    wtok = c.tokens["worker-phase3"]
    c.adapter.kill_pane(c.panes["worker-phase3"])
    reaped = c.broker.reap_exited_panes()
    if "worker-phase3" not in reaped:
        failures.append(f"pane_exited が revoke を誘発しない (reaped={reaped})")
    _, err = c.broker.authorize(wtok)
    if err != "token_revoked":
        failures.append(f"revoke 後 authorize が {err} (token_revoked 期待)")
    # revoke 済み token での送信は拒否される
    dead_bind = c.broker._binds[wtok]
    res = c.broker.enqueue(dead_bind, "secretary", "ゾンビ送信")
    if res.get("ok"):
        failures.append("revoke 済み token で送信できた")
    # revoke 済み worker は配送先 (list_peers/registered) からも消える
    if c.broker.find_registered("worker-phase3") is not None:
        failures.append("revoke 済み worker が registered に残存")

    # (2) close_pane revoke: dispatcher を broker.close_pane で退役
    dtok = c.tokens["dispatcher"]
    closed = c.broker.close_pane(c.panes["dispatcher"])
    if "dispatcher" not in closed:
        failures.append(f"close_pane が revoke を誘発しない (closed={closed})")
    if c.broker.authorize(dtok)[1] != "token_revoked":
        failures.append("close_pane 後に token_revoked にならない")

    # (3) TTL 失効: 短寿命 token を発行 → 経過後 token_expired
    ttok = c.broker.issue_token("ttl-agent", "ttl-agent", "worker", ttl=0.05)
    c.broker.register_local(ttok)
    if c.broker.authorize(ttok)[1] is not None:
        failures.append("TTL 内なのに失効扱い")
    time.sleep(0.08)
    if c.broker.authorize(ttok)[1] != "token_expired":
        failures.append("TTL 超過後に token_expired にならない")

    # (4) suspend-resume 再発行: suspend で全 revoke → resume 再発行は別 token
    old_sec = c.tokens["secretary"]
    n = c.broker.suspend()
    if n < 1:
        failures.append(f"suspend が token を revoke しない (n={n})")
    if c.broker.authorize(old_sec)[1] != "token_revoked":
        failures.append("suspend 後に旧 token が有効なまま")
    # resume: 再 spawn 相当で再発行 (別 token)。旧 token は死んだまま
    new_sec = c.broker.issue_token("secretary", "secretary", "secretary",
                                   pane_id=c.panes["secretary"])
    c.broker.register_local(new_sec)
    if new_sec == old_sec:
        failures.append("再発行 token が旧 token と同一 (再利用)")
    if c.broker.authorize(new_sec)[1] is not None:
        failures.append("再発行 token が有効にならない")
    if c.broker.authorize(old_sec)[1] != "token_revoked":
        failures.append("再発行後に旧 token が復活")

    go = not failures
    detail = (
        "pane_exited / close_pane で即時 revoke、TTL 超過で token_expired、"
        "suspend で全 revoke → resume は別 token を再発行 (旧 token 再利用不可)"
        if go else "; ".join(failures)
    )
    return go, detail


def _journal(c: Cycle) -> list[dict]:
    path = c.broker.state_dir / "queue.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _wait_event(c: Cycle, n0: int, event: str, timeout: float = 5.0) -> bool:
    """journal の n0 以降に指定 event が現れるまで待つ (ナッジスレッドとの同期)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(e["event"] == event for e in _journal(c)[n0:]):
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# ランナー
# ---------------------------------------------------------------------------

CHECKS = [
    ("AC-3-cycle", check_cycle),
    ("AC-3-nudge", check_nudge_defer),
    ("AC-3-spoof", check_spoof),
    ("AC-3-lifecycle", check_lifecycle),
]


def run() -> dict:
    """全 check を実行し results dict を返す (unittest からも呼ぶ)。"""
    OUT.mkdir(parents=True, exist_ok=True)
    # 過去 run の journal 残留を避けるため state を初期化
    qpath = OUT / "state" / "queue.jsonl"
    if qpath.exists():
        qpath.unlink()
    c = Cycle()
    c.setup()
    results: dict[str, dict] = {}
    try:
        for name, fn in CHECKS:
            go, detail = fn(c)
            results[name] = {"go": go, "detail": detail}
            log(f"{'GO   ' if go else 'NO-GO'} {name}: {detail}")
    finally:
        c.teardown()
    return results


def main() -> int:
    results = run()
    go = bool(results) and all(r["go"] for r in results.values())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(
        json.dumps(
            {
                "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "method": "B (broker queue 統合ハーネス / FakeAdapter / 無課金・決定的)",
                "backend": "FakeAdapter",
                "results": results,
                "go": go,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print("AC-3 統合検証: " + ("GO (全項目 green)" if go else "NO-GO"))
    return 0 if go else 1


if __name__ == "__main__":
    sys.exit(main())
