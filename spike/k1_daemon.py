# -*- coding: utf-8 -*-
"""K1 spike daemon — push 一次配送のデリバリ権威 (broker-native-roles.md §9.3 / §9.4)。

本書 §9.2 の「daemon(権威)+ per-session channel sidecar(配送)」のうち **daemon** を
K1 ゲート用に最小実装する。spike/broker.py の成熟版とは別に、K1 が検証する配送
ライフサイクル (UNDELIVERED -> CLAIMED(lease) -> DELIVERED) と delivery-scoped
credential、mode-epoch fencing だけを自己完結で実装し、ゲート判定を読みやすく保つ。

実装する設計点 (broker-native-roles.md §9.3 / §9.4):
- claim-with-lease: /poll-claims が UNDELIVERED 行を CLAIMED(lease, owner, epoch) にして返す
- 配達確定は emit の *後*: sidecar は emit が resolve した行だけ /confirm-delivered する
- lease reaping: confirm されないまま lease 失効した行は UNDELIVERED へ戻す (sidecar 死亡時の
  lost-message window を閉じる)
- delivery-scoped credential: /poll-claims と /confirm-delivered のみ・to_id==owner の行のみ許可。
  全ツール/tier 操作を持たない (§9.4 least-privilege)
- mode-epoch fencing: PUSH->PULL flip 時に epoch を進め、旧 epoch の stale な drain/confirm を拒否
- check_messages (pull フォールバック): claim-respecting view を 1 トランザクションでドレイン

ASCII-only CLI 文字列 (cp932 コンソール安全)。localhost bind のみ。隔離 state-dir。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- row states
UNDELIVERED = "UNDELIVERED"
CLAIMED = "CLAIMED"
DELIVERED = "DELIVERED"

PUSH = "PUSH"
PULL = "PULL"


@dataclass
class Row:
    id: str
    to_id: str
    content: str
    meta: dict
    state: str = UNDELIVERED
    lease_until: float = 0.0
    owner: str | None = None
    claim_epoch: int = -1
    reclaim_count: int = 0
    enqueued_at: float = 0.0


@dataclass
class Cred:
    """credential bind。scope=delivery は配送専用、scope=full は agent / admin。"""

    token: str
    owner: str           # delivery: 配送対象の to_id。full: agent_id。admin: "admin"
    scope: str           # "delivery" | "full" | "admin"


@dataclass
class Daemon:
    state_dir: Path
    lease_seconds: float = 5.0
    delivery_mode: str = PUSH
    epoch: int = 0
    rows: dict[str, Row] = field(default_factory=dict)
    creds: dict[str, Cred] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # --------------------------------------------------------------- journal
    def _journal(self, event: str, **kw) -> None:
        rec = {"ts": round(time.time(), 3), "event": event, **kw}
        path = self.state_dir / "k1-queue.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ credentials
    def issue_cred(self, owner: str, scope: str) -> str:
        token = f"{scope}-{uuid.uuid4().hex}"
        with self._lock:
            self.creds[token] = Cred(token=token, owner=owner, scope=scope)
        return token

    def _auth(self, token: str | None) -> Cred | None:
        if not token:
            return None
        with self._lock:           # creds は issue_cred がロック下で書くため読みもロック下に揃える
            return self.creds.get(token)

    # --------------------------------------------------------------- enqueue
    def enqueue(self, to_id: str, content: str, meta: dict) -> str:
        rid = uuid.uuid4().hex[:12]
        with self._lock:
            self.rows[rid] = Row(
                id=rid, to_id=to_id, content=content, meta=dict(meta or {}),
                enqueued_at=round(time.time(), 3),
            )
            # journal もロック下で（並行 poll_claims の 'claimed' が 'enqueue' を追い越し、
            # 証跡 jsonl の lifecycle 順序が乱れるのを防ぐ）
            self._journal("enqueue", id=rid, to_id=to_id, bytes=len(content))
        return rid

    # ------------------------------------------------------------- reaping
    def _reap_locked(self) -> None:
        """lease 失効した CLAIMED 行を UNDELIVERED へ戻す (sidecar 死亡回復)。"""
        now = time.time()
        for row in self.rows.values():
            if row.state == CLAIMED and row.lease_until < now:
                row.state = UNDELIVERED
                row.owner = None
                row.reclaim_count += 1
                self._journal("lease_reaped", id=row.id, reclaim=row.reclaim_count)

    # ----------------------------------------------------------- poll-claims
    def poll_claims(self, cred: Cred) -> dict:
        """delivery-scoped credential で owner 宛 UNDELIVERED 行を claim して返す。"""
        if cred.scope != "delivery":
            return {"error": "forbidden_scope", "rows": []}
        with self._lock:
            if self.delivery_mode != PUSH:
                # PUSH->PULL flip 後は新規 claim 発行を拒否 (§9.3 claim-issuance ゲート)
                return {"error": "push_disabled", "rows": [], "epoch": self.epoch}
            self._reap_locked()
            now = time.time()
            claimed = []
            for row in self.rows.values():
                if row.state == UNDELIVERED and row.to_id == cred.owner:
                    row.state = CLAIMED
                    row.lease_until = now + self.lease_seconds
                    row.owner = cred.owner
                    row.claim_epoch = self.epoch
                    claimed.append({
                        "id": row.id, "content": row.content,
                        "meta": row.meta, "epoch": self.epoch,
                    })
            if claimed:
                self._journal("claimed", owner=cred.owner,
                              ids=[c["id"] for c in claimed], epoch=self.epoch)
            return {"rows": claimed, "epoch": self.epoch}

    # ------------------------------------------------------- confirm-delivered
    def confirm_delivered(self, cred: Cred, rid: str, epoch: int) -> dict:
        """emit が resolve した行を DELIVERED に確定 (id で冪等)。"""
        if cred.scope != "delivery":
            return {"ok": False, "error": "forbidden_scope"}
        with self._lock:
            # lease 失効した claim を先に reap（reaper は poll 系でしか走らないため、ここで
            # 明示的に回す）。これにより「lease 切れの stale claim を confirm で DELIVERED 化」
            # を構造的に閉じる: 失効 claim は UNDELIVERED へ戻り、後段の state==CLAIMED 検査で拒否される。
            self._reap_locked()
            row = self.rows.get(rid)
            if row is None:
                return {"ok": False, "error": "unknown_row"}
            if row.to_id != cred.owner:
                return {"ok": False, "error": "not_owner"}
            if epoch != self.epoch:
                # stale epoch (mode flip があった) -> 再 eligible にして拒否
                if row.state == CLAIMED:
                    row.state = UNDELIVERED
                    row.owner = None
                self._journal("confirm_stale_epoch", id=rid,
                              row_epoch=epoch, cur=self.epoch)
                return {"ok": False, "error": "stale_epoch", "epoch": self.epoch}
            if row.state == DELIVERED:
                return {"ok": True, "idempotent": True}   # 冪等
            # §9.3 不変条件: confirm は **live な claim** に紐づくことを daemon が強制する。
            # 未 claim(UNDELIVERED) / lease reap 後 / 別 owner・別 epoch の claim を確定できない。
            if (row.state != CLAIMED or row.owner != cred.owner
                    or row.claim_epoch != epoch):
                return {"ok": False, "error": "not_claimed",
                        "state": row.state, "owner": row.owner}
            row.state = DELIVERED
            self._journal("delivered", id=rid, owner=cred.owner)
            return {"ok": True}

    # ----------------------------------------------------- check_messages(pull)
    def check_messages(self, cred: Cred) -> dict:
        """pull フォールバック。claim-respecting view を 1 txn でドレイン (§9.3)。

        UNDELIVERED-and-unclaimed + lease 失効で reclaim 済 の行のみ返し、即 DELIVERED 化。
        live な sidecar claim とは二重配達しない。並行 check_messages も二重ドレインしない。
        """
        if cred.scope not in ("full", "admin"):
            return {"messages": [], "error": "forbidden_scope"}
        with self._lock:
            self._reap_locked()
            out = []
            for row in self.rows.values():
                if row.state == UNDELIVERED and row.to_id == cred.owner:
                    row.state = DELIVERED
                    out.append({"id": row.id, "content": row.content,
                                "meta": row.meta})
            if out:
                self._journal("pull_drained", owner=cred.owner,
                              ids=[m["id"] for m in out])
            return {"messages": out}

    # ----------------------------------------------------------- mode flip
    def flip_mode(self, mode: str) -> dict:
        with self._lock:
            old = self.delivery_mode
            if mode != old:
                self.delivery_mode = mode
                self.epoch += 1          # mode-epoch fencing
                # flip 時に in-flight CLAIMED を UNDELIVERED に戻す (原子的 flip)
                for row in self.rows.values():
                    if row.state == CLAIMED:
                        row.state = UNDELIVERED
                        row.owner = None
                self._journal("mode_flip", old=old, new=mode, epoch=self.epoch)
            return {"mode": self.delivery_mode, "epoch": self.epoch}

    # --------------------------------------------------------------- dump
    def dump(self) -> dict:
        with self._lock:
            self._reap_locked()
            by_state: dict[str, int] = {}
            for row in self.rows.values():
                by_state[row.state] = by_state.get(row.state, 0) + 1
            return {
                "mode": self.delivery_mode, "epoch": self.epoch,
                "by_state": by_state,
                "rows": [
                    {"id": r.id, "to_id": r.to_id, "state": r.state,
                     "owner": r.owner, "reclaim": r.reclaim_count}
                    for r in self.rows.values()
                ],
            }


# ----------------------------------------------------------------- HTTP layer
def make_handler(daemon: Daemon):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401 - silence default logging
            pass

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if n == 0:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _cred(self) -> Cred | None:
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            return daemon._auth(token)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            body = self._body()
            cred = self._cred()

            if path == "/enqueue":
                if cred is None or cred.scope not in ("admin", "full"):
                    return self._send(401, {"error": "unauthorized"})
                rid = daemon.enqueue(body["to_id"], body["content"],
                                     body.get("meta", {}))
                return self._send(200, {"id": rid})

            if path == "/poll-claims":
                if cred is None:
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, daemon.poll_claims(cred))

            if path == "/confirm-delivered":
                if cred is None:
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, daemon.confirm_delivered(
                    cred, body["id"], int(body.get("epoch", -1))))

            if path == "/check-messages":
                if cred is None:
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, daemon.check_messages(cred))

            if path == "/flip-mode":
                if cred is None or cred.scope != "admin":
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, daemon.flip_mode(body["mode"]))

            if path == "/dump":
                # 横断トポロジ（owner/state）を晒すため admin scope に限定（§9.4 least-privilege:
                # delivery-scoped cred からは到達不能）。ハーネスは in-process で daemon.dump() を呼ぶ。
                if cred is None or cred.scope != "admin":
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, daemon.dump())

            return self._send(404, {"error": "not_found"})

    return Handler


class DaemonServer:
    """ThreadingHTTPServer で daemon を localhost に上げる。port=0 で空きポート。"""

    def __init__(self, state_dir: Path, lease_seconds: float = 5.0):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.daemon = Daemon(state_dir=state_dir, lease_seconds=lease_seconds)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.daemon))
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _main() -> None:
    ap = argparse.ArgumentParser(description="K1 spike push-delivery daemon (standalone).")
    ap.add_argument("--state-dir", required=True,
                    help="isolated state dir (repo-external WSL path)")
    ap.add_argument("--lease-seconds", type=float, default=5.0)
    args = ap.parse_args()
    srv = DaemonServer(Path(args.state_dir), lease_seconds=args.lease_seconds)
    srv.start()
    admin = srv.daemon.issue_cred("admin", "admin")
    (Path(args.state_dir) / "daemon.json").write_text(
        json.dumps({"url": srv.url, "admin": admin}), encoding="utf-8")
    print(f"K1 daemon listening on {srv.url} (admin token in daemon.json)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    _main()
