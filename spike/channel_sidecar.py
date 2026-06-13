# -*- coding: utf-8 -*-
"""tool-less claude/channel sidecar (broker-native-roles.md §9.2 / §9.3 / §9.5)。

K1 ゲートの核心オブジェクト。**ツール宣言ゼロ**で `experimental{claude/channel}` のみを
宣言する stdio MCP サーバー。daemon (k1_daemon.py) を ~1s で claim->push し、受信を
`notifications/claude/channel` でセッションへ in-band 注入する。

なぜ tool-less が K1 の核心か (§9.5):
- prior art (claude-peers-mcp server.ts) は **単一サーバーに 4 tools + channel を同梱**しており、
  tool-less 単独 channel サーバーの先例が無い。これを `--dangerously-load-development-channels`
  下で harness が load し、idle セッションを起こせるかは未検証の load-bearing 仮定。
- 副次的だが決定的な利点: このサーバーは check_messages を含む **いかなるツールも公開しない**ため、
  注入されたセッションには「能動 poll する手段が存在しない」。よって本文がターンに現れたら、
  それは **push 以外にありえない** (poll 混入の余地ゼロ = idle-wake-via-push の反証可能な証明)。

trust 境界 (§9.4): sidecar には agent の full token ではなく **delivery-scoped credential** のみを
env で渡す。daemon 側で /poll-claims と /confirm-delivered・to_id==owner の行のみに制限される。

stdio transport: 改行区切り JSON-RPC (1 メッセージ 1 行、埋め込み改行なし)。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request

DAEMON_URL = os.environ.get("K1_DAEMON_URL", "").rstrip("/")
DELIVERY_CRED = os.environ.get("K1_DELIVERY_CRED", "")
OWNER = os.environ.get("K1_OWNER", "")
POLL_INTERVAL = float(os.environ.get("K1_POLL_INTERVAL", "1.0"))
SOURCE_NAME = os.environ.get("K1_SOURCE_NAME", "org-broker-channel")
LOG_PATH = os.environ.get("K1_SIDECAR_LOG", "")
# テスト専用 fault injection: "skip-confirm" = emit はするが confirm しない
# (emit と confirm の間で sidecar が死亡したケースの再現。lease reaping の回復を検証する)
FAULT = os.environ.get("K1_FAULT", "")

_stdout_lock = threading.Lock()
_started = threading.Event()

# MCP protocolVersion negotiation（blind mirror を避ける）
_SUPPORTED_PROTO = frozenset((
    "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05",
))
_DEFAULT_PROTO = "2025-06-18"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # stderr は claude が mcp-logs に拾う。ファイル指定があれば証跡用に併記。
    print(line, file=sys.stderr, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _write_message(obj: dict) -> None:
    """JSON-RPC メッセージを stdout へ 1 行で書く (改行区切り transport)。"""
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _stdout_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _emit_channel(content: str, meta: dict) -> None:
    """claude/channel push 通知を emit。これが idle セッションを起こす in-band 注入。"""
    _write_message({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content, "meta": meta},
    })


# ----------------------------------------------------------------- daemon I/O
def _daemon_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        DAEMON_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DELIVERY_CRED}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"{}")


# ----------------------------------------------------------------- push loop
def _push_loop() -> None:
    """~1s で daemon を claim->emit->confirm する配送トランスデューサ (§9.3)。

    配達確定 (/confirm-delivered) は emit が成功した *後* にのみ行う。これにより
    sidecar が emit 途中で死んでも当該行は lease 失効で UNDELIVERED へ戻り (daemon 側 reaping)、
    lost-message window が閉じる。
    """
    _started.wait()   # client の initialized を待ってから配送開始
    _log(f"push loop start: daemon={DAEMON_URL} owner={OWNER} interval={POLL_INTERVAL}s")
    while True:
        try:
            res = _daemon_post("/poll-claims", {"owner": OWNER})
            rows = res.get("rows", [])
            for row in rows:
                meta = dict(row.get("meta") or {})
                # source は dev-channel 登録名から harness が付与する。meta では from_* 等 + dedup key。
                # msg_id = daemon 行 id。emit/confirm 残余 window や epoch flip での再配達を受信側が
                # 識別できる dedup key（at-least-once + 冪等表示の前提を実体化）。
                meta["msg_id"] = row["id"]
                _emit_channel(row["content"], meta)
                _log(f"emitted row {row['id']} ({len(row['content'])} bytes)")
                if FAULT == "skip-confirm":
                    _log(f"FAULT skip-confirm: not confirming {row['id']} (simulating death)")
                    continue
                # 配達確定は emit (stdout flush) の後にのみ。confirm 失敗時は再配達されうるため結果を検査する。
                conf = _daemon_post("/confirm-delivered",
                                    {"id": row["id"], "epoch": row.get("epoch", -1)})
                if conf.get("ok"):
                    _log(f"confirmed row {row['id']}")
                else:
                    # 既に emit 済。stale_epoch (PUSH->PULL flip) 等で行は UNDELIVERED へ戻り pull/次 push で
                    # 再配達されうる (msg_id で受信側 dedup 可能)。沈黙喪失ではなく重複側に倒れる。
                    _log(f"WARN confirm not ok for {row['id']}: {conf} (may redeliver; dedup via msg_id)")
        except Exception as exc:    # daemon 一時停止等でクラッシュさせない
            _log(f"poll error: {exc}")
        time.sleep(POLL_INTERVAL)


# ----------------------------------------------------------------- JSON-RPC
def _handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        # tool-less: capabilities に experimental{claude/channel} のみ。tools を宣言しない。
        # protocolVersion は blind mirror せず、既知サポート版なら同調・未知なら既定へ negotiate。
        want = (msg.get("params") or {}).get("protocolVersion", _DEFAULT_PROTO)
        proto = want if want in _SUPPORTED_PROTO else _DEFAULT_PROTO
        _log(f"initialize (client={want} -> negotiated={proto}) -> declaring tool-less claude/channel")
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"experimental": {"claude/channel": {}}},
                "serverInfo": {"name": SOURCE_NAME, "version": "0.1.0-k1"},
                "instructions": (
                    "This is a tool-less push channel. Messages arrive as "
                    "<channel source=\"" + SOURCE_NAME + "\"> tags injected into your "
                    "turn. There is no tool to call; just act on the content."
                ),
            },
        }

    if method == "notifications/initialized":
        _started.set()        # client ready -> push loop 開始
        _log("client initialized -> push loop armed")
        return None           # 通知には応答しない

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    # tool-less だが防御的に空で応答 (capability 未宣言なら通常 client は呼ばない)
    if method in ("tools/list",):
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": []}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}

    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None   # 未知の通知は無視


def main() -> None:
    if not (DAEMON_URL and DELIVERY_CRED and OWNER):
        _log("FATAL: K1_DAEMON_URL / K1_DELIVERY_CRED / K1_OWNER must be set in env")
        sys.exit(2)
    threading.Thread(target=_push_loop, daemon=True).start()
    _log(f"sidecar up: source={SOURCE_NAME}")
    for raw in sys.stdin.buffer:
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            # 不正バイトの 1 行で transport を落とさない (channel を維持)
            _log("bad stdin bytes (skipped)")
            continue
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log(f"bad json: {line[:120]}")
            continue
        resp = _handle(msg)
        if resp is not None:
            _write_message(resp)
    _log("stdin closed -> exit")


if __name__ == "__main__":
    main()
