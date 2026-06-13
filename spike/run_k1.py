# -*- coding: utf-8 -*-
"""K1 HARD ゲート 実機ハーネス (broker-native-roles.md §9.5 / ja-migration-plan §8 K1)。

実 claude TUI を tmux に spawn し、tool-less な claude/channel sidecar を
`--dangerously-load-development-channels` で load して、idle セッションが
**能動 poll なしに** push で起きるかを実機検証する。

AC (全 PASS 必須):
  AC1 tool-less claude/channel を spawn 経路で load + dev-channel 機械承認
  AC2 idle セッションに daemon queue->sidecar claim->push で in-band 注入し、能動 poll なしに起きる
  AC3 renga と coexist (別ハーネス run_k1_coexist.py)
  AC4 課金中立: 対話 TUI・実 argv attestation

反証可能な wake (advisor 指摘): push する本文に固有 nonce の出力を要求し、
  (a) pane に一切 *入力していない* かつ (b) nonce を出す新しいアシスタントターンが出現
を二値で観測する。tool-less ゆえ poll 手段がそもそも無いので、nonce 出現 = push 以外ありえない。

無課金にはできない (実 claude を起こすのが AC2 の本体)。対話 1 ターン・最小トークン。
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
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from k1_daemon import DaemonServer  # noqa: E402
from tmux_adapter import TmuxAdapter  # noqa: E402
from terminal_adapter import classify_pane_state  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

SIDECAR = str(Path(__file__).parent / "channel_sidecar.py")
SOURCE_NAME = "org-broker-channel"
OWNER = "claude-spike"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def approve_prompts_until_idle(adapter, pane_id, t0, timeout=120.0, evidence_dir=None):
    """起動プロンプト (folder trust / dev-channel 危険確認 / MCP 承認) を機械承認し idle 到達。

    どのプロンプトが何文言で出るかは実測対象なので、画面が変わるたびにログし、
    既知プロンプトを認識して適切なキーで承認する。戻り値: (idle_reached, observations)。
    """
    deadline = time.monotonic() + timeout
    last_screen = ""
    last_action = 0.0
    obs = {"folder_trust": False, "dev_channel_prompt": False,
           "mcp_approve_prompt": False, "approved_mechanically": False,
           "ready_seconds": None, "prompt_texts": []}
    dump_idx = 0
    while time.monotonic() < deadline:
        screen = adapter.get_text(pane_id)
        low = screen.lower()
        now = time.monotonic()
        if screen != last_screen:
            last_screen = screen
            if evidence_dir:
                (evidence_dir / f"startup-{dump_idx:02d}.txt").write_text(
                    screen, encoding="utf-8")
            dump_idx += 1
        # --- known prompts ---
        is_folder_trust = ("trust this folder" in low
                           or "is this a project you created" in low
                           or "do you trust the files" in low)
        is_dev_channel = ("development channel" in low
                          or "dangerously-load" in low
                          or ("channel" in low and "load" in low and
                              ("y/n" in low or "yes" in low or "allow" in low)))
        is_mcp_approve = ("mcp server" in low and ("trust" in low or "approve" in low
                          or "use this" in low or "allow this server" in low))
        if (is_folder_trust or is_dev_channel or is_mcp_approve) and now - last_action > 2.5:
            tag = ("folder_trust" if is_folder_trust else
                   "dev_channel_prompt" if is_dev_channel else "mcp_approve_prompt")
            obs[tag] = True
            # 直近プロンプト画面の末尾数行を記録
            tail = "\n".join(ln for ln in screen.splitlines() if ln.strip())[-600:]
            obs["prompt_texts"].append({"kind": tag, "tail": tail})
            log(f"prompt detected [{tag}] -> approving")
            # 既定が Yes でないプロンプト (dangerously) に備え、"1" 選択も試みる。
            # 多くは Enter で既定 (Yes/trust) を採れる。dev-channel 危険確認のみ明示選択。
            if is_dev_channel:
                # "1. Yes" を選んでから Enter (既定が No のケースを潰す)
                adapter._tmux("send-keys", "-t", str(pane_id), "1")
                time.sleep(0.3)
            adapter.send_enter(pane_id)
            obs["approved_mechanically"] = True
            last_action = now
        elif classify_pane_state(screen) == "idle":
            obs["ready_seconds"] = round(now - t0, 1)
            log(f"claude reached idle in {obs['ready_seconds']}s")
            return True, obs
        time.sleep(1.0)
    return False, obs


def ps_argv_for_pane(pane_id: str) -> list[str]:
    """実 claude プロセスの argv を ps で採取 (課金中立 attestation)。

    tmux ラッパー行ではなく、**argv[0] の basename が claude** の実プロセス行を採る
    (= 起動された claude 本体の実 argv。headless flag 不在の直接証跡)。
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:
        return [f"ps_error:{exc}"]
    for line in out.splitlines():
        toks = line.split()
        if not toks:
            continue
        # tmux ラッパー / sidecar は除外。argv[0] が claude 本体の行のみ採る。
        if toks[0].rsplit("/", 1)[-1] == "claude" and "--mcp-config" in toks:
            return toks
    return []


def run_isolation(model: str, wake_timeout: float) -> dict:
    """AC1 + AC2 + AC4: tool-less channel-only で idle-wake を反証可能に検証。"""
    state = Path("/tmp/claude/broker-k1-spike/isolation")
    if state.exists():
        shutil.rmtree(state, ignore_errors=True)
    evid = state / "evidence"
    evid.mkdir(parents=True, exist_ok=True)

    srv = DaemonServer(state, lease_seconds=5.0)
    srv.start()
    d = srv.daemon
    delivery_cred = d.issue_cred(OWNER, "delivery")
    log(f"daemon up at {srv.url}")

    adapter = TmuxAdapter()
    scratch = Path(tempfile.mkdtemp(prefix="k1-iso-"))
    sidecar_log = str(state / "sidecar.log")
    mcp_cfg = {
        "mcpServers": {
            SOURCE_NAME: {
                "command": sys.executable,
                "args": [SIDECAR],
                "env": {
                    "K1_DAEMON_URL": srv.url,
                    "K1_DELIVERY_CRED": delivery_cred,
                    "K1_OWNER": OWNER,
                    "K1_POLL_INTERVAL": "1.0",
                    "K1_SOURCE_NAME": SOURCE_NAME,
                    "K1_SIDECAR_LOG": sidecar_log,
                },
            }
        }
    }
    cfg_path = scratch / "mcp-config.json"
    cfg_path.write_text(json.dumps(mcp_cfg), encoding="utf-8")

    claude = shutil.which("claude")
    argv = [
        claude,
        "--mcp-config", str(cfg_path),
        "--strict-mcp-config",
        "--dangerously-load-development-channels", f"server:{SOURCE_NAME}",
        "--model", model,
    ]
    log(f"spawn argv: {' '.join(argv)}")
    t0 = time.monotonic()
    pane = adapter.spawn(argv, cwd=str(scratch), new_window=True)
    log(f"spawned pane {pane.pane_id}")

    result: dict = {"ac": "isolation", "argv": argv, "pane": pane.pane_id}
    try:
        idle, obs = approve_prompts_until_idle(adapter, pane.pane_id, t0,
                                               evidence_dir=evid)
        result["startup"] = obs
        result["ac1_tool_less_load_and_approve"] = bool(
            idle and obs["approved_mechanically"])

        if not idle:
            result["ac2_idle_wake"] = False
            result["note"] = "did not reach idle; see evidence startup dumps"
            return result

        # --- AC4 billing attestation: 実 argv に headless flag が無いこと ---
        real_argv = ps_argv_for_pane(pane.pane_id)
        headless = [f for f in ("-p", "--print", "--headless", "--output-format",
                                "--input-format")
                    if f in real_argv]
        idle_screen = adapter.get_text(pane.pane_id)
        result["ac4_billing_neutral"] = {
            "real_argv": real_argv,
            "headless_flags_present": headless,
            "interactive_prompt_rendered": "❯" in idle_screen or ">" in idle_screen,
            "pass": (len(headless) == 0 and bool(real_argv)),
        }
        (evid / "idle-before.txt").write_text(idle_screen, encoding="utf-8")

        # --- AC2: 反証可能な idle-wake ---
        nonce = f"WOKE-K1-{uuid.uuid4().hex[:8]}"
        result["nonce"] = nonce
        # 重要: idle 到達後、pane には *一切入力しない*。push のみで起こす。
        content = (
            "A message arrived on your push channel. To acknowledge, output this "
            f"exact token on its own line, nothing else: {nonce}"
        )
        log(f"enqueue nonce elicitation (owner={OWNER}); NO input will be typed")
        d.enqueue(OWNER, content, {"from_id": "observer", "kind": "k1-wake"})

        deadline = time.monotonic() + wake_timeout
        woke = False
        while time.monotonic() < deadline:
            scr = adapter.get_text(pane.pane_id)
            if nonce in scr:
                woke = True
                break
            time.sleep(1.0)
        after = adapter.get_text(pane.pane_id)
        (evid / "wake-after.txt").write_text(after, encoding="utf-8")

        # daemon 側で行が DELIVERED まで進んだか
        dump = d.dump()
        delivered = sum(1 for r in dump["rows"] if r["state"] == "DELIVERED")
        result["ac2_idle_wake"] = {
            "nonce_appeared_in_session": woke,
            "no_input_typed": True,           # 構造的: idle 後 send-keys/type は呼ばない
            "channel_source_rendered": f'source="{SOURCE_NAME}"' in after
                                       or SOURCE_NAME in after,
            "daemon_rows_delivered": delivered,
            "seconds_to_wake": (round(time.monotonic() - (deadline - wake_timeout), 1)
                                if woke else None),
            "pass": bool(woke),
        }
        return result
    finally:
        try:
            adapter.kill_pane(pane.pane_id)
        except Exception:
            pass
        srv.stop()
        # tmux session 後始末は kill_pane が担う。scratch は痕跡として残さない。
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="K1 push idle-wake HARD gate (real claude TUI).")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--wake-timeout", type=float, default=45.0)
    args = ap.parse_args()

    res = run_isolation(args.model, args.wake_timeout)
    out_dir = Path("/tmp/claude/broker-k1-spike/isolation/evidence")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=2, ensure_ascii=False))

    ac1 = res.get("ac1_tool_less_load_and_approve")
    ac2 = (res.get("ac2_idle_wake") or {})
    ac2 = ac2.get("pass") if isinstance(ac2, dict) else ac2
    ac4 = (res.get("ac4_billing_neutral") or {}).get("pass")
    print(f"\nAC1 (tool-less load + approve): {'PASS' if ac1 else 'FAIL'}")
    print(f"AC2 (idle wake via push)      : {'PASS' if ac2 else 'FAIL'}")
    print(f"AC4 (billing neutral)         : {'PASS' if ac4 else 'FAIL'}")
    return 0 if (ac1 and ac2 and ac4) else 1


if __name__ == "__main__":
    raise SystemExit(main())
