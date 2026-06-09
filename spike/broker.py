# -*- coding: utf-8 -*-
"""org-broker prototype (Phase 1 spike).

設計 SoT: docs/design/renga-decoupling.md §4 (broker/adapter 設計)・§7.1 (AC)。

スパイクとしての簡略化 (既知。Phase 3 本実装スコープとの境界):
- 認証は static headers で token を渡す (確定事項 (2))。token ライフサイクル
  (TTL / pane_exited revoke / close revoke / suspend-resume 再発行) は Phase 3 で
  本実装した (§4.4。下記 issue_token / authorize / revoke_* / suspend を参照)。
  headersHelper による動的更新は導入していない (static headers のまま)。
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

from terminal_adapter import (
    NUDGE_TEXT,
    PaneId,
    TerminalAdapter,
    classify_pane_state,
    make_adapter,
)

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
    pane_id: PaneId | None = None     # backend ネイティブ型 (WezTerm=int / tmux="%N"=str)
    registered: bool = False          # MCP initialize 到達で True (AC-2-3 の検知点)
    registered_at: float | None = None
    session_id: str | None = None
    summary: str = ""
    revoked: bool = False
    revoked_reason: str | None = None   # pane_exited / close_pane / suspend / 明示
    issued_at: float | None = None
    expires_at: float | None = None     # TTL 失効刻 (None=失効なし)。§4.4 の保険

    def is_active(self, now: float | None = None) -> bool:
        """revoke も TTL 失効もしていない = 帰属・配送・公開面の有効判定。"""
        if self.revoked:
            return False
        if self.expires_at is not None and (now or time.time()) >= self.expires_at:
            return False
        return True

    def auth_error(self, now: float | None = None) -> str | None:
        """無効時のエラーコード (設計書 §5 Surface 6 の新設語彙)。有効なら None。"""
        if self.revoked:
            return "token_revoked"
        if self.expires_at is not None and (now or time.time()) >= self.expires_at:
            return "token_expired"
        return None


class Broker:
    """localhost HTTP MCP サーバー + queue store + ナッジ配達。"""

    def __init__(
        self,
        state_dir: str | Path,
        adapter: TerminalAdapter | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        nudge_defer_interval: float = 2.0,
        nudge_defer_max_tries: int = 30,
        default_token_ttl: float | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.adapter = adapter
        self.host = host
        self.port = port
        self.nudge_defer_interval = nudge_defer_interval
        self.nudge_defer_max_tries = nudge_defer_max_tries
        # 既定 TTL (None=失効なし)。§4.4 はセッション寿命より長い TTL を基本とし、
        # TTL は失効漏れの保険。issue_token に明示 ttl があればそちらを優先。
        self.default_token_ttl = default_token_ttl

        self._lock = threading.Lock()
        self._binds: dict[str, AgentBind] = {}        # token -> bind
        self._queues: dict[str, list[dict]] = {}      # agent_id -> messages
        # key=agent_id -> (配達スレッド, そのスレッドが宛先とする token)。
        # token を併せ持つことで「生存しているが宛先 token が失効済みの dying worker」を
        # dedup から除外できる (codex round 4 Major 対応)。
        self._nudge_threads: dict[str, tuple[threading.Thread, str]] = {}
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
        self,
        agent_id: str,
        name: str,
        role: str,
        pane_id: PaneId | None = None,
        ttl: float | None = None,
    ) -> str:
        """spawn 時の per-agent token 発行 (設計書 §4.4)。

        ttl を与えると発行時刻 + ttl 秒で失効する (None=失効なし)。設計書 §4.4 は
        「セッション寿命より長い TTL + 退役時 revoke」を基本とし、TTL は失効漏れの
        保険と位置付ける。suspend/resume をまたいだ token 再利用は不可 (resume は
        本メソッドの再呼出で別 token を再発行する。旧 token は revoke 済みのまま)。
        """
        ttl = self.default_token_ttl if ttl is None else ttl
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self._lock:
            # 既存の有効 bind が他に無い agent_id への発行 = 新規ライフサイクルの
            # 開始 (初回 spawn / 退役・TTL 失効後の resume 再発行)。この場合は旧
            # ライフサイクルの未読キューを破棄して継承を断つ。TTL 失効は revoke_token
            # を経ないため setdefault のままだと旧キューを引き継ぐ (codex round 2
            # Major-B 対応)。有効 bind が他に在る場合 (同一 agent の追加 token) は
            # 既存キューを尊重する。
            had_active = any(
                b.agent_id == agent_id and b.is_active(now)
                for b in self._binds.values()
            )
            self._binds[token] = AgentBind(
                token=token, agent_id=agent_id, name=name, role=role, pane_id=pane_id,
                issued_at=now,
                expires_at=(now + ttl) if ttl is not None else None,
            )
            if had_active:
                self._queues.setdefault(agent_id, [])
            else:
                self._queues[agent_id] = []
                # 旧ライフサイクルの nudge thread dedup エントリも eager に破棄する。
                # _trigger_nudge は token 失効を見て dying worker を信用しないため
                # (codex round 4 Major 対応) 本 pop が無くても新規 nudge は起動するが、
                # 失効エントリを即時掃除して dedup 表の肥大化を防ぐ (codex round 3 由来)。
                self._nudge_threads.pop(agent_id, None)
        self._journal(
            "token_issued", agent_id=agent_id, role=role, pane_id=pane_id,
            ttl=ttl,
        )
        return token

    # --------------------------------------------------------- token lifecycle
    def authorize(self, token: str) -> tuple[AgentBind | None, str | None]:
        """token を分類して (bind, error_code) を返す (設計書 §4.4 / §5 Surface 6)。

        error_code: None(有効) / "token_invalid"(未知) / "token_revoked"(失効) /
        "token_expired"(TTL 超過)。HTTP ハンドラと直呼び両方の単一権限判定点。
        """
        with self._lock:
            bind = self._binds.get(token)
            if bind is None:
                return None, "token_invalid"
            err = bind.auth_error()
            if err is not None:
                return None, err
            return bind, None

    def revoke_token(self, token: str, reason: str = "revoked") -> bool:
        """token を即時失効させる。冪等 (既失効は False)。

        失効と同時に session / 登録も落とし、list_peers・配送先・以後の MCP 呼出
        から構造的に排除する。子プロセスに env が漏れていても以後使えない (§4.4)。

        併せて当該 agent の未読キューも破棄する: queue は agent_id 単位で drain される
        ため、suspend/退役後に同一 agent_id で token を再発行すると旧ライフサイクルの
        未読が新 token に漏れる (codex Major 対応)。退役済み宛の未読は配達不能であり、
        破棄が正しい。pending nudge も drain 対象が空になることで自然停止する。
        """
        with self._lock:
            bind = self._binds.get(token)
            if bind is None or bind.revoked:
                return False
            bind.revoked = True
            bind.revoked_reason = reason
            bind.registered = False
            bind.session_id = None
            agent_id = bind.agent_id
            # 同一 agent_id の有効 bind が他に無ければキューを破棄する
            # (resume で別 token を後発行する経路は issue_token が空キューを再用意)。
            if not any(
                b.agent_id == agent_id and not b.revoked
                for b in self._binds.values()
            ):
                self._queues[agent_id] = []
        self._journal("token_revoked", agent_id=agent_id, reason=reason)
        return True

    def revoke_pane(self, pane_id: PaneId, reason: str = "pane_exited") -> list[str]:
        """指定 pane に bind された全 token を revoke する (pane 退役 / close_pane)。"""
        with self._lock:
            tokens = [
                b.token for b in self._binds.values()
                if b.pane_id == pane_id and not b.revoked
            ]
        revoked: list[str] = []
        for t in tokens:
            if self.revoke_token(t, reason=reason):
                with self._lock:
                    revoked.append(self._binds[t].agent_id)
        return revoked

    def reap_exited_panes(self) -> list[str]:
        """adapter で退役した pane を検出し、その token を revoke する (§4.4)。

        正規の pane_exited イベント経路 (Phase 4 の poll_events) の messaging 段階での
        代替。dispatcher 監視ループ等から定期的に呼ぶ想定。adapter 不通時は誤 revoke を
        避けるため何もしない (生存判定不能を「退役」と扱わない)。戻り値: revoke した agent_id。
        """
        if self.adapter is None:
            return []
        with self._lock:
            bound = [
                (b.token, b.pane_id) for b in self._binds.values()
                if b.pane_id is not None and not b.revoked
            ]
        revoked: list[str] = []
        for token, pane_id in bound:
            try:
                alive = self.adapter.pane_exists(pane_id)
            except Exception:
                continue  # adapter 不通 = 生存判定不能。誤 revoke しない
            if not alive and self.revoke_token(token, reason="pane_exited"):
                with self._lock:
                    revoked.append(self._binds[token].agent_id)
        return revoked

    def close_pane(self, pane_id: PaneId) -> list[str]:
        """broker 経由の pane クローズ + 即時 revoke (§4.4)。

        adapter で pane を kill し、成功時にその pane の token を revoke する。pane 操作は
        Phase 4 で MCP 公開 (worker/curator 非公開) する面なので、ここでは broker
        内部 API として置く (messaging Phase のライフサイクル検証用)。

        kill が失敗した場合は pane が生存している可能性があるため revoke しない
        (reap_exited_panes の「生存判定不能を退役扱いしない」方針と統一)。実 adapter は
        kill を check=False で呼び失敗を例外化しないため、例外捕捉だけでは塞げない。
        kill 後に pane_exists で実際に消えたことを確認してから revoke する
        (codex round 2 Minor 対応)。生存判定不能 (adapter 不通) 時は close の意図を
        尊重して revoke する。
        """
        if self.adapter is not None:
            try:
                self.adapter.kill_pane(pane_id)
            except Exception as e:
                self._journal("close_pane_failed", pane_id=pane_id, error=str(e))
                return []  # kill 例外 = live pane の可能性。誤 revoke しない
            try:
                if self.adapter.pane_exists(pane_id):
                    # kill が例外を出さずとも pane が残存 = 退役失敗。revoke しない
                    self._journal("close_pane_still_alive", pane_id=pane_id)
                    return []
            except Exception:
                pass  # 生存判定不能時は従来どおり close 意図を尊重して revoke
        return self.revoke_pane(pane_id, reason="close_pane")

    def suspend(self) -> int:
        """全 token を revoke する (/org-suspend 相当、§4.4)。

        resume 時は issue_token の再呼出で別 token を再発行する (suspend をまたいだ
        token 再利用は不可)。戻り値: revoke した token 数。
        """
        with self._lock:
            tokens = [t for t, b in self._binds.items() if not b.revoked]
        n = sum(1 for t in tokens if self.revoke_token(t, reason="suspend"))
        self._journal("broker_suspended", revoked=n)
        return n

    def bind_pane(self, token: str, pane_id: PaneId) -> None:
        with self._lock:
            self._binds[token].pane_id = pane_id

    def register_local(self, token: str) -> None:
        """MCP を経由しない server-side 合成エージェント (検証用 observer 等) を
        登録済みにする。実エージェントの登録は initialize 到達でのみ行う。"""
        with self._lock:
            bind = self._binds[token]
            bind.registered = True
            bind.registered_at = time.time()

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
            if bind and bind.is_active():
                return bind
            return None

    def find_registered(self, agent_id: str) -> AgentBind | None:
        """list_peers 相当の登録検知 (AC-2-3)。bind 表ベース。"""
        with self._lock:
            for b in self._binds.values():
                if b.agent_id == agent_id and b.registered and b.is_active():
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
        """queue store 投入 + ナッジ配達 trigger。帰属は token 由来 (自己申告不可)。

        from_bind は呼出元の bind であり、`from_id` / `from_name` はここから付与する
        (クライアント自己申告フィールドは一切採らない = なりすまし構造的不可、§4.4)。
        直呼び経路でも送信者 token が失効していれば送れない (HTTP 経路は authorize で
        既に弾かれるが、直呼びの revoke/expire テストを意味あるものにするための保険)。
        """
        sender_err = from_bind.auth_error()
        if sender_err is not None:
            return {"ok": False, "error": f"[{sender_err}] sender token not active"}
        with self._lock:
            target: AgentBind | None = None
            for b in self._binds.values():
                # registered かつ有効 (未失効 / TTL 内) な bind のみ配送先にする
                # (未接続 / DELETE 済み / revoke 済み client への配送を防ぐ。
                #  codex review round 3 Major 対応 + Phase 3 ライフサイクル)
                if not b.is_active() or not b.registered:
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
        # check-and-set はロック下で行う: ThreadingHTTPServer 配下で同一宛先へ
        # 並行 send_message された場合の nudge worker 二重起動 (= NUDGE_TEXT
        # 二重注入) を防ぐ (codex review round 3 Major 対応)
        with self._lock:
            existing = self._nudge_threads.get(key)
            if existing is not None:
                ex_thread, ex_token = existing
                ex_bind = self._binds.get(ex_token)
                if ex_thread.is_alive() and ex_bind is not None and ex_bind.is_active():
                    return  # 有効 token の配達スレッドが稼働中 (冪等性)
                # 生存していても token が失効済み = 旧ライフサイクルの dying worker
                # (新しい未読を配達しない)。信用せず新 worker を起動する。dedup を
                # agent_id 単位の is_alive() だけにすると、同一 agent_id に別の有効
                # token が残るケースで新 worker が抑止される (codex round 4 Major
                # 対応)。worker が宛先とする token の有効性まで含めて dedup する。
            t = threading.Thread(
                target=self._nudge_worker, args=(target,),
                name=f"nudge-{key}", daemon=True,
            )
            self._nudge_threads[key] = (t, target.token)
        t.start()

    def _nudge_worker(self, target: AgentBind) -> None:
        pane_id = target.pane_id
        assert pane_id is not None and self.adapter is not None
        for attempt in range(1, self.nudge_defer_max_tries + 1):
            # 失効した宛先には打鍵しない (revoke/TTL 後の stale target への
            # nudge を防ぐ。codex Major 対応)。revoke はキューも空にするため
            # pending 判定でも止まるが、打鍵直前の TOCTOU 窓を本チェックで閉じる。
            if not target.is_active():
                return
            with self._lock:
                pending = bool(self._queues.get(target.agent_id))
            if not pending:
                return  # 配達前に drain 済み / 失効でキュー破棄 (再ナッジ不要)
            try:
                state = classify_pane_state(self.adapter.get_text(pane_id))
            except Exception as e:  # adapter 不通は nudge_failed 相当
                self._journal(
                    "nudge_failed", agent_id=target.agent_id, error=str(e)
                )
                return
            if state == "idle":
                # 打鍵直前にロック下で active + pending を再確認する: get_text 〜
                # send_line の間に revoke/suspend/TTL 失効が入ると失効 pane に
                # ナッジを打ちうる (codex round 2 Major-A 対応)。本再確認で窓を
                # send_line 自体の I/O 時間まで縮める (それ以上はロックを I/O 越しに
                # 保持しない方針のため許容する残余窓。混入しても本文非経由の定型 1 行)。
                with self._lock:
                    still_ok = (
                        target.is_active()
                        and bool(self._queues.get(target.agent_id))
                    )
                if not still_ok:
                    return
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
        """ツール実行。引数不正は ToolArgError (handler 側で -32602 に変換)。

        冒頭で bind の有効性を再検証する: HTTP 経路は do_POST の authorize() で
        既に弾かれるが、revoke/TTL 前に取得した stale AgentBind を直呼びする経路
        (server-side / テスト) では check_messages / set_summary が素通りしうる。
        authorize() を「単一権限判定点」とするため、ここでも auth_error を尊重する
        (codex Major 対応)。
        """
        auth_err = bind.auth_error()
        if auth_err is not None:
            return {
                "content": [{"type": "text", "text": f"[{auth_err}] token not active"}],
                "isError": True,
            }
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
                        if b.registered and b.is_active()
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
                # 登録も落とす: 切断済み client を list_peers / 配送先に
                # 残さない (codex review round 3 Major 対応)
                bind.registered = False
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
        bind, auth_err = self.broker.authorize(token)
        if bind is None:
            # auth_err: token_invalid(未知) / token_revoked(失効) / token_expired(TTL)
            self._send_json(
                401,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": f"[{auth_err}] unauthorized"},
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
    ap.add_argument(
        "--backend", choices=("wezterm", "tmux"), default=None,
        help="terminal backend (省略時は OS から自動選択: POSIX=tmux / Windows=wezterm)",
    )
    ns = ap.parse_args()
    b = Broker(state_dir=ns.state_dir, adapter=make_adapter(ns.backend), port=ns.port)
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
