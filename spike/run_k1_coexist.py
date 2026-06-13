# -*- coding: utf-8 -*-
"""K1 AC-3 coexist 実機ハーネス (broker-native-roles.md §9.5(iii) / §9.1)。

「renga と coexist (同一マシンで renga 経路と broker push 経路が干渉しない)」を実機検証する。

検証構成 (2 系の claude/channel を 1 セッションに同居させる):
  - org-broker-channel : 本 spike の tool-less channel sidecar (k1_daemon backing)
  - claude-peers       : prior art (happy-ryo/claude-peers-mcp) の *実コード* を、
                         隔離した db/port/token で起動 (= renga と同型の channel 実装。
                         本番 ~/.claude-peers.db には一切触れない)

2 系それぞれに固有 nonce を push し、(a) 両方の `<channel source=...>` がセッションに
注入され (b) 互いを block せず (c) source で区別されることを観測する。
加えて本番 ~/.claude-peers.db の不可触 (machine-level non-interference) を attestation する。

無課金にはできない (実 claude の同居 wake が AC-3 の本体)。最小トークン。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from k1_daemon import DaemonServer  # noqa: E402
from tmux_adapter import TmuxAdapter  # noqa: E402
from run_k1 import (  # noqa: E402
    approve_prompts_until_idle, log, make_wake_probe, scan_wake,
    write_committed_evidence,
)

sys.stdout.reconfigure(encoding="utf-8")

SIDECAR = str(Path(__file__).parent / "channel_sidecar.py")
PRIOR_ART = "/home/happy_ryo/claude-peers-mcp"
OWNER = "claude-spike"
LIVE_PEERS_DB = str(Path.home() / ".claude-peers.db")  # 本番 (不可触)


def _free_port() -> int:
    """空きポートを 1 つ確保して返す（固定ポート衝突を避ける）。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _peers_post(port, token, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-peers-token": token},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def run_coexist(model: str, wake_timeout: float) -> dict:
    state = Path("/tmp/claude/broker-k1-spike/coexist")
    if state.exists():
        shutil.rmtree(state, ignore_errors=True)
    evid = state / "evidence"
    evid.mkdir(parents=True, exist_ok=True)

    result: dict = {"ac": "coexist"}

    # --- machine-level isolation baseline: 本番 db の mtime を採取 ---
    live_mtime_before = os.path.getmtime(LIVE_PEERS_DB) if os.path.exists(LIVE_PEERS_DB) else None

    broker_log = None
    broker_proc = None
    srv = None
    pane = None
    scratch = None
    adapter = TmuxAdapter()
    try:
        # --- (1) 隔離 claude-peers broker (実 prior art。空きポートで衝突回避) ---
        bun = str(Path.home() / ".bun/bin/bun")
        peers_port = str(_free_port())
        peers_db = str(state / "peers.db")
        peers_token_path = str(state / "peers-token")
        peers_env = dict(os.environ,
                         CLAUDE_PEERS_PORT=peers_port,
                         CLAUDE_PEERS_DB=peers_db,
                         CLAUDE_PEERS_TOKEN_PATH=peers_token_path,
                         PATH=f"{Path.home()}/.bun/bin:" + os.environ.get("PATH", ""))
        broker_log = open(state / "peers-broker.log", "w")
        broker_proc = subprocess.Popen([bun, "run", f"{PRIOR_ART}/broker.ts"],
                                       stdout=broker_log, stderr=subprocess.STDOUT,
                                       env=peers_env)
        # token file が書かれるまで poll（固定 sleep より堅牢）
        peers_token = None
        for _ in range(30):
            if Path(peers_token_path).exists():
                peers_token = Path(peers_token_path).read_text().strip()
                if peers_token:
                    break
            time.sleep(0.2)
        if not peers_token:
            raise RuntimeError("isolated claude-peers broker did not start (no token file)")
        log(f"isolated claude-peers broker up on :{peers_port} (db={peers_db})")

        # --- (2) 本 spike daemon ---
        srv = DaemonServer(state, lease_seconds=5.0)
        srv.start()
        delivery_cred = srv.daemon.issue_cred(OWNER, "delivery")
        log(f"k1 daemon up at {srv.url}")

        scratch = Path(tempfile.mkdtemp(prefix="k1-coexist-"))
        sidecar_log = str(state / "sidecar.log")
        mcp_cfg = {"mcpServers": {
            "org-broker-channel": {
                "command": sys.executable, "args": [SIDECAR],
                "env": {"K1_DAEMON_URL": srv.url, "K1_DELIVERY_CRED": delivery_cred,
                        "K1_OWNER": OWNER, "K1_POLL_INTERVAL": "1.0",
                        "K1_SOURCE_NAME": "org-broker-channel",
                        "K1_SIDECAR_LOG": sidecar_log}},
            "claude-peers": {
                "command": bun, "args": ["run", f"{PRIOR_ART}/server.ts"],
                "env": {"CLAUDE_PEERS_PORT": peers_port, "CLAUDE_PEERS_DB": peers_db,
                        "CLAUDE_PEERS_TOKEN_PATH": peers_token_path,
                        "PATH": f"{Path.home()}/.bun/bin:" + os.environ.get("PATH", ""),
                        "HOME": os.environ.get("HOME", "")}},
        }}
        cfg_path = scratch / "mcp-config.json"
        cfg_path.write_text(json.dumps(mcp_cfg), encoding="utf-8")

        claude = shutil.which("claude")
        argv = [claude, "--mcp-config", str(cfg_path), "--strict-mcp-config",
                "--dangerously-load-development-channels",
                "server:org-broker-channel", "server:claude-peers",
                "--model", model]
        log(f"spawn (both channels): {' '.join(argv)}")
        t0 = time.monotonic()
        pane = adapter.spawn(argv, cwd=str(scratch), new_window=True)
        result["argv"] = argv
        result["pane"] = pane.pane_id

        idle, obs = approve_prompts_until_idle(adapter, pane.pane_id, t0,
                                               evidence_dir=evid, timeout=140.0)
        result["startup"] = obs
        if not idle:
            result["note"] = "did not reach idle; see startup dumps"
            result["ac3_coexist"] = {"pass": False}
            return result

        # --- claude-peers 側: 合成 sender 登録 + テストセッション peer 発見 ---
        sender = _peers_post(peers_port, peers_token, "/register",
                             {"pid": os.getpid(), "cwd": "/tmp", "git_root": None,
                              "tty": None, "summary": "k1-coexist-sender"})["id"]
        # テストセッションの server.ts が自己登録するまで待つ
        target = None
        for _ in range(20):
            peers = _peers_post(peers_port, peers_token, "/list-peers",
                                {"scope": "machine", "exclude_id": sender})
            cand = [p for p in peers if p.get("id") != sender]
            if cand:
                target = cand[0]["id"]
                break
            time.sleep(1.0)
        result["claude_peers_target_registered"] = target is not None

        # --- 2 系へ transform プローブを push (pane には一切入力しない) ---
        # echo confound 対策: grep 対象は大文字変換後 target（push 本文に存在しない）。
        # 注入 echo（小文字）では一致せず、モデルが実ターンで変換出力したときのみ一致する。
        probe_a = make_wake_probe("org-broker-channel")
        probe_b = make_wake_probe("claude-peers")
        result["probe_a"] = {"base": probe_a["base"], "target": probe_a["target"]}
        result["probe_b"] = {"base": probe_b["base"], "target": probe_b["target"]}
        log(f"push A via org-broker-channel: base={probe_a['base']} target={probe_a['target']}")
        srv.daemon.enqueue(OWNER, probe_a["content"],
                           {"from_id": "observer-a", "kind": "coexist-a"})
        if target:
            log(f"push B via claude-peers: base={probe_b['base']} target={probe_b['target']}")
            _peers_post(peers_port, peers_token, "/send-message",
                        {"from_id": sender, "to_id": target, "text": probe_b["content"]})

        deadline = time.monotonic() + wake_timeout
        seen_a = seen_b = False
        while time.monotonic() < deadline and not (seen_a and seen_b):
            scr = adapter.get_text(pane.pane_id)
            seen_a = seen_a or scan_wake(scr, probe_a["target"])["appeared"]
            seen_b = seen_b or scan_wake(scr, probe_b["target"])["appeared"]
            time.sleep(1.0)
        after = adapter.get_text(pane.pane_id)
        (evid / "coexist-after.txt").write_text(after, encoding="utf-8")
        # committed evidence は result 完成後に main() で書く（pass/wake/mtime を含めるため）

        result["ac3_coexist"] = {
            # 両 channel がモデルを実際に起こした（transform 出力で実証）
            "channel_A_org_broker_woke": seen_a,
            "channel_B_claude_peers_woke": seen_b,
            # 非干渉: 両 source が同一セッションに load/inject された（wake とは別の観測）
            "both_sources_loaded_and_injected": ("org-broker-channel" in after
                                                 and "claude-peers" in after),
            "neither_blocked": seen_a and seen_b,
            "pass": seen_a and seen_b,
        }

        # --- machine-level non-interference attestation ---
        live_mtime_after = os.path.getmtime(LIVE_PEERS_DB) if os.path.exists(LIVE_PEERS_DB) else None
        result["machine_isolation"] = {
            "live_peers_db": LIVE_PEERS_DB,
            "live_db_mtime_unchanged": live_mtime_before == live_mtime_after,
            "isolated_db_used": peers_db,
            "note": "broker push path (isolated port/db) does not touch live renga db",
        }
        return result
    finally:
        if pane is not None:
            try:
                adapter.kill_pane(pane.pane_id)
            except Exception:
                pass
        if broker_proc is not None:
            broker_proc.terminate()
            try:
                broker_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                broker_proc.kill()
                broker_proc.wait(timeout=5)
        if broker_log is not None:
            broker_log.close()
        if srv is not None:
            srv.stop()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="K1 AC-3 coexist gate (two claude/channel servers).")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--wake-timeout", type=float, default=90.0)  # 2 系の実ターン分
    args = ap.parse_args()
    res = run_coexist(args.model, args.wake_timeout)
    out_dir = Path("/tmp/claude/broker-k1-spike/coexist/evidence")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    # committed durable evidence（result 完成後・PII 除去）
    after_path = out_dir / "coexist-after.txt"
    after = after_path.read_text(encoding="utf-8") if after_path.exists() else ""
    targets = [res.get("probe_a", {}).get("target", ""), res.get("probe_b", {}).get("target", "")]
    committed = write_committed_evidence("coexist", after, res, [t for t in targets if t])
    print(f"committed evidence: {committed}")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    ac3 = (res.get("ac3_coexist") or {}).get("pass")
    print(f"\nAC3 (coexist with renga-equivalent): {'PASS' if ac3 else 'FAIL'}")
    return 0 if ac3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
