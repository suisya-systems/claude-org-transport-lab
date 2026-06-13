# -*- coding: utf-8 -*-
"""K1 プロトコル層スモーク (無課金・claude 不要)。

daemon (in-process) + channel_sidecar.py (subprocess) を直結し、
JSON-RPC を stdin に流して claude/channel push の機構を検証する:

  1. initialize 応答が tool-less (experimental{claude/channel} のみ・tools 非宣言)
  2. enqueue した行が claim->emit (notifications/claude/channel) される
  3. emit 後に /confirm-delivered で DELIVERED に確定する
  4. delivery-scoped cred は別 owner の行を claim できない (§9.4 least-privilege)

実 claude の idle-wake (AC-2) は run_k1.py が担う。本スモークは配管の決定的検証。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from k1_daemon import DaemonServer  # noqa: E402

SIDE = str(Path(__file__).parent / "channel_sidecar.py")


def _reader(proc, sink, ready):
    for raw in proc.stdout:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        try:
            sink.append(json.loads(line))
        except json.JSONDecodeError:
            pass
        ready.set()


def main() -> int:
    state = Path("/tmp/claude/broker-k1-spike/smoke")
    srv = DaemonServer(state, lease_seconds=5.0)
    srv.start()
    d = srv.daemon
    cred = d.issue_cred("worker", "delivery")
    other = d.issue_cred("other-owner", "delivery")

    env = {
        "PATH": "/usr/bin:/bin",
        "K1_DAEMON_URL": srv.url,
        "K1_DELIVERY_CRED": cred,
        "K1_OWNER": "worker",
        "K1_POLL_INTERVAL": "0.3",
        "K1_SOURCE_NAME": "org-broker-channel",
    }
    proc = subprocess.Popen(
        [sys.executable, SIDE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )
    out: list[dict] = []
    ready = threading.Event()
    threading.Thread(target=_reader, args=(proc, out, ready), daemon=True).start()

    def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    results = {}

    # 1. initialize
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    time.sleep(0.5)
    init = next((m for m in out if m.get("id") == 1), None)
    caps = (init or {}).get("result", {}).get("capabilities", {})
    results["tool_less_initialize"] = (
        init is not None
        and "claude/channel" in caps.get("experimental", {})
        and "tools" not in caps           # tool-less: tools capability を宣言しない
    )

    # initialized -> push loop arm
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(0.4)

    # 2/3. enqueue -> emit -> confirm
    before = len(out)
    rid = d.enqueue("worker", "K1 push smoke payload",
                    {"from_id": "observer", "kind": "test"})
    deadline = time.time() + 5
    pushed = None
    while time.time() < deadline:
        for m in out[before:]:
            if m.get("method") == "notifications/claude/channel":
                pushed = m
                break
        if pushed:
            break
        time.sleep(0.1)
    results["channel_push_emitted"] = (
        pushed is not None
        and pushed["params"]["content"] == "K1 push smoke payload"
        and pushed["params"]["meta"].get("from_id") == "observer"
    )
    time.sleep(0.6)
    row_state = next((r for r in d.dump()["rows"] if r["id"] == rid), {})
    results["confirmed_delivered"] = row_state.get("state") == "DELIVERED"

    # 4. cross-owner isolation: other-owner cred は worker 宛行を claim できない
    d.enqueue("worker", "should-not-leak", {})
    leak = d.poll_claims(d.creds[other])
    results["delivery_scope_isolation"] = (len(leak.get("rows", [])) == 0)

    proc.stdin.close()
    proc.terminate()
    srv.stop()

    print(json.dumps(results, indent=2))
    ok = all(results.values())
    print("SMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
