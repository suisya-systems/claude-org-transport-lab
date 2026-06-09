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

    def test_started_then_exited_exactly_once(self) -> None:
        cur = self.c.broker.poll_events(since=None, timeout_ms=0)["next_since"]
        self.c.adapter.add_pane("p1", width=40, height=10)
        self.c.broker._reconcile()
        ev = self.c.broker.poll_events(since=cur, timeout_ms=0)
        started = [e for e in ev["events"] if e["type"] == "pane_started"]
        self.assertEqual(len(started), 1)
        cur = ev["next_since"]
        # 再 poll では同じ pane_started を再配信しない (exactly-once)
        ev2 = self.c.broker.poll_events(since=cur, timeout_ms=0)
        self.assertEqual([e for e in ev2["events"] if e["type"] == "pane_started"], [])
        # kill → pane_exited 1 回
        self.c.adapter.kill_pane("p1")
        ev3 = self.c.broker.poll_events(since=cur, timeout_ms=0)
        exited = [e for e in ev3["events"] if e["type"] == "pane_exited"]
        self.assertEqual(len(exited), 1)

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
