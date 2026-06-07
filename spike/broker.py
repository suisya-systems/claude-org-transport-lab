# -*- coding: utf-8 -*-
"""org-broker prototype (Phase 1 spike).

設計 SoT: docs/design/renga-decoupling.md §4 (broker/adapter 設計)・§7.1 (AC)。

スパイクとしての簡略化 (既知。Phase 3 本実装スコープとの境界):
- 認証は「長寿命 token + static headers」固定 (確定事項 (2))。
  TTL / headersHelper / 失効・再発行は実装しない。
- queue store は spike/broker-state/ 配下 (自己完結)。本体の .state/ には
  一切触れない (本体設計上の置き場 .state/broker/ は Phase 3 で扱う)。
- localhost (127.0.0.1) bind のみ。外部公開しない (non-goals §12 整合)。

MCP surface (worker / curator 向け最小面、設計書 §4.2):
  send_message / check_messages / list_peers / set_summary
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wezterm_adapter import NUDGE_TEXT, WezTermAdapter, classify_pane_state

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "org-broker-spike", "version": "0.1.0"}

TOOLS = [
    {
        "name": "send_message",
        "description": "Send a message to another agent via the broker queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_id": {"type": "string", "description": "Recipient agent id or name."},
                "message": {"type": "string", "description": "Text to deliver."},
            },
            "required": ["to_id", "message"],
        },
    },
    {
        "name": "check_messages",
        "description": "Drain queued messages addressed to this agent (at-most-once).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_peers",
        "description": "List registered agents visible to this agent.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_summary",
        "description": "Set a short summary of what this agent is working on.",
        "inputSchema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


class ToolArgError(ValueError):
    """tools/call の引数不正 (JSON-RPC -32602 invalid params に変換される)。"""


@dataclass
class AgentBind:
    """token ↔ agent/pane の bind (設計書 §4.4)。broker のみが保持する。"""

    token: str
    agent_id: str
    name: str
    role: str
    pane_id: int | None = None
    registered: bool = False          # MCP initialize 到達で True (AC-2-3 の検知点)
    registered_at: float | None = None
    session_id: str | None = None
    summary: str = ""
    revoked: bool = False


class Broker:
    """localhost HTTP MCP サーバー + queue store + ナッジ配達。"""

    def __init__(
        self,
        state_dir: str | Path,
        adapter: WezTermAdapter | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        nudge_defer_interval: float = 2.0,
        nudge_defer_max_tries: int = 30,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.adapter = adapter
        self.host = host
        self.port = port
        self.nudge_defer_interval = nudge_defer_interval
        self.nudge_defer_max_tries = nudge_defer_max_tries

        self._lock = threading.Lock()
        self._binds: dict[str, AgentBind] = {}        # token -> bind
        self._queues: dict[str, list[dict]] = {}      # agent_id -> messages
        self._nudge_threads: dict[str, threading.Thread] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        broker = self

        class Handler(_McpHandler):
            pass

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # クライアント側切断 (WinError 10054 等) はログ汚染しない
                import sys as _sys
                exc = _sys.exception()
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                                    BrokenPipeError, TimeoutError)):
                    return
                super().handle_error(request, client_address)

        Handler.broker = broker
        self._server = QuietServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="broker-http", daemon=True
        )
        self._thread.start()
        self._journal("broker_started", host=self.host, port=self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._journal("broker_stopped")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    # ----------------------------------------------------------------- token
    def issue_token(
        self, agent_id: str, name: str, role: str, pane_id: int | None = None
    ) -> str:
        """spawn 時の per-agent token 発行 (設計書 §4.4)。"""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._binds[token] = AgentBind(
                token=token, agent_id=agent_id, name=name, role=role, pane_id=pane_id
            )
            self._queues.setdefault(agent_id, [])
        self._journal("token_issued", agent_id=agent_id, role=role, pane_id=pane_id)
        return token

    def bind_pane(self, token: str, pane_id: int) -> None:
        with self._lock:
            self._binds[token].pane_id = pane_id

    def mcp_config_for(self, token: str, server_name: str = "org-broker") -> dict:
        """--mcp-config に渡す JSON。token は static headers に埋める (確定事項 (2))。

        env 参照 (${VAR}) は config parse 時の失敗リスクがあるため使わない。
        """
        return {
            "mcpServers": {
                server_name: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }

    def get_bind(self, token: str) -> AgentBind | None:
        with self._lock:
            bind = self._binds.get(token)
            if bind and not bind.revoked:
                return bind
            return None

    def find_registered(self, agent_id: str) -> AgentBind | None:
        """list_peers 相当の登録検知 (AC-2-3)。bind 表ベース。"""
        with self._lock:
            for b in self._binds.values():
                if b.agent_id == agent_id and b.registered and not b.revoked:
                    return b
        return None

    # ----------------------------------------------------------- queue store
    def _journal(self, event: str, **fields) -> None:
        rec = {"ts": time.time(), "event": event, **fields}
        path = self.state_dir / "queue.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def enqueue(self, from_bind: AgentBind, to_id: str, message: str) -> dict:
        """queue store 投入 + ナッジ配達 trigger。帰属は token 由来 (自己申告不可)。"""
        with self._lock:
            target: AgentBind | None = None
            for b in self._binds.values():
                if b.revoked:
                    continue
                if b.agent_id == to_id or b.name == to_id:
                    target = b
                    break
        if target is None:
            return {"ok": False, "error": f"[peer_not_found] no agent '{to_id}'"}
        entry = {
            "from_id": from_bind.agent_id,
            "from_name": from_bind.name,
            "sent_at": time.time(),
            "message": message,
        }
        with self._lock:
            self._queues.setdefault(target.agent_id, []).append(entry)
        self._journal(
            "message_enqueued",
            from_id=from_bind.agent_id,
            to_id=target.agent_id,
            chars=len(message),
        )
        self._trigger_nudge(target)
        return {"ok": True, "delivered_to": target.agent_id}

    def drain(self, bind: AgentBind) -> list[dict]:
        """at-most-once drain (Set D 2.3 継承)。"""
        with self._lock:
            msgs = self._queues.get(bind.agent_id, [])
            self._queues[bind.agent_id] = []
        if msgs:
            self._journal("queue_drained", agent_id=bind.agent_id, count=len(msgs))
        return msgs

    # ----------------------------------------------------------------- nudge
    def _trigger_nudge(self, target: AgentBind) -> None:
        """ナッジ配達 (設計書 §4.3)。定型 1 行のみ PTY 経由、本文は通さない。

        注入前に get-text で入力欄静止を確認し、静止していなければ
        defer + 再試行する (確定事項 (1) の静止確認)。
        重複ナッジは冪等 (キュー消費は check_messages 側で一度きり)。
        """
        if self.adapter is None or target.pane_id is None:
            return
        key = target.agent_id
        existing = self._nudge_threads.get(key)
        if existing and existing.is_alive():
            return  # 配達スレッドが既に走っている (冪等性)
        t = threading.Thread(
            target=self._nudge_worker, args=(target,), name=f"nudge-{key}", daemon=True
        )
        self._nudge_threads[key] = t
        t.start()

    def _nudge_worker(self, target: AgentBind) -> None:
        pane_id = target.pane_id
        assert pane_id is not None and self.adapter is not None
        for attempt in range(1, self.nudge_defer_max_tries + 1):
            with self._lock:
                pending = bool(self._queues.get(target.agent_id))
            if not pending:
                return  # 配達前に drain 済み (再ナッジ不要)
            try:
                state = classify_pane_state(self.adapter.get_text(pane_id))
            except Exception as e:  # adapter 不通は nudge_failed 相当
                self._journal(
                    "nudge_failed", agent_id=target.agent_id, error=str(e)
                )
                return
            if state == "idle":
                self.adapter.send_line(pane_id, NUDGE_TEXT)
                self._journal(
                    "nudge_sent",
                    agent_id=target.agent_id,
                    pane_id=pane_id,
                    attempt=attempt,
                )
                return
            self._journal(
                "nudge_deferred",
                agent_id=target.agent_id,
                pane_id=pane_id,
                state=state,
                attempt=attempt,
            )
            time.sleep(self.nudge_defer_interval)
        self._journal(
            "nudge_failed",
            agent_id=target.agent_id,
            pane_id=pane_id,
            error="defer retries exhausted",
        )

    # ------------------------------------------------------------- MCP tools
    def call_tool(self, bind: AgentBind, name: str, args: dict) -> dict:
        """ツール実行。引数不正は ToolArgError (handler 側で -32602 に変換)。"""
        if name == "send_message":
            to_id, message = args.get("to_id"), args.get("message")
            if not isinstance(to_id, str) or not isinstance(message, str):
                raise ToolArgError("send_message requires string to_id and message")
            result = self.enqueue(bind, to_id, message)
        elif name == "check_messages":
            result = {"messages": self.drain(bind)}
        elif name == "list_peers":
            with self._lock:
                result = {
                    "peers": [
                        {
                            "id": b.agent_id,
                            "name": b.name,
                            "role": b.role,
                            "summary": b.summary,
                        }
                        for b in self._binds.values()
                        if b.registered and not b.revoked
                    ]
                }
        elif name == "set_summary":
            summary = args.get("summary")
            if not isinstance(summary, str):
                raise ToolArgError("set_summary requires string summary")
            with self._lock:
                bind.summary = summary
            result = {"ok": True}
        else:
            return {
                "content": [{"type": "text", "text": f"[unknown_tool] {name}"}],
                "isError": True,
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ]
        }


class _McpHandler(BaseHTTPRequestHandler):
    """MCP streamable-HTTP (JSON-RPC over POST, application/json 応答)。"""

    broker: Broker  # start() 時に注入
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 標準 stderr ログ抑止
        pass

    def _send_json(self, status: int, payload: dict | None, session_id: str | None = None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # SSE ストリームは提供しない (POST 応答のみで完結)
        self._send_json(405, None)

    def do_DELETE(self):
        """セッション終了: 当該 bind の session を失効させる。

        POST 側と対称に、session 不一致 / 欠落は 404 で拒否する
        (codex review round 2 Major 対応)。_journal はロック外で呼ぶ
        (非再入 Lock の二重取得デッドロック回避。同 round Blocker 対応)。
        """
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        bind = self.broker.get_bind(token)
        if bind is None:
            self._send_json(401, None)
            return
        sid = self.headers.get("Mcp-Session-Id")
        closed = False
        with self.broker._lock:
            if bind.session_id is not None and sid == bind.session_id:
                bind.session_id = None
                closed = True
        if not closed:
            self._send_json(404, None)
            return
        self.broker._journal("session_closed", agent_id=bind.agent_id)
        self._send_json(200, None)

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            self._send_json(404, None)
            return
        # --- 認証 (per-agent token, 設計書 §4.4) -------------------------
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        bind = self.broker.get_bind(token)
        if bind is None:
            self._send_json(
                401,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "[token_invalid] unauthorized"},
                },
            )
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                },
            )
            return

        method = req.get("method", "")
        req_id = req.get("id")

        # --- セッション検証 (initialize 以外は Mcp-Session-Id 必須) -------
        # codex review Major 対応: bearer token のみで操作可能だと
        # initialize 前 / DELETE 後の stale client を排除できない。
        # 不一致は 404 (MCP spec: クライアントは再 initialize する)。
        if method != "initialize":
            sid = self.headers.get("Mcp-Session-Id")
            with self.broker._lock:
                expected = bind.session_id
            if expected is None or sid != expected:
                self._send_json(
                    404,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32001,
                            "message": "[session_invalid] initialize first",
                        },
                    },
                )
                return

        # --- notification (id なし) は 202 で受理 ------------------------
        if req_id is None:
            if method == "notifications/initialized":
                pass  # 登録自体は initialize 時に済んでいる
            self._send_json(202, None)
            return

        if method == "initialize":
            client_pv = (req.get("params") or {}).get("protocolVersion", "")
            pv = client_pv if client_pv in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            session_id = secrets.token_hex(16)
            with self.broker._lock:
                bind.registered = True
                bind.registered_at = time.time()
                bind.session_id = session_id
            self.broker._journal(
                "agent_registered", agent_id=bind.agent_id, role=bind.role
            )
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": pv,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                },
                session_id=session_id,
            )
        elif method == "tools/list":
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}},
            )
        elif method == "tools/call":
            params = req.get("params") or {}
            try:
                result = self.broker.call_tool(
                    bind, params.get("name", ""), params.get("arguments") or {}
                )
            except ToolArgError as e:
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"invalid params: {e}"},
                    },
                )
                return
            self._send_json(
                200, {"jsonrpc": "2.0", "id": req_id, "result": result}
            )
        elif method == "ping":
            self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})
        else:
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                },
            )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="org-broker spike (standalone)")
    ap.add_argument("--port", type=int, default=48720)
    ap.add_argument("--state-dir", default=str(Path(__file__).parent / "broker-state"))
    ns = ap.parse_args()
    b = Broker(state_dir=ns.state_dir, adapter=WezTermAdapter(), port=ns.port)
    b.start()
    print(f"org-broker spike listening on {b.url}")
    tok = b.issue_token("manual-test", "manual-test", "worker")
    print("manual test token:", tok)
    print("mcp-config:", json.dumps(b.mcp_config_for(tok)))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        b.stop()
