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
                res = c.send(frm, to, msg)
                self.assertTrue(res.get("ok"), f"{label}: {res}")
                self.assertTrue(c.wait_nudge(to), f"{label}: nudge 未配達")
                got = c.drain(to)
                self.assertEqual(len(got), 1, f"{label}: 配達数")
                self.assertEqual(got[0]["from_id"], frm, f"{label}: from 帰属")
                self.assertEqual(got[0]["message"], msg, f"{label}: 本文")
                self.assertEqual(c.drain(to), [], f"{label}: at-most-once")
        finally:
            c.teardown()


if __name__ == "__main__":
    unittest.main()
