# -*- coding: utf-8 -*-
"""Phase 3 (messaging 移行) の broker ライフサイクル・帰属・1 委譲サイクルの単体検証。

CI は `python -m unittest discover -s tests` で本ファイルを拾う (.github/workflows/tests.yml)。
spike/ は importable パッケージではないため sys.path に追加して broker / run_ac3 を読む。
実 Claude / tmux / wezterm は不要 (FakeAdapter で決定的に駆動、無課金・CI 可)。

設計 SoT: docs/design/renga-decoupling.md §4.4 (token ライフサイクル) / §7.3 (Phase 3 完了基準)。
人手の GO/NO-GO ランナーは spike/run_ac3.py。本ファイルはその機械判定を CI に常設化する。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

_SPIKE = Path(__file__).resolve().parents[1] / "spike"
sys.path.insert(0, str(_SPIKE))

from broker import Broker  # noqa: E402
import run_ac3  # noqa: E402
from run_ac3 import Cycle, FakeAdapter  # noqa: E402


def _broker(tmp: Path, **kw) -> Broker:
    return Broker(state_dir=tmp, adapter=FakeAdapter(), **kw)


class TokenLifecycleTest(unittest.TestCase):
    """設計書 §4.4: 発行 / bind / revoke / TTL / suspend-resume。"""

    def setUp(self) -> None:
        self._tmp = Path(self.id().replace(".", "_"))
        self.broker = _broker(_SPIKE / "broker-state" / "ut" / self._tmp.name)

    def tearDown(self) -> None:
        # 本 broker は HTTP を start していないので stop 不要 (queue store のみ使用)。
        pass

    def test_issue_then_authorize_ok(self) -> None:
        tok = self.broker.issue_token("a", "a", "worker")
        bind, err = self.broker.authorize(tok)
        self.assertIsNotNone(bind)
        self.assertIsNone(err)

    def test_unknown_token_is_invalid(self) -> None:
        bind, err = self.broker.authorize("nope")
        self.assertIsNone(bind)
        self.assertEqual(err, "token_invalid")

    def test_revoke_is_idempotent_and_blocks(self) -> None:
        tok = self.broker.issue_token("a", "a", "worker")
        self.assertTrue(self.broker.revoke_token(tok, reason="test"))
        self.assertFalse(self.broker.revoke_token(tok))  # 二度目は False
        self.assertEqual(self.broker.authorize(tok)[1], "token_revoked")

    def test_ttl_expiry(self) -> None:
        tok = self.broker.issue_token("a", "a", "worker", ttl=0.05)
        self.assertIsNone(self.broker.authorize(tok)[1])
        time.sleep(0.08)
        self.assertEqual(self.broker.authorize(tok)[1], "token_expired")

    def test_pane_exited_revoke(self) -> None:
        adapter: FakeAdapter = self.broker.adapter  # type: ignore[assignment]
        adapter.add_pane("%1", state="idle")
        tok = self.broker.issue_token("w", "w", "worker", pane_id="%1")
        self.broker.register_local(tok)
        adapter.kill_pane("%1")
        reaped = self.broker.reap_exited_panes()
        self.assertIn("w", reaped)
        self.assertEqual(self.broker.authorize(tok)[1], "token_revoked")

    def test_reap_ignores_live_and_adapterless(self) -> None:
        adapter: FakeAdapter = self.broker.adapter  # type: ignore[assignment]
        adapter.add_pane("%live", state="idle")
        tok = self.broker.issue_token("w", "w", "worker", pane_id="%live")
        self.broker.register_local(tok)
        self.assertEqual(self.broker.reap_exited_panes(), [])  # 生存 pane は revoke しない
        self.assertIsNone(self.broker.authorize(tok)[1])

    def test_close_pane_revokes(self) -> None:
        adapter: FakeAdapter = self.broker.adapter  # type: ignore[assignment]
        adapter.add_pane("%2", state="idle")
        tok = self.broker.issue_token("d", "d", "dispatcher", pane_id="%2")
        self.broker.register_local(tok)
        closed = self.broker.close_pane("%2")
        self.assertIn("d", closed)
        self.assertFalse(adapter.pane_exists("%2"))
        self.assertEqual(self.broker.authorize(tok)[1], "token_revoked")

    def test_close_pane_no_revoke_on_kill_exception(self) -> None:
        # kill が例外 → live pane の可能性 → revoke しない (codex round 2 Minor)
        class RaisingAdapter(FakeAdapter):
            def kill_pane(self, pane_id):  # noqa: ANN001
                raise RuntimeError("kill failed")
        broker = Broker(state_dir=_SPIKE / "broker-state" / "ut" / "killraise",
                        adapter=RaisingAdapter())
        broker.adapter.add_pane("%x", state="idle")  # type: ignore[attr-defined]
        tok = broker.issue_token("w", "w", "worker", pane_id="%x")
        broker.register_local(tok)
        self.assertEqual(broker.close_pane("%x"), [])
        self.assertIsNone(broker.authorize(tok)[1])  # 失効していない

    def test_close_pane_no_revoke_when_pane_survives(self) -> None:
        # kill が例外を出さずとも pane が残存 → revoke しない (check=False 相当)
        class NoopKillAdapter(FakeAdapter):
            def kill_pane(self, pane_id):  # noqa: ANN001
                pass  # pane を消さない
        broker = Broker(state_dir=_SPIKE / "broker-state" / "ut" / "killnoop",
                        adapter=NoopKillAdapter())
        broker.adapter.add_pane("%x", state="idle")  # type: ignore[attr-defined]
        tok = broker.issue_token("w", "w", "worker", pane_id="%x")
        broker.register_local(tok)
        self.assertEqual(broker.close_pane("%x"), [])
        self.assertIsNone(broker.authorize(tok)[1])

    def test_ttl_expiry_does_not_inherit_queue(self) -> None:
        # TTL 失効は revoke_token を経ないため、再発行で旧キューを継承しないこと
        # を別途検証する (codex round 2 Major-B)
        sender = self.broker.issue_token("snd", "snd", "worker")
        self.broker.register_local(sender)
        b = self.broker.issue_token("b", "b", "worker", ttl=0.05)
        self.broker.register_local(b)
        self.broker.enqueue(self.broker.get_bind(sender), "b", "stale")
        time.sleep(0.08)  # b が TTL 失効
        self.assertEqual(self.broker.authorize(b)[1], "token_expired")
        new_b = self.broker.issue_token("b", "b", "worker")  # resume 再発行
        self.broker.register_local(new_b)
        self.assertEqual(self.broker.drain(self.broker.get_bind(new_b)), [])

    def test_suspend_resume_reissue(self) -> None:
        old = self.broker.issue_token("s", "s", "secretary")
        self.broker.register_local(old)
        self.assertGreaterEqual(self.broker.suspend(), 1)
        self.assertEqual(self.broker.authorize(old)[1], "token_revoked")
        new = self.broker.issue_token("s", "s", "secretary")  # resume = 再発行
        self.assertNotEqual(new, old)
        self.assertIsNone(self.broker.authorize(new)[1])
        self.assertEqual(self.broker.authorize(old)[1], "token_revoked")  # 旧 token は死んだまま


class AttributionTest(unittest.TestCase):
    """設計書 §4.4 / §7.3: from は token 由来固定、なりすまし構造的不可。"""

    def setUp(self) -> None:
        self.broker = _broker(_SPIKE / "broker-state" / "ut" / "attr")
        self.a = self.broker.issue_token("agent-a", "agent-a", "worker")
        self.b = self.broker.issue_token("agent-b", "agent-b", "worker")
        for t in (self.a, self.b):
            self.broker.register_local(t)

    def test_from_is_token_derived(self) -> None:
        self.broker.enqueue(self.broker.get_bind(self.a), "agent-b", "hi")
        msgs = self.broker.drain(self.broker.get_bind(self.b))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["from_id"], "agent-a")
        self.assertEqual(msgs[0]["from_name"], "agent-a")

    def test_spoofed_from_fields_ignored(self) -> None:
        # call_tool に from_id/from_name を仕込んでも broker は token 由来で上書き
        self.broker.call_tool(
            self.broker.get_bind(self.a), "send_message",
            {"to_id": "agent-b", "message": "x", "from_id": "agent-b",
             "from_name": "agent-b"},
        )
        msgs = self.broker.drain(self.broker.get_bind(self.b))
        self.assertEqual(msgs[0]["from_id"], "agent-a")  # 偽装は通らない

    def test_enqueue_signature_takes_no_self_reported_from(self) -> None:
        import inspect
        params = inspect.signature(self.broker.enqueue).parameters
        self.assertNotIn("from_id", params)
        self.assertNotIn("from_name", params)

    def test_revoked_sender_cannot_send(self) -> None:
        bind = self.broker._binds[self.a]
        self.broker.revoke_token(self.a, reason="test")
        res = self.broker.enqueue(bind, "agent-b", "zombie")
        self.assertFalse(res.get("ok"))
        self.assertIn("token_revoked", res.get("error", ""))

    def test_call_tool_rejects_stale_bind(self) -> None:
        # revoke 前に取得した bind を直呼びしても check_messages / set_summary が
        # 素通りしないこと (authorize を単一権限判定点にする。codex Major 対応)
        bind = self.broker._binds[self.a]
        self.broker.revoke_token(self.a, reason="test")
        for tool in ("check_messages", "set_summary", "list_peers"):
            res = self.broker.call_tool(bind, tool, {"summary": "x"})
            self.assertTrue(res.get("isError"), f"{tool} が失効 bind で素通りした")
            self.assertIn("token_revoked", res["content"][0]["text"])

    def test_reissue_clears_stale_nudge_thread(self) -> None:
        # revoke→同 agent_id 再発行→enqueue が、旧 nudge thread 生存中でも新規
        # nudge を起動できること (codex round 3 Major)。旧 thread を模した「生存中の
        # ダミー thread」を _nudge_threads に差し込んで race を決定的に再現する。
        import threading
        broker = Broker(
            state_dir=_SPIKE / "broker-state" / "ut" / "nudgethread",
            adapter=FakeAdapter(), nudge_defer_interval=0.01,
        )
        adapter: FakeAdapter = broker.adapter  # type: ignore[assignment]
        adapter.add_pane("%b", state="idle")
        snd = broker.issue_token("snd", "snd", "worker")
        broker.register_local(snd)
        b1 = broker.issue_token("b", "b", "worker", pane_id="%b")
        broker.register_local(b1)
        stale = threading.Thread(target=lambda: time.sleep(0.5), daemon=True)
        stale.start()
        broker._nudge_threads["b"] = (stale, b1)     # 旧ライフサイクルの生存 thread を模す
        broker.revoke_token(b1, reason="suspend")
        b2 = broker.issue_token("b", "b", "worker", pane_id="%b")  # 再発行
        broker.register_local(b2)
        res = broker.enqueue(broker.get_bind(snd), "b", "再発行後の新着")
        self.assertTrue(res.get("ok"), res)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not adapter.nudges_for("%b"):
            time.sleep(0.01)
        self.assertEqual(adapter.nudges_for("%b"), [run_ac3.NUDGE_TEXT])
        stale.join(timeout=1.0)

    def test_dedup_ignores_dying_worker_of_revoked_token(self) -> None:
        # 同一 agent_id に別の有効 token が残るケース: 旧 token 向け worker が生存中でも
        # その token が失効していれば dedup を信用せず新 worker を起動する
        # (codex round 4 Major)。
        import threading
        broker = Broker(
            state_dir=_SPIKE / "broker-state" / "ut" / "dyingworker",
            adapter=FakeAdapter(), nudge_defer_interval=0.01,
        )
        adapter: FakeAdapter = broker.adapter  # type: ignore[assignment]
        adapter.add_pane("%b1", state="idle")
        adapter.add_pane("%b2", state="idle")
        snd = broker.issue_token("snd", "snd", "worker")
        broker.register_local(snd)
        b1 = broker.issue_token("b", "b", "worker", pane_id="%b1")
        broker.register_local(b1)
        b2 = broker.issue_token("b", "b", "worker", pane_id="%b2")  # had_active=True
        broker.register_local(b2)
        # 旧 token b1 向けの生存 worker を模す + b1 を revoke (dying worker 化)
        stale = threading.Thread(target=lambda: time.sleep(0.5), daemon=True)
        stale.start()
        broker._nudge_threads["b"] = (stale, b1)
        broker.revoke_token(b1, reason="rotate")
        # enqueue は有効な b2 (pane %b2) へ配送される
        res = broker.enqueue(broker.get_bind(snd), "b", "新 token 宛")
        self.assertTrue(res.get("ok"), res)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not adapter.nudges_for("%b2"):
            time.sleep(0.01)
        self.assertEqual(adapter.nudges_for("%b2"), [run_ac3.NUDGE_TEXT])
        stale.join(timeout=1.0)

    def test_reissue_does_not_inherit_stale_queue(self) -> None:
        # agent-b 宛に未読を積んでから revoke → 同 agent_id で再発行した token が
        # 旧ライフサイクルの未読を読めないこと (codex Major 対応)
        self.broker.enqueue(self.broker.get_bind(self.a), "agent-b", "stale")
        self.broker.revoke_token(self.b, reason="suspend")
        new_b = self.broker.issue_token("agent-b", "agent-b", "worker")
        self.broker.register_local(new_b)
        self.assertEqual(self.broker.drain(self.broker.get_bind(new_b)), [])


class DelegationCycleTest(unittest.TestCase):
    """設計書 §7.3: 6 経路全数往復 + ナッジ defer の統合 GO を CI に常設化する。"""

    def test_all_ac3_checks_go(self) -> None:
        results = run_ac3.run()
        self.assertTrue(results, "no AC-3 results produced")
        for name, r in results.items():
            self.assertTrue(r["go"], f"{name} NO-GO: {r['detail']}")

    def test_six_paths_roundtrip_with_correct_attribution(self) -> None:
        c = Cycle()
        c.setup()
        try:
            for label, frm, to, msg in run_ac3.DELEGATION_PATHS:
                base = c.nudge_count(to)  # 送信前件数 (再発火の増加分を待つ)
                res = c.send(frm, to, msg)
                self.assertTrue(res.get("ok"), f"{label}: {res}")
                self.assertTrue(c.wait_nudge(to, baseline=base), f"{label}: nudge 未配達/再発火せず")
                got = c.drain(to)
                self.assertEqual(len(got), 1, f"{label}: 配達数")
                self.assertEqual(got[0]["from_id"], frm, f"{label}: from 帰属")
                self.assertEqual(got[0]["message"], msg, f"{label}: 本文")
                self.assertEqual(c.drain(to), [], f"{label}: at-most-once")
        finally:
            c.teardown()


if __name__ == "__main__":
    unittest.main()
