# -*- coding: utf-8 -*-
"""Phase 4 (ペイン操作移行) の broker 6 面 + poll_events 合成 + 権限分離 + 監視サイクルの単体検証。

CI は `python -m unittest discover -s tests` で本ファイルを拾う (.github/workflows/tests.yml)。
spike/ は importable パッケージではないため sys.path に追加して broker / run_ac4 を読む。
**実 tmux smoke は CI から除外** (sandbox で unix socket 不可 / 環境依存)。FakeAdapter の
決定的 5 検証のみを CI に常設化する (実 tmux smoke は spike/run_ac4.py の手動ランナー側で実証済み)。

設計 SoT: docs/design/renga-decoupling.md §4.2 (role-scoped surface) / §4.7 / §7.4 (Phase 4 完了基準)。
balanced split SoT: claude_org_runtime.dispatcher.runner.choose_split (再利用)。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SPIKE = Path(__file__).resolve().parents[1] / "spike"
sys.path.insert(0, str(_SPIKE))

import run_ac4  # noqa: E402
from broker import role_tier, tools_for_role, TIER_OPS, TIER_MESSAGING  # noqa: E402
from run_ac4 import Cycle  # noqa: E402


class Ac4HarnessTest(unittest.TestCase):
    """run_ac4 の 5 検証 (FakeAdapter) を CI 機械判定として常設する。"""

    def _run_check(self, fn):
        c = Cycle()
        try:
            go, detail = fn(c)
        finally:
            c.teardown()
        self.assertTrue(go, detail)

    def test_surface(self) -> None:
        self._run_check(run_ac4.check_surface)

    def test_events(self) -> None:
        self._run_check(run_ac4.check_events)

    def test_split(self) -> None:
        self._run_check(run_ac4.check_split)

    def test_cycle(self) -> None:
        self._run_check(run_ac4.check_cycle)

    def test_cadence(self) -> None:
        self._run_check(run_ac4.check_cadence)


class RoleScopeTest(unittest.TestCase):
    """権限分離 (item 4): role tier → 公開面のフィルタ。"""

    def test_tier_mapping(self) -> None:
        self.assertEqual(role_tier("worker"), TIER_MESSAGING)
        self.assertEqual(role_tier("curator"), TIER_MESSAGING)
        self.assertEqual(role_tier("dispatcher"), TIER_OPS)
        self.assertEqual(role_tier("secretary"), TIER_OPS)
        # 未知 role は fail-safe で最小権限
        self.assertEqual(role_tier("attacker"), TIER_MESSAGING)

    def test_messaging_tier_hides_pane_ops(self) -> None:
        names = {t["name"] for t in tools_for_role("worker")}
        self.assertEqual(
            names, {"send_message", "check_messages", "list_peers", "set_summary"}
        )
        for forbidden in ("list_panes", "spawn_agent", "close_pane", "send_keys",
                          "poll_events", "inspect_pane", "set_pane_identity"):
            self.assertNotIn(forbidden, names)

    def test_ops_tier_exposes_pane_ops(self) -> None:
        names = {t["name"] for t in tools_for_role("dispatcher")}
        for tool in ("list_panes", "spawn_agent", "close_pane", "send_keys",
                     "poll_events", "inspect_pane", "set_pane_identity"):
            self.assertIn(tool, names)


class PollEventsUnitTest(unittest.TestCase):
    """poll_events 合成の境界 (baseline / exactly-once / events_dropped count)。"""

    def setUp(self) -> None:
        self.c = Cycle(event_cap=3)
        self.c.add_role_pane("dispatcher", "dispatcher", 0, 0, 140, 43)

    def tearDown(self) -> None:
        self.c.teardown()

    def test_baseline_no_replay(self) -> None:
        base = self.c.broker.poll_events(since=None, timeout_ms=0)
        self.assertEqual(base["events"], [])
        self.assertIn("next_since", base)

    def test_started_then_exited_emitted_once(self) -> None:
        cur = self.c.broker.poll_events(since=None, timeout_ms=0)["next_since"]
        self.c.adapter.add_pane("p1", width=40, height=10)
        self.c.broker._reconcile()
        ev = self.c.broker.poll_events(since=cur, timeout_ms=0)
        started = [e for e in ev["events"] if e["type"] == "pane_started"]
        self.assertEqual(len(started), 1)
        cur = ev["next_since"]
        # カーソルを進めた後は同じ pane_started を再配信しない (cursor 前進)
        ev2 = self.c.broker.poll_events(since=cur, timeout_ms=0)
        self.assertEqual([e for e in ev2["events"] if e["type"] == "pane_started"], [])
        # kill → pane_exited は **emit ちょうど 1 回** (反復 reconcile でも増えない)
        self.c.adapter.kill_pane("p1")
        for _ in range(3):
            self.c.broker._reconcile()
        ev3 = self.c.broker.poll_events(since=cur, timeout_ms=0)
        exited = [e for e in ev3["events"] if e["type"] == "pane_exited"]
        self.assertEqual(len(exited), 1, "pane_exited は emit 1 回 (反復 reconcile で重複しない)")

    def test_close_response_has_no_native_pane_id(self) -> None:
        # close_pane の MCP 応答も handle のみ (native pane_id 非露出)
        sp = self.c.broker.spawn_agent("worker-z", "worker-z", "worker", ["claude"])
        self.c.broker.register_local(sp["token"])
        resp = self.c.broker.close_pane_target(sp["handle"])
        self.assertTrue(resp.get("ok"))
        self.assertNotIn("pane_id", resp)
        self.assertEqual(resp.get("handle"), sp["handle"])

    def test_event_payload_has_no_native_pane_id(self) -> None:
        # MCP 面は handle (`id`) のみ。native pane_id は payload に露出しない
        cur = self.c.broker.poll_events(since=None, timeout_ms=0)["next_since"]
        self.c.adapter.add_pane("p9", width=40, height=10)
        self.c.broker._reconcile()
        ev = self.c.broker.poll_events(since=cur, timeout_ms=0)
        self.assertTrue(ev["events"])
        for e in ev["events"]:
            self.assertNotIn("pane_id", e)
            self.assertIn("id", e)

    def test_events_dropped_carries_count(self) -> None:
        cur = self.c.broker.poll_events(since=None, timeout_ms=0)["next_since"]
        for i in range(6):
            self.c.adapter.add_pane(f"d{i}", width=40, height=10)
            self.c.broker._reconcile()
            self.c.adapter.kill_pane(f"d{i}")
            self.c.broker._reconcile()
        dropped = self.c.broker.poll_events(since=cur, timeout_ms=0)
        de = [e for e in dropped["events"] if e["type"] == "events_dropped"]
        self.assertEqual(len(de), 1)
        self.assertIsInstance(de[0]["count"], int)
        self.assertGreater(de[0]["count"], 0)

    def test_types_filter_advances_cursor(self) -> None:
        cur = self.c.broker.poll_events(since=None, timeout_ms=0)["next_since"]
        self.c.adapter.add_pane("p2", width=40, height=10)
        self.c.broker._reconcile()
        # pane_exited のみ要求 → pane_started は落ちるが next_since は前進する
        ev = self.c.broker.poll_events(since=cur, timeout_ms=0, types=["pane_exited"])
        self.assertEqual(ev["events"], [])
        self.assertNotEqual(ev["next_since"], cur)


class PrivilegeEscalationTest(unittest.TestCase):
    """set_pane_identity の表示 role 変更が権限 tier を昇格させないこと (codex Blocker)。"""

    def setUp(self) -> None:
        self.c = Cycle()
        self.c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)
        self.c.add_role_pane("worker-x", "worker", 140, 43, 140, 43)
        # handle を採番させる
        self.c.broker.mcp_list_panes()

    def tearDown(self) -> None:
        self.c.teardown()

    def test_role_relabel_does_not_escalate_tier(self) -> None:
        wh = self.c.handle_of("worker-x")
        # ops (dispatcher) が worker pane に role="dispatcher" を付け替える
        r = self.c.broker.set_pane_identity(wh, role="dispatcher")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["role"], "dispatcher")  # 表示ラベルは変わる
        # しかし worker token の権限 tier (auth_role) は不変 → 依然 pane 操作不可
        wbind = self.c.broker.get_bind(self.c.tokens["worker-x"])
        self.assertEqual(wbind.auth_role, "worker")
        forbidden = self.c.broker.call_tool(wbind, "list_panes", {})
        self.assertTrue(forbidden.get("isError"))
        self.assertIn("[tool_forbidden]", forbidden["content"][0]["text"])


class PaneExitRevokeTest(unittest.TestCase):
    """pane_exited 合成時に token を即時 revoke すること (codex Major / §4.4)。"""

    def setUp(self) -> None:
        self.c = Cycle()
        self.c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)

    def tearDown(self) -> None:
        self.c.teardown()

    def test_crash_revokes_token_on_poll(self) -> None:
        cur = self.c.broker.poll_events(since=None, timeout_ms=0)["next_since"]
        sp = self.c.broker.spawn_agent("worker-c", "worker-c", "worker", ["claude"])
        self.c.broker.register_local(sp["token"])
        self.assertIsNone(self.c.broker.authorize(sp["token"])[1])  # 当初有効
        # broker 非経由 kill (crash) → poll_events の reconcile が pane_exited 合成
        self.c.adapter.kill_pane(sp["pane_id"])
        self.c.broker.poll_events(since=cur, timeout_ms=0)
        # reap_exited_panes の明示呼出を待たず token が失効していること
        self.assertEqual(self.c.broker.authorize(sp["token"])[1], "token_revoked")


class SpawnInjectionTest(unittest.TestCase):
    """spawn_agent が token を先発行し --mcp-config を起動 argv に注入すること (codex Blocker)。"""

    def setUp(self) -> None:
        self.c = Cycle()
        self.c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
        self.c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)

    def tearDown(self) -> None:
        self.c.teardown()

    def test_mcp_config_injected_with_token(self) -> None:
        sp = self.c.broker.spawn_agent("worker-i", "worker-i", "worker", ["claude", "--x"])
        self.assertTrue(sp.get("ok"), sp)
        # token を先発行し pane に bind、per-agent の 0600 config を生成している
        self.assertIsNotNone(self.c.broker.get_bind(sp["token"]))
        cfg = self.c.broker.state_dir / "agents" / "worker-i.mcp.json"
        self.assertTrue(cfg.exists())
        import json as _json
        body = _json.loads(cfg.read_text(encoding="utf-8"))
        # config の Authorization に発行 token が埋まっている (worker の接続経路)
        auth = body["mcpServers"]["org-broker"]["headers"]["Authorization"]
        self.assertIn(sp["token"], auth)

    def test_agent_id_path_traversal_rejected(self) -> None:
        # agent_id に path traversal を仕込むと token 発行・config 書込み前に弾く
        for bad in ("../../evil", "/abs/evil", "a/b", "name.with.dot", ""):
            sp = self.c.broker.spawn_agent(bad, bad, "worker", ["claude"])
            self.assertFalse(sp.get("ok"), bad)
            self.assertIn("[name_invalid]", sp.get("error", ""), bad)

    def test_split_exception_revokes_token_and_sanitizes(self) -> None:
        # split が例外 → 発行済み token は revoke され、応答に例外文字列を漏らさない
        captured = {}
        orig_issue = self.c.broker.issue_token

        def _spy_issue(*a, **k):
            tok = orig_issue(*a, **k)
            captured["token"] = tok
            return tok

        self.c.broker.issue_token = _spy_issue
        self.c.adapter.split = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom --mcp-config /secret/path Bearer SECRET")
        )
        sp = self.c.broker.spawn_agent("worker-f", "worker-f", "worker", ["claude"])
        self.assertFalse(sp.get("ok"))
        self.assertEqual(sp.get("error"), "[io_error] split failed")  # 例外文字列を漏らさない
        self.assertNotIn("SECRET", sp.get("error", ""))
        self.assertEqual(
            self.c.broker.authorize(captured["token"])[1], "token_revoked"
        )


class SendKeysValidationTest(unittest.TestCase):
    """send_keys のキー語彙検証 (Set D §1.9 invalid-params) は broker 側で行う。"""

    def setUp(self) -> None:
        self.c = Cycle()
        self.c.add_role_pane("dispatcher", "dispatcher", 0, 0, 140, 43)
        self.h = self.c.handle_of("dispatcher")

    def tearDown(self) -> None:
        self.c.teardown()

    def test_known_keys_ok(self) -> None:
        r = self.c.broker.send_keys_op(self.h, keys=["Enter", "Shift+Tab", "Ctrl+C"])
        self.assertTrue(r.get("ok"), r)

    def test_unknown_key_invalid_params(self) -> None:
        r = self.c.broker.send_keys_op(self.h, keys=["Nope"])
        self.assertFalse(r.get("ok"))
        self.assertIn("[invalid-params]", r.get("error", ""))

    def test_unknown_handle_pane_not_found(self) -> None:
        r = self.c.broker.send_keys_op(999999, text="x")
        self.assertFalse(r.get("ok"))
        self.assertIn("[pane_not_found]", r.get("error", ""))


if __name__ == "__main__":
    unittest.main()
