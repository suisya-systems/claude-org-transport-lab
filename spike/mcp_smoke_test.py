# -*- coding: utf-8 -*-
"""broker の MCP プロトコル層を合成クライアントで検証する (Claude 不要・無課金)。

advisor 方針: 「protocol が悪いのか Claude が繋がらないのか」を切り分けるため、
実 Claude を spawn する前に stdlib クライアントで handshake / tools を全数確認する。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

# Windows コンソール (cp932) でも UTF-8 で出力する
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))
from broker import Broker  # noqa: E402


class MiniMcpClient:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self._id = 0

    def _post(self, payload: dict, expect_status: int = 200):
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
                **({"Mcp-Session-Id": self.session_id} if self.session_id else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            body = e.read()
        assert status == expect_status, f"status {status} != {expect_status}: {body!r}"
        return json.loads(body) if body else None

    def rpc(self, method: str, params: dict | None = None, expect_status: int = 200):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload, expect_status)

    def notify(self, method: str):
        self._post({"jsonrpc": "2.0", "method": method}, expect_status=202)

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        res = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        assert "result" in res, res
        return json.loads(res["result"]["content"][0]["text"])


def main() -> int:
    state = Path(__file__).parent / "broker-state" / "smoke"
    broker = Broker(state_dir=state, adapter=None)  # adapter なし = ナッジ無効
    broker.start()
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = ""):
        print(("  ok " if cond else "  NG ") + label + (f" — {detail}" if detail else ""))
        if not cond:
            failures.append(label)

    try:
        tok_a = broker.issue_token("agent-a", "agent-a", "worker")
        tok_b = broker.issue_token("agent-b", "agent-b", "worker")

        print("[1] handshake (initialize / initialized notification)")
        a = MiniMcpClient(broker.url, tok_a)
        init = a.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        })
        check("initialize returns result", "result" in init, json.dumps(init)[:120])
        check("protocolVersion echoed",
              init["result"]["protocolVersion"] == "2025-06-18")
        check("session id assigned", a.session_id is not None)
        a.notify("notifications/initialized")
        check("initialized notification -> 202", True)
        check("bind registered on initialize (AC-2-3 検知点)",
              broker.find_registered("agent-a") is not None)

        print("[2] tools/list")
        tl = a.rpc("tools/list")
        names = {t["name"] for t in tl["result"]["tools"]}
        check("worker surface = 4 tools",
              names == {"send_message", "check_messages", "list_peers", "set_summary"},
              str(names))

        print("[3] auth")
        bad = MiniMcpClient(broker.url, "wrong-token")
        resp = bad.rpc("initialize", {"protocolVersion": "2025-06-18"}, expect_status=401)
        check("invalid token -> 401 [token_invalid]",
              "token_invalid" in resp["error"]["message"])

        print("[4] messaging roundtrip + token 帰属")
        b = MiniMcpClient(broker.url, tok_b)
        b.rpc("initialize", {"protocolVersion": "2025-06-18"})
        b.notify("notifications/initialized")
        sent = a.call_tool("send_message",
                           {"to_id": "agent-b", "message": "こんにちは 🎌 multibyte test"})
        check("send_message ok", sent.get("ok") is True, str(sent))
        msgs = b.call_tool("check_messages")["messages"]
        check("delivered exactly 1", len(msgs) == 1)
        check("from 帰属が token 由来 (自己申告でない)",
              msgs and msgs[0]["from_id"] == "agent-a")
        check("multibyte 本文無傷",
              msgs and msgs[0]["message"] == "こんにちは 🎌 multibyte test")
        msgs2 = b.call_tool("check_messages")["messages"]
        check("at-most-once drain (2 回目は空)", msgs2 == [])

        print("[5] list_peers / set_summary")
        a.call_tool("set_summary", {"summary": "smoke testing"})
        peers = a.call_tool("list_peers")["peers"]
        ids = {p["id"] for p in peers}
        check("registered peers visible", ids == {"agent-a", "agent-b"}, str(ids))
        check("summary reflected",
              any(p["summary"] == "smoke testing" for p in peers))

        print("[6] unknown method / unknown tool")
        um = a.rpc("nonexistent/method")
        check("unknown method -> JSON-RPC error", "error" in um)
        ut = a.rpc("tools/call", {"name": "spawn_agent", "arguments": {}})
        check("worker から非公開 tool は isError",
              ut["result"].get("isError") is True)
    finally:
        broker.stop()

    print()
    if failures:
        print(f"SMOKE TEST FAILED: {len(failures)} failure(s): {failures}")
        return 1
    print("SMOKE TEST PASSED (all checks green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
