# -*- coding: utf-8 -*-
"""AC-5 完動ゲート dogfood (Issue #5 / Epic #6) の方式 B 検証を CI 機械判定として常設する。

CI は `python -m unittest discover -s tests` で本ファイルを拾う。spike/ は importable
パッケージではないため sys.path に追加して broker / run_ac5 を読む。

**実 tmux smoke + 実 claude active 1 サイクル + idle attestation は CI から除外**
(sandbox で unix socket 不可 / 実 claude は課金を伴う)。FakeAdapter の決定的 6 検証のみを
CI に常設化する (実機分は spike/run_ac5.py --real-tmux の手動ランナー側で実証)。

設計 SoT: docs/design/renga-decoupling.md / spike/ac5-design-note.md (codex design review 反映)。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SPIKE = Path(__file__).resolve().parents[1] / "spike"
sys.path.insert(0, str(_SPIKE))

import run_ac5  # noqa: E402
from run_ac5 import Cycle, HEADLESS_FLAGS  # noqa: E402
from broker import SPAWNABLE_ROLES  # noqa: E402


class Ac5DogfoodTest(unittest.TestCase):
    """run_ac5 の 6 検証 (FakeAdapter) を CI 機械判定として常設する。"""

    def _run_check(self, fn):
        c = Cycle()
        try:
            go, detail = fn(c)
        finally:
            c.teardown()
        self.assertTrue(go, detail)

    def test_multi_cycle_isolation(self) -> None:
        self._run_check(run_ac5.check_multi)

    def test_stall_detect_to_escalation(self) -> None:
        self._run_check(run_ac5.check_stall)

    def test_escalation_defer_relay(self) -> None:
        self._run_check(run_ac5.check_escalation)

    def test_handover_keeps_pane(self) -> None:
        self._run_check(run_ac5.check_handover)

    def test_suspend_resume_isolation(self) -> None:
        self._run_check(run_ac5.check_resume)

    def test_billing_neutral_argv(self) -> None:
        self._run_check(run_ac5.check_billing)


class HeadlessFlagGuardTest(unittest.TestCase):
    """課金中立: spawn argv builder が全 spawnable role で headless 系 flag を混入しないこと。"""

    def setUp(self) -> None:
        self.c = Cycle()
        self.c.add_role_pane("secretary", "secretary", 0, 0, 280, 43)
        self.c.add_role_pane("dispatcher", "dispatcher", 0, 43, 140, 43)

    def tearDown(self) -> None:
        self.c.teardown()

    def test_no_headless_flag_for_any_spawnable_role(self) -> None:
        for role in SPAWNABLE_ROLES:
            wid = f"agent-{role}"
            sp = self.c.broker.spawn_agent(wid, wid, role, ["claude"])
            self.assertTrue(sp.get("ok"), sp)
            self.c.broker.register_local(sp["token"])
            argv = self.c.adapter.split_argv[-1]
            self.assertEqual(argv[0], "claude")
            self.assertIn("--mcp-config", argv)
            for bad in HEADLESS_FLAGS:
                self.assertNotIn(bad, argv, f"{role}: headless flag {bad} 混入")
            # 平文 token は argv に載らない (0600 config path 参照のみ)
            self.assertNotIn(sp["token"], argv)
            self.c.broker.close_pane_target(sp["handle"])

    def test_headless_argv_rejected(self) -> None:
        # 課金中立の構造強制: ヘッドレス / print 系 flag に加え、flag を持たない headless ラッパー
        # (python/node 等) や 空 argv も拒否する (token 注入 agent は対話 claude TUI のみ)。
        forbidden = (["claude", "-p", "x"], ["claude", "--print"], ["claude", "--headless"],
                     ["claude", "--output-format", "json"], ["claude", "--output-format=json"],
                     ["python", "agent_sdk_worker.py"], ["node", "agent.js"],
                     ["claude", "mcp", "serve"], ["claude", "doctor"])  # 非 TUI サブコマンド
        for bad in forbidden:
            r = self.c.broker.spawn_agent("agent-bad", "agent-bad", "worker", bad)
            self.assertFalse(r.get("ok"), bad)
            self.assertIn("[headless_forbidden]", r.get("error", ""), bad)
        # 空 argv は invalid-params
        r = self.c.broker.spawn_agent("agent-empty", "agent-empty", "worker", [])
        self.assertFalse(r.get("ok"))
        self.assertIn("[invalid-params]", r.get("error", ""))

    def test_non_claude_probe_allowed_without_config(self) -> None:
        # 非 claude プローブ (cat 等) は token 非注入 (inject_mcp_config=False) なら許可される
        # (org agent でないため whitelist 対象外)。
        r = self.c.broker.spawn_agent("probe", "probe", "worker", ["cat"], inject_mcp_config=False)
        self.assertTrue(r.get("ok"), r)
        self.c.broker.close_pane_target(r["handle"])


if __name__ == "__main__":
    unittest.main()
