# -*- coding: utf-8 -*-
"""K1 spike の決定的 CI テスト (無課金・claude 不要)。

push 一次配送 daemon (k1_daemon) の配送ライフサイクル不変条件 (§9.3) と
delivery-scoped credential 境界 (§9.4)、tool-less channel sidecar の
initialize / push emit を回帰被覆する。実 claude の idle-wake (AC-2) は
spike/run_k1.py が実機で担う (CI 非常設・課金のため)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

SPIKE = Path(__file__).resolve().parent.parent / "spike"
sys.path.insert(0, str(SPIKE))

from k1_daemon import Daemon, DaemonServer, UNDELIVERED, CLAIMED, DELIVERED, PUSH, PULL  # noqa: E402
import broker as broker_mod  # noqa: E402


def _daemon(tmp: Path, lease=5.0) -> Daemon:
    tmp.mkdir(parents=True, exist_ok=True)
    return Daemon(state_dir=tmp, lease_seconds=lease)


class DeliveryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/claude/broker-k1-spike/ci") / self._testMethodName
        if self.tmp.exists():
            for f in self.tmp.glob("*"):
                f.unlink()
        self.d = _daemon(self.tmp)
        self.cred = self.d.creds[self.d.issue_cred("w", "delivery")]

    def test_claim_emit_confirm_happy_path(self):
        rid = self.d.enqueue("w", "hello", {"k": "v"})
        claimed = self.d.poll_claims(self.cred)["rows"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], rid)
        # claim 直後はまだ CLAIMED (confirm 前)。配達確定は emit の後。
        self.assertEqual(self.d.rows[rid].state, CLAIMED)
        res = self.d.confirm_delivered(self.cred, rid, claimed[0]["epoch"])
        self.assertTrue(res["ok"])
        self.assertEqual(self.d.rows[rid].state, DELIVERED)

    def test_confirm_rejects_unclaimed_row(self):
        # §9.3: 未 claim(UNDELIVERED) の行を confirm できない
        rid = self.d.enqueue("w", "x", {})
        res = self.d.confirm_delivered(self.cred, rid, self.d.epoch)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not_claimed")
        self.assertEqual(self.d.rows[rid].state, UNDELIVERED)

    def test_confirm_rejects_lease_expired_claim(self):
        # lease 失効後の stale claim を **reaper 未実行のまま** confirm しても DELIVERED 化しない。
        # confirm_delivered 内の明示 reap が失効 claim を UNDELIVERED へ戻し拒否する（沈黙喪失防止）。
        d = _daemon(self.tmp, lease=0.3)
        cred = d.creds[d.issue_cred("w", "delivery")]
        rid = d.enqueue("w", "y", {})
        c = d.poll_claims(cred)["rows"][0]      # CLAIMED, lease=now+0.3
        self.assertEqual(d.rows[rid].state, CLAIMED)
        time.sleep(0.45)                        # lease 失効（poll を挟まないので reaper は未実行）
        res = d.confirm_delivered(cred, rid, c["epoch"])
        self.assertFalse(res["ok"])             # 失効 claim の confirm は拒否
        self.assertEqual(res["error"], "not_claimed")
        self.assertNotEqual(d.rows[rid].state, DELIVERED)
        self.assertEqual(d.rows[rid].state, UNDELIVERED)  # 再 eligible（回復可能）

    def test_confirm_is_idempotent(self):
        rid = self.d.enqueue("w", "x", {})
        c = self.d.poll_claims(self.cred)["rows"][0]
        self.d.confirm_delivered(self.cred, rid, c["epoch"])
        again = self.d.confirm_delivered(self.cred, rid, c["epoch"])
        self.assertTrue(again["ok"])
        self.assertTrue(again.get("idempotent"))

    def test_lease_reaping_recovers_dead_sidecar(self):
        """confirm されないまま lease 失効した CLAIMED 行は UNDELIVERED へ戻る。"""
        d = _daemon(self.tmp, lease=0.3)
        cred = d.creds[d.issue_cred("w", "delivery")]
        rid = d.enqueue("w", "lost?", {})
        d.poll_claims(cred)                     # claim するが confirm しない (sidecar 死亡)
        self.assertEqual(d.rows[rid].state, CLAIMED)
        time.sleep(0.4)
        reclaimed = d.poll_claims(cred)["rows"]  # reaping 後に再 eligible
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0]["id"], rid)
        self.assertGreaterEqual(d.rows[rid].reclaim_count, 1)

    def test_delivery_scope_cannot_read_other_owner(self):
        other = self.d.creds[self.d.issue_cred("other", "delivery")]
        self.d.enqueue("w", "for-w", {})
        self.assertEqual(self.d.poll_claims(other)["rows"], [])   # 他 owner 行は claim 不可

    def test_delivery_scope_rejected_on_pull_endpoint(self):
        # delivery cred は check_messages (pull) を呼べない (§9.4 least-privilege)
        res = self.d.check_messages(self.cred)
        self.assertEqual(res.get("error"), "forbidden_scope")

    def test_mode_epoch_fencing_rejects_stale_confirm(self):
        rid = self.d.enqueue("w", "x", {})
        c = self.d.poll_claims(self.cred)["rows"][0]
        old_epoch = c["epoch"]
        self.d.flip_mode(PULL)                  # epoch を進める (flip 時 in-flight は UNDELIVERED へ)
        res = self.d.confirm_delivered(self.cred, rid, old_epoch)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "stale_epoch")
        # flip 後の行は再 eligible (沈黙喪失しない)
        self.assertEqual(self.d.rows[rid].state, UNDELIVERED)

    def test_push_disabled_after_flip_to_pull(self):
        self.d.flip_mode(PULL)
        self.d.enqueue("w", "x", {})
        res = self.d.poll_claims(self.cred)
        self.assertEqual(res.get("error"), "push_disabled")   # 新規 claim 発行を拒否

    def test_pull_does_not_double_deliver_live_claim(self):
        # live な sidecar claim 中の行は pull (check_messages) に出ない（二重配達しない）
        full = self.d.creds[self.d.issue_cred("w", "full")]
        self.d.enqueue("w", "a", {})
        self.d.poll_claims(self.cred)           # CLAIMED (lease 内)
        self.assertEqual(self.d.check_messages(full)["messages"], [])
        # 注: 並行 single-drainer 性は check_messages が _lock を drain 全体で保持することで担保（k1_daemon.py）。
        # 本テストは serial idempotency（2 回目空）を確認する: drain 後 DELIVERED で再取得されない。
        self.d.flip_mode(PULL)                  # push を止め pull 経路に倒す（in-flight は UNDELIVERED へ）
        first = self.d.check_messages(full)["messages"]
        self.assertEqual(len(first), 1)         # reaped row を pull が回収
        self.assertEqual(self.d.check_messages(full)["messages"], [])  # 2 回目は空

    def test_spoof_from_is_ignored(self):
        # enqueue は to_id 宛先のみ。meta は本文付帯で、from 帰属は daemon の owner 由来。
        rid = self.d.enqueue("w", "x", {"from_id": "attacker"})
        c = self.d.poll_claims(self.cred)["rows"][0]
        # delivery cred は owner=w に固定され、攻撃者 owner の行は触れない
        self.assertEqual(self.d.rows[rid].to_id, "w")
        self.assertEqual(c["meta"]["from_id"], "attacker")   # meta は本文 — 帰属ではない
        # owner 偽装での confirm は not_owner
        other = self.d.creds[self.d.issue_cred("attacker", "delivery")]
        self.assertEqual(
            self.d.confirm_delivered(other, rid, c["epoch"])["error"], "not_owner")


class ToollessSidecarSubprocessTest(unittest.TestCase):
    """channel_sidecar.py を subprocess 起動し tool-less initialize + push emit を検証。"""

    def test_tool_less_initialize_and_push(self):
        from k1_daemon import DaemonServer
        state = Path("/tmp/claude/broker-k1-spike/ci/sidecar")
        srv = DaemonServer(state, lease_seconds=5.0)
        srv.start()
        try:
            cred = srv.daemon.issue_cred("w", "delivery")
            env = {"PATH": "/usr/bin:/bin", "K1_DAEMON_URL": srv.url,
                   "K1_DELIVERY_CRED": cred, "K1_OWNER": "w",
                   "K1_POLL_INTERVAL": "0.3", "K1_SOURCE_NAME": "org-broker-channel"}
            proc = subprocess.Popen([sys.executable, str(SPIKE / "channel_sidecar.py")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
            out = []
            ev = threading.Event()

            def reader():
                for raw in proc.stdout:
                    line = raw.decode("utf-8").strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                            ev.set()
                        except json.JSONDecodeError:
                            pass
            threading.Thread(target=reader, daemon=True).start()

            def send(o):
                proc.stdin.write((json.dumps(o) + "\n").encode())
                proc.stdin.flush()

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
            time.sleep(0.5)
            init = next((m for m in out if m.get("id") == 1), None)
            caps = (init or {}).get("result", {}).get("capabilities", {})
            self.assertIn("claude/channel", caps.get("experimental", {}))
            self.assertNotIn("tools", caps)     # tool-less: tools capability を宣言しない

            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            time.sleep(0.3)
            srv.daemon.enqueue("w", "push-payload", {"from_id": "obs"})
            deadline = time.time() + 5
            pushed = None
            while time.time() < deadline:
                pushed = next((m for m in out
                               if m.get("method") == "notifications/claude/channel"), None)
                if pushed:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(pushed)
            self.assertEqual(pushed["params"]["content"], "push-payload")
            self.assertEqual(pushed["params"]["meta"]["from_id"], "obs")
            proc.stdin.close()
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        finally:
            srv.stop()


class LeaseReapEndToEndTest(unittest.TestCase):
    """fault sidecar が emit 後 confirm せず死亡 → lease reaping → fallback で回復（§9.3）。

    in-process 単体だけでなく、実 sidecar subprocess を daemon(HTTP) に対して回し、
    emit と confirm の間で死んだケースの **end-to-end** 回復を実証する。"""

    def test_emit_without_confirm_recovers_via_reap(self):
        state = Path("/tmp/claude/broker-k1-spike/ci/reap")
        srv = DaemonServer(state, lease_seconds=0.6)
        srv.start()
        try:
            cred = srv.daemon.issue_cred("w", "delivery")
            full = srv.daemon.creds[srv.daemon.issue_cred("w", "full")]
            env = {"PATH": "/usr/bin:/bin", "K1_DAEMON_URL": srv.url,
                   "K1_DELIVERY_CRED": cred, "K1_OWNER": "w",
                   "K1_POLL_INTERVAL": "0.2", "K1_SOURCE_NAME": "org-broker-channel",
                   "K1_FAULT": "skip-confirm"}
            proc = subprocess.Popen([sys.executable, str(SPIKE / "channel_sidecar.py")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
            out = []

            def reader():
                for raw in proc.stdout:
                    line = raw.decode("utf-8").strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            threading.Thread(target=reader, daemon=True).start()

            def send(o):
                proc.stdin.write((json.dumps(o) + "\n").encode())
                proc.stdin.flush()

            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            time.sleep(0.4)
            rid = srv.daemon.enqueue("w", "recover-me", {"from_id": "obs"})

            # emit が来るのを待つ（faulty sidecar は emit するが confirm しない）
            deadline = time.time() + 4
            emitted = False
            while time.time() < deadline:
                if any(m.get("method") == "notifications/claude/channel" for m in out):
                    emitted = True
                    break
                time.sleep(0.1)
            self.assertTrue(emitted, "faulty sidecar should emit")
            # confirm していないので DELIVERED にはなっていない
            self.assertNotEqual(srv.daemon.rows[rid].state, DELIVERED)

            # sidecar 死亡 -> 以後 claim されない
            proc.stdin.close()
            proc.kill()
            proc.wait(timeout=3)
            for s in (proc.stdout, proc.stderr):
                try:
                    s.close()
                except OSError:
                    pass

            # lease 失効後、fallback(pull) が reap して回復（沈黙喪失しない）
            time.sleep(1.0)
            recovered = srv.daemon.check_messages(full)["messages"]
            self.assertEqual([m["id"] for m in recovered], [rid])
            self.assertEqual(srv.daemon.rows[rid].state, DELIVERED)
        finally:
            srv.stop()


class BillingAllowlistTests(unittest.TestCase):
    """K1 で追加した --dangerously-load-development-channels の課金中立 allowlist 回帰。

    自己課す保守契約（RESULTS.md Phase 5 / K1 節）: allowlist 変更時は許可/拒否ケースを更新する。"""

    def _argv(self, *extra):
        return ["/home/x/.local/bin/claude", "--mcp-config", "/c",
                "--strict-mcp-config", *extra, "--model", "sonnet"]

    def test_single_channel_ceremony_accepted(self):
        ok, why = broker_mod.is_interactive_claude_argv(
            self._argv("--dangerously-load-development-channels", "server:org-broker-channel"))
        self.assertTrue(ok, why)

    def test_headless_still_rejected_even_with_channel_flag(self):
        ok, _ = broker_mod.is_interactive_claude_argv(
            ["claude", "-p", "--dangerously-load-development-channels", "server:x"])
        self.assertFalse(ok)

    def test_multi_value_coexist_form_rejected(self):
        # coexist テストの複数 channel 同時 load 形は本番 spawn 経路では使わない（guard 対象外で拒否される）
        ok, _ = broker_mod.is_interactive_claude_argv(
            self._argv("--dangerously-load-development-channels",
                       "server:a", "server:b"))
        self.assertFalse(ok)

    def test_bare_positional_still_rejected(self):
        ok, _ = broker_mod.is_interactive_claude_argv(["claude", "mcp", "serve"])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
