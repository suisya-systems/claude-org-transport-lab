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
import re
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


def make_wake_probe(channel_tag: str) -> dict:
    """反証可能な wake プローブを 1 つ作る。

    advisor + 事前 adversarial review の指摘への対処: 「push 本文に nonce を verbatim で
    埋めて `nonce in screen` で判定」すると、**注入メッセージの echo（`← src: …<nonce>`）が
    描画された瞬間に** モデルのターン無しでも substring が一致してしまう（echo confound）。
    そこで grep 対象を push 本文に **構造的に存在しない変換後トークン** にする:

      - base = 小文字 hex（英字を必ず含む）。push 本文には base（小文字）を載せ、
        「これを **大文字** にして 1 行で出力せよ」と指示する。
      - 検出対象 target = base.upper()。**target は本文（小文字 base）に部分文字列として現れない**ため、
        画面に target が出現する = モデルが実際にターンを起こして変換出力した、以外にありえない。
        注入 echo（小文字）では決して一致しない。tool-less ゆえツール poll 経路も無い。
    """
    base = uuid.uuid4().hex[:8]
    while base.upper() == base:        # 英字を含み大文字化で必ず変わることを保証
        base = uuid.uuid4().hex[:8]
    target = base.upper()
    content = (
        f"A push message arrived on channel {channel_tag}. Acknowledge by outputting "
        f"ONLY this token converted to UPPERCASE, on its own line, nothing else: {base}"
    )
    return {"base": base, "target": target, "content": content}


def scan_wake(screen: str, target: str) -> dict:
    """target（大文字変換後トークン）が **注入行以外** に出現したかを判定。

    注入メッセージ行は TUI で `←`（incoming）プレフィックスで描画される。target は本文に
    無いので出現自体が model turn の証拠だが、念のため `←` 行を除外して assistant 出力上の
    出現も別途記録する（belt-and-suspenders）。
    """
    appeared = target in screen
    on_non_injection_line = any(
        target in ln and not ln.lstrip().startswith("←")
        for ln in screen.splitlines()
    )
    return {"appeared": appeared, "on_assistant_line": on_non_injection_line}


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
    idle_streak = 0          # 連続 idle フレーム数（単一フレームの誤検出を避ける settle 要件）
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
            idle_streak = 0
        elif classify_pane_state(screen) == "idle":
            idle_streak += 1
            # settle: 2 連続 idle フレームを要求（プロンプト遷移中の単一フレーム誤検出を排除）
            if idle_streak >= 2:
                obs["ready_seconds"] = round(now - t0, 1)
                obs["idle_settle_frames"] = idle_streak
                log(f"claude reached idle (settled {idle_streak} frames) in {obs['ready_seconds']}s")
                return True, obs
        else:
            idle_streak = 0
        time.sleep(1.0)
    return False, obs


def ps_argv_for_claude(cfg_path: str) -> list[str]:
    """**自分が spawn した** claude プロセスの実 argv を ps で採取 (課金中立 attestation)。

    重要: 本マシンには renga 組織の別 claude セッションが多数走っているため、単に
    'argv[0]==claude' で拾うと **他セッションの argv（Bearer token を含みうる）を誤取得**する。
    自分の一意な `--mcp-config <cfg_path>`（scratch のファイルパス）を含む行のみに限定し、
    かつ取り違え防止に dev-channel flag の存在も要求する。自分の argv に平文 token は無い
    (token は config ファイルの env 側。argv はクリーン)。
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
        if (toks[0].rsplit("/", 1)[-1] == "claude"
                and cfg_path in toks
                and "--dangerously-load-development-channels" in toks):
            return toks
    return []


SPIKE_DIR = Path(__file__).resolve().parent
REPO_EVIDENCE = SPIKE_DIR / "k1-evidence"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def write_committed_evidence(name: str, after: str, result: dict, targets: list[str]) -> Path:
    """wake 証跡を repo 追跡ディレクトリへ **PII 除去のうえ** 抜粋保存（再現可能・durable）。

    生 pane ダンプは claude の welcome box にユーザーの email/氏名を含むため repo には入れない。
    wake を区別する load-bearing 行（注入 `←` / アシスタント出力 `●` / 変換後 target /
    `Baked`/`Cooking` スピナー / idle `❯`）だけを email 伏字で残す。
    """
    out = REPO_EVIDENCE / name
    out.mkdir(parents=True, exist_ok=True)
    keep = []
    for ln in after.splitlines():
        s = ln.strip()
        if (s.startswith("←") or s.startswith("●") or "Baked" in s or "Cooking" in s
                or "❯" in s or any(t in ln for t in targets)
                or "inject directly in this session" in s):
            keep.append(_EMAIL_RE.sub("<redacted-email>", ln.rstrip()))
    (out / "wake-excerpt.txt").write_text(
        "# K1 wake 証跡（抜粋・email 伏字）。生ダンプは実行時 /tmp/claude/broker-k1-spike に生成\n"
        f"# transform targets（push 本文に存在しない＝モデル出力でのみ一致）: {targets}\n\n"
        + "\n".join(keep) + "\n", encoding="utf-8")
    (out / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


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
        real_argv = ps_argv_for_claude(str(cfg_path))
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

        # --- AC2: 反証可能な idle-wake（transform プローブ） ---
        probe = make_wake_probe(SOURCE_NAME)
        result["probe"] = {"base": probe["base"], "target": probe["target"]}
        # 重要: idle 到達後、pane には *一切入力しない*（send-keys/type を一切呼ばない）。push のみで起こす。
        log(f"enqueue transform probe (owner={OWNER}); base={probe['base']} "
            f"target={probe['target']}; NO input will be typed")
        t_enq = time.monotonic()
        d.enqueue(OWNER, probe["content"], {"from_id": "observer", "kind": "k1-wake"})

        deadline = time.monotonic() + wake_timeout
        scan = {"appeared": False, "on_assistant_line": False}
        while time.monotonic() < deadline:
            scr = adapter.get_text(pane.pane_id)
            scan = scan_wake(scr, probe["target"])
            if scan["appeared"]:
                break
            time.sleep(1.0)
        after = adapter.get_text(pane.pane_id)
        (evid / "wake-after.txt").write_text(after, encoding="utf-8")

        dump = d.dump()
        delivered = sum(1 for r in dump["rows"] if r["state"] == "DELIVERED")
        result["ac2_idle_wake"] = {
            # target（大文字変換後）は push 本文に無いため、出現 = モデルが実ターンで変換出力した証拠
            "transform_target_emitted_by_model": scan["appeared"],
            "target_on_assistant_line": scan["on_assistant_line"],
            "no_input_typed_after_idle": True,   # 構造的不変: idle 後 send-keys/type/enter を一切呼ばない
            "tool_less_no_poll_path": True,      # sidecar はツール非公開 = check_messages 等が存在しない
            "channel_source_rendered": SOURCE_NAME in after,
            "daemon_rows_delivered": delivered,
            "seconds_to_wake": round(time.monotonic() - t_enq, 1) if scan["appeared"] else None,
            "pass": bool(scan["appeared"]),
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
    targets = [res.get("probe", {}).get("target", "")]
    after_path = out_dir / "wake-after.txt"
    after = after_path.read_text(encoding="utf-8") if after_path.exists() else ""
    committed = write_committed_evidence("isolation", after, res, [t for t in targets if t])
    print(f"committed evidence: {committed}")
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
