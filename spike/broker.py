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
    normalize_key,
)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "org-broker-spike", "version": "0.1.0"}

# ---------------------------------------------------------------------------
# role-scoped tool 公開 (設計書 §4.2)。tier で公開面を変え、worker/curator から
# pane 操作を tools/list にも出さず・呼べなくする (許可設定ではなく構造的遮断)。
# ---------------------------------------------------------------------------
TIER_MESSAGING = 0  # worker / curator
TIER_OPS = 1        # dispatcher / secretary

# role → tier。未知 role は最小権限 (messaging) に倒す (fail-safe)。
ROLE_TIER = {
    "worker": TIER_MESSAGING,
    "curator": TIER_MESSAGING,
    "dispatcher": TIER_OPS,
    "secretary": TIER_OPS,
}

# spawn_agent が発行できる新 token の role (caller tier 昇格を構造的に禁じる)。
SPAWNABLE_ROLES = ("worker", "curator")

# 課金中立 (§1.3): spawn する Claude は対話型 TUI のみ。ヘッドレス / Agent-SDK 起動を
# 構造的に禁じる。spawn_agent はこれらの flag を含む argv を `[headless_forbidden]` で拒否し、
# 「ヘッドレスに落ちない」を caller 任せではなく broker が強制する (AC-5 課金中立)。
# 完全一致で弾く flag と、`=value` 形を吸収する prefix の 2 系統で判定する。
_HEADLESS_EXACT = frozenset(("-p", "--print", "--headless"))
_HEADLESS_PREFIX = ("--output-format", "--input-format", "--print=")


def is_interactive_argv(argv: list[str]) -> bool:
    """argv が対話 TUI 起動か (ヘッドレス / print / Agent-SDK 系 flag を含まない)。

    blacklist 層。token 注入 spawn では下記 allowlist (is_interactive_claude_argv) と二重で使い、
    値位置に紛れた headless flag (`--model -p` 等) も弾く。非 claude プローブ路では単独で使う。
    """
    for tok in argv:
        if tok in _HEADLESS_EXACT:
            return False
        if any(tok.startswith(p) for p in _HEADLESS_PREFIX):
            return False
    return True


# 課金中立 (§1.3) — token を注入する org agent の spawn を許す **対話 claude TUI flag allowlist**
# (default-deny)。allowlist 外の token (未知 flag・非 TUI サブコマンド・bare word・`--`・flag 後の
# サブコマンド) は一律拒否され、これにより「flag 後サブコマンド」等の理論バイパスも構造的に閉じる。
# headless 系 (-p/--print/--output-format/--input-format) は意図的に allowlist 外。
#
# **保守契約 (重要)**: claude CLI が新しい正規の対話 flag を追加した場合、本 allowlist を拡張するまで
# その flag を伴う正規起動は false-reject される。拒否時のエラーメッセージが allowlist 拡張を促す。
# 拡張手順は spike/RESULTS.md (Phase 5 / AC-5 節) と docs/design/renga-decoupling.md (§7.6) を参照。
_CLAUDE_TUI_VALUE_FLAGS = frozenset((  # 値を 1 つ取る対話 flag
    "--mcp-config", "--allowedTools", "--allowed-tools", "--disallowedTools",
    "--disallowed-tools", "--model", "--permission-mode", "--add-dir",
    "--append-system-prompt", "--settings", "--setting-sources", "--resume",
    "--session-id", "--agents",
))
_CLAUDE_TUI_BOOL_FLAGS = frozenset((  # 値を取らない対話 flag
    "--strict-mcp-config", "--dangerously-skip-permissions", "--ide",
    "--continue", "-c", "--verbose", "--debug", "--fork-session",
))


def is_interactive_claude_argv(argv: list[str]) -> tuple[bool, str]:
    """token 注入 spawn 用の **対話 claude TUI allowlist 判定** (default-deny)。

    argv[0] が claude かつ、以降の token が対話 flag allowlist (+ その値) のみで構成されることを要求する。
    返り値: (ok, 拒否理由)。ok=False の理由は呼び手が `[headless_forbidden]` エラーに載せる。
    """
    if not argv:
        return False, "argv must be non-empty"
    if argv[0].rsplit("/", 1)[-1] != "claude":
        return False, f"argv[0] must be 'claude' (got {argv[0]!r})"
    i = 1
    while i < len(argv):
        tok = argv[i]
        flag = tok.split("=", 1)[0]
        inline = tok.startswith("-") and "=" in tok
        if flag in _CLAUDE_TUI_BOOL_FLAGS:
            if inline:
                return False, f"boolean flag に値が付与されている: {tok!r}"
            i += 1
        elif flag in _CLAUDE_TUI_VALUE_FLAGS:
            if inline:
                i += 1          # --flag=value 形は 1 token
            elif i + 1 < len(argv):
                i += 2          # 次 token を値として消費する
            else:
                return False, f"値を取る flag に値が無い: {tok!r}"
        else:
            return False, (f"allowlist 外の token: {tok!r} — 対話 claude TUI の正規 flag のみ許可 "
                           "(許可するには broker の対話 flag allowlist を拡張)")
    return True, ""


def role_tier(role: str) -> int:
    return ROLE_TIER.get(role, TIER_MESSAGING)


# ASCII の `[A-Za-z0-9_-]` のみを許す集合。str.isalnum() は Unicode 英数字も通すため
# 明示集合で ASCII 契約に揃える (codex Minor 対応)。
_FILENAME_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def is_filename_safe(s: str) -> bool:
    """`[A-Za-z0-9_-]` (ASCII) のみ・非空。agent_id を config ファイル名に使う際の path
    traversal (`../`・絶対パス) を構造的に防ぐ (Set D name 文字種に整合)。"""
    return bool(s) and all(c in _FILENAME_SAFE_CHARS for c in s)


# messaging tier (worker/curator 含む全 role) に公開する 4 面。
_MESSAGING_TOOLS = [
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

# ops tier (dispatcher / secretary) のみに公開する pane 操作面 (Phase 4 の 6 面 +
# Surface 1.8 継承の set_pane_identity)。worker/curator の token では tools/list に
# 現れず、call_tool でも [tool_forbidden] で弾かれる。
_OPS_TOOLS = [
    {
        "name": "spawn_agent",
        "description": "Balanced-split spawn a new agent pane (dispatcher/secretary only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "name": {"type": "string"},
                "role": {"type": "string", "enum": list(SPAWNABLE_ROLES)},
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "target": {"type": "integer", "description": "pane handle (省略=balanced split)"},
                "direction": {"type": "string", "enum": ["vertical", "horizontal"]},
            },
            "required": ["agent_id", "name", "role", "argv"],
        },
    },
    {
        "name": "close_pane",
        "description": "Close a pane by handle and revoke its token (dispatcher/secretary only).",
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "integer"}},
            "required": ["target"],
        },
    },
    {
        "name": "list_panes",
        "description": "Enumerate panes with geometry + role (dispatcher/secretary only).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "inspect_pane",
        "description": "Grid-scrape a pane's screen (dispatcher/secretary only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "integer"},
                "lines": {"type": "integer"},
                "include_cursor": {"type": "boolean"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "send_keys",
        "description": "Write raw PTY input to a pane (dispatcher/secretary only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "integer"},
                "text": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "enter": {"type": "boolean"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "poll_events",
        "description": "Long-poll synthesized pane lifecycle events (dispatcher/secretary only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "timeout_ms": {"type": "integer"},
                "types": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "set_pane_identity",
        "description": "Rename/relabel a pane's name/role (dispatcher/secretary only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "integer"},
                "name": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": ["target"],
        },
    },
]

# tool 名 → 必要 tier。tools/list と call_tool の単一判定表。
TOOL_TIER = {t["name"]: TIER_MESSAGING for t in _MESSAGING_TOOLS}
TOOL_TIER.update({t["name"]: TIER_OPS for t in _OPS_TOOLS})

# 互換: 旧 import `from broker import TOOLS` を壊さない (全 tool の列挙)。
TOOLS = _MESSAGING_TOOLS + _OPS_TOOLS


def tools_for_role(role: str) -> list[dict]:
    """role tier で公開する tool 一覧 (tools/list のフィルタ)。"""
    tier = role_tier(role)
    return [t for t in TOOLS if TOOL_TIER[t["name"]] <= tier]


class ToolArgError(ValueError):
    """tools/call の引数不正 (JSON-RPC -32602 invalid params に変換される)。"""


@dataclass
class AgentBind:
    """token ↔ agent/pane の bind (設計書 §4.4)。broker のみが保持する。"""

    token: str
    agent_id: str
    name: str
    role: str
    # 権限 tier を決める **不変** role。issue_token でのみ設定し set_pane_identity からは
    # 変更不可 (codex Blocker 対応: 可変 role でのツール権限昇格を構造的に断つ)。
    # `role` は list_peers / list_panes 表示用の可変ラベル (Set D §1.8) であり、権限判定は
    # auth_role のみを見る。空文字は最小権限 (messaging) に倒れる (role_tier の fail-safe)。
    auth_role: str = ""
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
        event_cap: int = 1000,
        event_poll_interval: float = 0.5,
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

        # --- Phase 4: poll_events 合成 + pane handle (全て self._lock 下で扱う) ---
        self.event_cap = event_cap                  # ring 上限 (超過分は drop)
        self.event_poll_interval = event_poll_interval
        self._events: list[dict] = []               # 単調 seq 昇順のイベント列
        self._event_seq = 0                         # 直近採番 seq
        self._dropped_total = 0                      # ring trim で捨てた累計
        # 既知 pane の record map: native_id -> {name, role, agent_id}。
        # id 集合ではなく map にすることで pane_exited 後も meta を payload に載せる
        # (exit 後は list_panes から name/role を復元できない。codex Major 対応)。
        self._known_panes: dict[PaneId, dict] = {}
        self._baseline_done = False                 # 初回 = 「今以降」(履歴 replay 無し)
        # native pane id ↔ broker handle (Set D 数値 id 面)。MCP は handle で話し、
        # 内部 (bind / adapter) は native を使う。choose_split も handle を id に取る。
        self._pane_handles: dict[PaneId, int] = {}
        self._handle_to_native: dict[int, PaneId] = {}
        self._handle_seq = 0
        # reconcile は backend I/O (list_panes) を伴うため _lock の外で行い、診断の
        # 一意性は専用 lock で直列化する (codex Minor 対応: _lock を I/O 越しに保持
        # しない = auth/messaging/close を reconcile の遅延で詰まらせない)。
        self._reconcile_lock = threading.Lock()
        # pane_exited 合成時に revoke すべき native の保留集合。revoke_token は _lock を
        # 取り直すため、合成 (lock 下) では集めるだけにし lock 解放後に適用する。
        self._pending_exit_revokes: list[PaneId] = []

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
        reject_if_active: bool = False,
    ) -> str | None:
        """spawn 時の per-agent token 発行 (設計書 §4.4)。

        ttl を与えると発行時刻 + ttl 秒で失効する (None=失効なし)。設計書 §4.4 は
        「セッション寿命より長い TTL + 退役時 revoke」を基本とし、TTL は失効漏れの
        保険と位置付ける。suspend/resume をまたいだ token 再利用は不可 (resume は
        本メソッドの再呼出で別 token を再発行する。旧 token は revoke 済みのまま)。

        reject_if_active=True のとき、同一 agent_id / name の有効 bind が既に在れば
        **同一ロック下で** None を返す (発行しない)。check-then-act を避け、
        ThreadingHTTPServer 配下の並行 spawn での二重発行 race を構造的に断つ
        (queue は agent_id 単位の inbox なので二重 spawn は message 横取りを生む。
        codex round 4 Major 対応)。spawn_agent からのみ True で呼ぶ。
        """
        ttl = self.default_token_ttl if ttl is None else ttl
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self._lock:
            if reject_if_active and any(
                b.is_active(now) and (b.agent_id == agent_id or b.name == name)
                for b in self._binds.values()
            ):
                return None  # caller は [name_in_use] にマップする (発行も journal もしない)
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
                token=token, agent_id=agent_id, name=name, role=role,
                auth_role=role,  # 不変。権限 tier の唯一の根拠 (set_pane_identity で変わらない)
                pane_id=pane_id, issued_at=now,
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

    # ===================================================================
    # Phase 4: pane handle / poll_events 合成 / pane 操作 (ops tier)
    # ===================================================================

    # --- handle / target 解決 (全て self._lock 下で呼ぶ) -------------------
    def _handle_for_locked(self, native: PaneId) -> int:
        h = self._pane_handles.get(native)
        if h is None:
            self._handle_seq += 1
            h = self._handle_seq
            self._pane_handles[native] = h
            self._handle_to_native[h] = native
        return h

    def _pane_meta_locked(self, native: PaneId) -> dict:
        """native pane に bind された有効 agent の name/role/agent_id を解決。"""
        for b in self._binds.values():
            if b.pane_id == native and not b.revoked:
                return {"name": b.name, "role": b.role, "agent_id": b.agent_id}
        return {"name": None, "role": None, "agent_id": None}

    def _resolve_target(self, target) -> PaneId | None:
        """MCP target (broker handle = int / 全桁数字 str) を native pane id へ。

        Set D §4.1: 全桁数字は id 解釈。未知 handle は None (caller が
        pane_not_found 化)。MCP 面は handle で話し native を露出しない。
        """
        try:
            h = int(target)
        except (TypeError, ValueError):
            return None
        with self._lock:
            return self._handle_to_native.get(h)

    # --- poll_events 合成 (唯一の合成点。exactly-once は単一 lock で担保) ----
    def _emit_locked(self, etype: str, native: PaneId, meta: dict) -> int:
        """イベント 1 件を log に積む (単一 lock 下で唯一 emit。exactly-once 担保)。

        payload は **broker handle (`id`) のみ** を露出し native pane id は載せない
        (MCP 面は handle で話す。WezTerm の native int と handle int の取り違え回避。
        codex Major 対応)。返り値は採番した handle (exit 時の handle 掃除に使う)。
        """
        self._event_seq += 1
        handle = self._handle_for_locked(native)
        ev = {
            "seq": self._event_seq,
            "type": etype,
            "id": handle,                            # broker handle (Set D 数値 id)
            "name": meta.get("name"),
            "role": meta.get("role"),
            "agent_id": meta.get("agent_id"),
            "ts": time.time(),
        }
        self._events.append(ev)
        if len(self._events) > self.event_cap:
            overflow = len(self._events) - self.event_cap
            self._dropped_total += overflow
            del self._events[:overflow]
        return handle

    def _diff_emit_locked(self, raw: list[dict]) -> None:
        """list_panes スナップショット (raw) と _known_panes の差分を合成する (_lock 下)。

        I/O (list_panes) は呼出側が _lock の外で取得済み。本メソッドは差分計算と emit のみ。
        pane_exited 時は (1) revoke を保留集合へ積み (pane 退役時 revoke、§4.4)、
        (2) handle 対応を掃除する (native id 再利用で stale handle が再対応しない。codex Major 対応)。
        """
        current: dict[PaneId, dict] = {}
        for rec in raw:
            nid = rec.get("pane_id")
            if nid is None:
                continue
            current[nid] = self._pane_meta_locked(nid)
        if not self._baseline_done:
            self._known_panes = current
            self._baseline_done = True
            return
        for nid, meta in current.items():
            if nid not in self._known_panes:
                self._emit_locked("pane_started", nid, meta)
        for nid in list(self._known_panes):
            if nid not in current:
                # exit 後は meta を復元できないため直近スナップショットの meta を使う
                handle = self._emit_locked("pane_exited", nid, self._known_panes[nid])
                self._pending_exit_revokes.append(nid)  # lock 解放後に revoke
                self._handle_to_native.pop(handle, None)  # handle 対応を掃除
                self._pane_handles.pop(nid, None)
        self._known_panes = current

    def _apply_pending_revokes(self) -> None:
        """pane_exited で積んだ native の token を revoke する (_lock 解放後に呼ぶ)。

        pane 退役 = 即時 revoke (§4.4)。broker 非経由 kill / crash でも poll_events の
        reconcile が pane_exited を合成した時点で token を失効させ、漏洩面を閉じる
        (reap_exited_panes の明示呼出を待たない。codex Major 対応)。revoke は冪等なので
        close_pane で既に revoke 済みでも二重作用はしない。
        """
        with self._lock:
            pend = self._pending_exit_revokes
            self._pending_exit_revokes = []
        for native in pend:
            self.revoke_pane(native, reason="pane_exited")

    def _collect_events_locked(self, since, types) -> dict:
        """cursor (since) より後のイベントを返す (Set D §3.1 の cursor-based long-poll)。

        本面は **replayable な cursor 読み出し** であり破壊的 queue ではない: 同一 since の
        並行 poll は同じイベントを返す (caller が next_since でカーソルを前進させる renga と
        同型のセマンティクス)。Set D の exactly-once は「イベントの **emit** が close/crash
        ごとに 1 回」であり (それは _diff_emit_locked が単一 lock + _reconcile_lock 直列化で
        担保する)、「1 reader へ 1 回 deliver」ではない (cursor モデルは再読を許す)。
        """
        max_seq = self._event_seq
        if since is None:
            # 初回 = 「今以降」: 履歴を返さず現在 seq をカーソルにする (Set D §3.1)
            return {"events": [], "next_since": str(max_seq)}
        try:
            since_i = int(since)
        except (TypeError, ValueError):
            since_i = 0
        earliest = self._events[0]["seq"] if self._events else (max_seq + 1)
        if self._events and since_i < earliest - 1:
            # since 以降〜最古保持の手前までを取りこぼした (ring trim)。Set D §3.1:
            # count 付き events_dropped を返し、caller は list_panes reconcile する。
            dropped = (earliest - 1) - since_i
            ev = {
                "seq": earliest - 1, "type": "events_dropped",
                "count": dropped, "ts": time.time(),
            }
            return {"events": [ev], "next_since": str(earliest - 1)}
        evs = [e for e in self._events if e["seq"] > since_i]
        if types:
            evs = [e for e in evs if e["type"] in types]
        # types フィルタで全件落ちても next_since は max_seq まで進める
        # (filtered-out を越えてカーソル前進。Set D §3.1 の重複スキャン回避)
        return {"events": evs, "next_since": str(max_seq)}

    def poll_events(self, since=None, timeout_ms: int = 2000, types=None) -> dict:
        """合成イベントの cursor 付き long-poll (Set D Surface 3)。

        timeout_ms は 30000ms にクランプ。イベント発生で早期 return、無ければ
        interval で reconcile を回し timeout で空応答 + 前進カーソルを返す。
        """
        timeout = min(max(int(timeout_ms), 0), 30000) / 1000.0
        deadline = time.monotonic() + timeout
        while True:
            self._reconcile()  # backend I/O は _lock 外。差分 emit + revoke 適用を含む
            with self._lock:
                result = self._collect_events_locked(since, types)
            if result["events"] or timeout <= 0 or time.monotonic() >= deadline:
                return result
            time.sleep(min(self.event_poll_interval, max(deadline - time.monotonic(), 0)))

    def _reconcile(self) -> None:
        """list_panes 差分からイベントを合成する (唯一の合成点)。

        backend I/O (list_panes) は _lock の外で実行し、合成の一意性は _reconcile_lock で
        直列化する (二重 reconcile による pane_exited 二重 emit を防ぐ = exactly-once、
        かつ _lock を I/O 越しに保持しない)。adapter 不通時は何も合成しない (誤合成回避)。
        spawn/close 直後の即時合成や poll_events のループから呼ぶ。
        """
        if self.adapter is None:
            return
        with self._reconcile_lock:
            try:
                raw = self.adapter.list_panes()
            except Exception:
                return  # adapter_unavailable: 生存判定不能を「退役」と扱わない
            with self._lock:
                self._diff_emit_locked(raw)
        self._apply_pending_revokes()

    # --- pane 操作 (ops tier) ---------------------------------------------
    def mcp_list_panes(self) -> list[dict]:
        """geometry + role 付き pane 一覧 (Set D §1.5)。MCP 面は handle で話す。

        adapter の生 geometry (tmux: left/top, WezTerm: size) を正規化し、
        name/role は bind 表から付与する (adapter 自体は role を知らない)。
        """
        if self.adapter is None:
            raise ToolArgError("[adapter_unavailable] no terminal backend")
        raw = self.adapter.list_panes()  # backend I/O は _lock の外で取得する
        out: list[dict] = []
        with self._lock:
            for rec in raw:
                nid = rec.get("pane_id")
                if nid is None:
                    continue
                meta = self._pane_meta_locked(nid)
                # MCP 面は broker handle (`id`) のみで pane を指す。native id は露出しない
                # (WezTerm の native int と handle int の取り違え回避。codex Major 対応)。
                rc = {
                    "id": self._handle_for_locked(nid),
                    "name": meta["name"],
                    "role": meta["role"],
                    "focused": bool(rec.get("focused", rec.get("active", False))),
                    "x": int(rec.get("x", rec.get("left", 0))),
                    "y": int(rec.get("y", rec.get("top", 0))),
                    "width": int(rec["width"]),
                    "height": int(rec["height"]),
                }
                if "cursor_x" in rec:
                    rc["cursor_x"] = rec["cursor_x"]
                    rc["cursor_y"] = rec["cursor_y"]
                out.append(rc)
        return out

    def resolve_balanced_split(self, panes_records: list[dict]):
        """現行 renga 同等の balanced split (claude_org_runtime.choose_split 再利用)。

        最大ペイン選択ではなく、role priority / MIN_PANE / SECRETARY 保険 /
        dispatcher 隣接 / (priority desc, metric desc, id asc) sort / capacity 検出を
        含む runtime SoT をそのまま使う (prose は runtime と drift 済みのため移植不可)。
        返り値: SplitChoice | None (None=候補空=split_capacity_exceeded)。
        """
        from claude_org_runtime.dispatcher import runner  # lazy (declared dependency)

        panes = [
            runner.Pane(
                id=r["id"], name=r["name"], role=r["role"],
                focused=r["focused"], x=r["x"], y=r["y"],
                width=r["width"], height=r["height"],
            )
            for r in panes_records
        ]
        return runner.choose_split(panes)

    def _write_agent_mcp_config(self, agent_id: str, token: str) -> Path:
        """per-agent の mcp-config を 0600 で書き出す (§4.4 の token 受け渡し経路)。

        token は queue store / journal には平文で書かない方針 (§4.4) だが、起動引数
        `--mcp-config` 経由の受け渡しは §4.4 が明示的に認める 0600 一時ファイル経路。
        """
        cfg_dir = self.state_dir / "agents"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        path = cfg_dir / f"{agent_id}.mcp.json"
        path.write_text(
            json.dumps(self.mcp_config_for(token), ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            import os as _os
            _os.chmod(path, 0o600)
        except OSError:
            pass  # Windows 等で chmod 非対応でも config 自体は書ける
        return path

    def spawn_agent(
        self, agent_id: str, name: str, role: str, argv: list[str],
        cwd: str | None = None, target: int | None = None,
        direction: str | None = None, ttl: float | None = None,
        inject_mcp_config: bool = True,
    ) -> dict:
        """balanced split で新 agent pane を spawn + token 発行 + bind (設計書 §4.6)。

        起動チェーン (§4.6 段階 1): token を **先に** 発行し、per-agent の `--mcp-config`
        (0600) を起動 argv に注入してから split する。spawn された worker はこの config で
        broker に接続し initialize handshake で registered になる (登録検知は §4.6 段階 4)。
        起動チェーン自体は Phase 1/2 AC-2 で両 backend 実証済み。

        target 省略時は choose_split が geometry から split 対象/方向を決める。候補が無ければ
        split_capacity_exceeded を返す (spawn せず escalate 相当)。実 Claude を起動しない
        プローブ (cat 等) では inject_mcp_config=False を渡す (config を消費できないため)。
        """
        if self.adapter is None:
            return {"ok": False, "error": "[adapter_unavailable] no terminal backend"}
        if role not in SPAWNABLE_ROLES:
            return {"ok": False,
                    "error": f"[invalid-params] role must be one of {SPAWNABLE_ROLES}"}
        # 課金中立 (§1.3): 「対話 TUI のみ・ヘッドレスに落ちない」を broker が構造的に強制する
        # (caller 任せにしない)。argv は非空必須。
        if not argv:
            return {"ok": False, "error": "[invalid-params] argv must be non-empty"}
        # 二重防御: (1) headless flag blacklist (値位置に紛れた -p 等も弾く)、(2) token 注入経路は
        # 対話 claude TUI の **allowlist (default-deny)** で許可 flag のみ通す。allowlist が flag 後
        # サブコマンド・`--`・未知 flag・非 claude ラッパーを一律拒否し、課金を負う org agent の spawn を
        # 対話 TUI に構造的に限定する (codex round 1-4 / 人間判断で allowlist 化を選択)。非 claude
        # プローブ (inject_mcp_config=False, cat 等) は broker token を持たない = org agent でないため
        # allowlist の対象外とし、blacklist のみで通す。
        if not is_interactive_argv(argv):
            return {"ok": False,
                    "error": "[headless_forbidden] argv must launch an interactive TUI "
                             "(no -p/--print/--headless/--output-format)"}
        if inject_mcp_config:
            ok_tui, reason = is_interactive_claude_argv(argv)
            if not ok_tui:
                return {"ok": False, "error": f"[headless_forbidden] {reason}"}
        # agent_id は per-agent config のファイル名に使う。filename-safe を強制して
        # `../` / 絶対パスで state_dir 外へ token 入り config を書く経路を断つ (codex Major)。
        if not is_filename_safe(agent_id):
            return {"ok": False,
                    "error": "[name_invalid] agent_id must match [A-Za-z0-9_-]"}
        records = self.mcp_list_panes()
        if target is None:
            choice = self.resolve_balanced_split(records)
            if choice is None:
                return {"ok": False,
                        "error": "[split_capacity_exceeded] no balanced-split candidate"}
            target_native = self._resolve_target(choice.target_id)
            direction = choice.direction
        else:
            target_native = self._resolve_target(target)
            direction = direction or "vertical"
        if target_native is None:
            return {"ok": False, "error": "[pane_not_found] split target unresolved"}
        # token を先に発行し (§4.4 発行=spawn 要求時点)、起動 argv に config を注入する。
        # reject_if_active=True で「重複判定 + 予約」を単一ロック下に閉じ、並行 spawn の
        # 二重発行 race を断つ (codex round 4 Major 対応)。queue は agent_id 単位 inbox。
        tok = self.issue_token(agent_id, name, role, pane_id=None, ttl=ttl,
                               reject_if_active=True)
        if tok is None:
            return {"ok": False,
                    "error": f"[name_in_use] active agent_id/name '{agent_id}' exists"}
        launch_argv = list(argv)
        if inject_mcp_config:
            cfg_path = self._write_agent_mcp_config(agent_id, tok)
            launch_argv += ["--mcp-config", str(cfg_path)]
        try:
            ref = self.adapter.split(target_native, launch_argv, cwd=cwd, direction=direction)
        except Exception as e:
            # 失敗時は発行済み token を revoke して漏洩面を残さない。adapter 例外文字列は
            # argv (token-bearing になりうる) を含むため MCP 応答へ素通ししない (codex Major)。
            self.revoke_token(tok, reason="spawn_failed")
            self._journal("spawn_failed", agent_id=agent_id, error=str(e))
            return {"ok": False, "error": "[io_error] split failed"}
        self.bind_pane(tok, ref.pane_id)  # pane id 確定後に token↔pane を bind
        self._reconcile()  # 新 pane を pane_started として即時合成 + handle 採番
        with self._lock:
            handle = self._handle_for_locked(ref.pane_id)
        return {
            "ok": True, "pane_id": ref.pane_id, "handle": handle,
            "direction": direction, "token": tok,  # token / pane_id は MCP 応答で除去する
        }

    def inspect_pane(self, target, lines: int | None = None,
                     include_cursor: bool = False) -> dict:
        """pane の grid scrape (Set D §1.7)。lines で末尾 N 行トリム。"""
        if self.adapter is None:
            return {"ok": False, "error": "[adapter_unavailable] no terminal backend"}
        native = self._resolve_target(target)
        if native is None:
            return {"ok": False, "error": "[pane_not_found] unknown pane handle"}
        try:
            text = self.adapter.get_text(native)
        except Exception as e:
            self._journal("inspect_pane_failed", error=str(e))
            return {"ok": False, "error": "[io_error] get_text failed"}
        if lines is not None and lines >= 0:
            text = "\n".join(text.splitlines()[-lines:]) if lines else ""
        result = {"ok": True, "text": text,
                  "state": classify_pane_state(text)}
        if include_cursor:
            try:
                for rec in self.adapter.list_panes():
                    if rec.get("pane_id") == native and "cursor_x" in rec:
                        result["cursor"] = {"x": rec["cursor_x"], "y": rec["cursor_y"]}
                        break
            except Exception:
                pass  # cursor は best-effort (WezTerm 等は欠落)
        return result

    def send_keys_op(self, target, text=None, keys=None, enter: bool = False) -> dict:
        """raw PTY 入力 (Set D §1.9)。未知キー名は invalid-params。"""
        if self.adapter is None:
            return {"ok": False, "error": "[adapter_unavailable] no terminal backend"}
        native = self._resolve_target(target)
        if native is None:
            return {"ok": False, "error": "[pane_not_found] unknown pane handle"}
        # キー語彙は broker 側で検証する (Set D §1.9 invalid-params)。adapter 実装に
        # 依らず単一判定点とする (FakeAdapter 等は検証しないため)。
        if keys:
            try:
                for k in keys:
                    normalize_key(k)
            except ValueError as e:
                return {"ok": False, "error": f"[invalid-params] {e}"}
        try:
            self.adapter.send_keys(native, text=text, keys=keys, enter=enter)
        except ValueError as e:
            return {"ok": False, "error": f"[invalid-params] {e}"}
        except Exception as e:
            self._journal("send_keys_failed", error=str(e))
            return {"ok": False, "error": "[io_error] send_keys failed"}
        return {"ok": True}

    def set_pane_identity(self, target, name=None, role=None) -> dict:
        """pane の bind の **表示** name/role を更新 (Set D §1.8 継承)。name は検証する。

        ここで更新する `role` は list_peers / list_panes 表示用のラベルであり、権限 tier を
        決める `auth_role` (issue_token で確定・不変) には一切影響しない。ops token が worker
        pane に role="dispatcher" を設定しても、その worker の token は ops 面に昇格しない
        (codex Blocker 対応: 可変メタデータでの権限昇格を構造的に断つ)。
        """
        native = self._resolve_target(target)
        if native is None:
            return {"ok": False, "error": "[pane_not_found] unknown pane handle"}
        if name is not None:
            if not name or name.isdigit() or not all(
                c.isalnum() or c in "_-" for c in name
            ):
                return {"ok": False, "error": "[name_invalid] bad pane name"}
        with self._lock:
            for b in self._binds.values():
                if b.pane_id == native and not b.revoked:
                    if name is not None and any(
                        ob.name == name and ob.pane_id != native and not ob.revoked
                        for ob in self._binds.values()
                    ):
                        return {"ok": False, "error": "[name_in_use] name collision"}
                    if name is not None:
                        b.name = name
                    if role is not None:
                        b.role = role
                    return {"ok": True, "name": b.name, "role": b.role}
        return {"ok": False, "error": "[pane_not_found] no active bind for pane"}

    def close_pane_target(self, target) -> dict:
        """MCP close: handle → native 解決 + kill + revoke (Set D §1.4)。

        pane_exited は次の poll_events reconcile で合成される (pane が list_panes
        から消えるため。直 kill 取りこぼしと同一経路で構造的に回復)。
        """
        native = self._resolve_target(target)
        if native is None:
            return {"ok": False, "error": "[pane_not_found] unknown pane handle"}
        revoked = self.close_pane(native)  # 既存内部 API (kill + 生存確認 + revoke)
        # kill 失敗 / pane 残存時は close_pane が [] を返す。pane が実際に消えたかを
        # 確認し、残存していれば ok=False を返す (呼び手が ok だけで成否を判断できる。
        # codex Minor 対応)。生存判定不能 (adapter 不通) 時は close の意図を尊重し ok=True。
        try:
            still_alive = self.adapter.pane_exists(native) if self.adapter else False
        except Exception:
            still_alive = False
        if still_alive:
            return {"ok": False, "error": "[io_error] pane still alive after close",
                    "handle": int(target)}
        # MCP 面は handle で話す。native id は応答に載せない (handle 取り違え回避)。
        # `closed` は revoke した agent_id のリスト (pane id ではない)。
        return {"ok": True, "closed": revoked, "handle": int(target)}

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
        # role-scoped 公開 (設計書 §4.2): tier 外ツールは構造的に拒否する。
        # worker/curator の token では pane 操作が tools/list にも出ず、ここでも
        # [tool_forbidden] で弾かれる (許可設定ではなく権限分離)。
        required = TOOL_TIER.get(name)
        if required is not None and required > role_tier(bind.auth_role):
            return {
                "content": [{
                    "type": "text",
                    "text": f"[tool_forbidden] '{name}' not available to role '{bind.auth_role}'",
                }],
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
        elif name == "list_panes":
            result = {"panes": self.mcp_list_panes()}
        elif name == "inspect_pane":
            target = args.get("target")
            if target is None:
                raise ToolArgError("inspect_pane requires target")
            result = self.inspect_pane(
                target, lines=args.get("lines"),
                include_cursor=bool(args.get("include_cursor", False)),
            )
        elif name == "send_keys":
            target = args.get("target")
            if target is None:
                raise ToolArgError("send_keys requires target")
            result = self.send_keys_op(
                target, text=args.get("text"), keys=args.get("keys"),
                enter=bool(args.get("enter", False)),
            )
        elif name == "poll_events":
            result = self.poll_events(
                since=args.get("since"),
                timeout_ms=int(args.get("timeout_ms", 2000)),
                types=args.get("types"),
            )
        elif name == "close_pane":
            target = args.get("target")
            if target is None:
                raise ToolArgError("close_pane requires target")
            result = self.close_pane_target(target)
        elif name == "spawn_agent":
            for req in ("agent_id", "name", "role", "argv"):
                if args.get(req) is None:
                    raise ToolArgError(f"spawn_agent requires {req}")
            spawned = self.spawn_agent(
                args["agent_id"], args["name"], args["role"], list(args["argv"]),
                cwd=args.get("cwd"), target=args.get("target"),
                direction=args.get("direction"),
            )
            # token (漏洩面の限定) と native pane_id (handle と取り違え回避) は MCP 応答に
            # 載せない。dispatcher MCP client には handle (`id`) のみ返す。
            result = {k: v for k, v in spawned.items()
                      if k not in ("token", "pane_id")}
        elif name == "set_pane_identity":
            target = args.get("target")
            if target is None:
                raise ToolArgError("set_pane_identity requires target")
            result = self.set_pane_identity(
                target, name=args.get("name"), role=args.get("role")
            )
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
            # role tier で公開面をフィルタ (worker/curator には pane 操作を出さない)。
            # 不変の auth_role で判定する (set_pane_identity の表示 role 変更で動かない)。
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": req_id,
                 "result": {"tools": tools_for_role(bind.auth_role)}},
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
