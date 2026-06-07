# -*- coding: utf-8 -*-
"""AC-2 自動検証: 起動・接続チェーンの置き換え成立 (設計書 §7.1 AC-2)。

検証 4 項目:
  AC-2-1: --mcp-config 注入で spawn した対話ペインの Claude が broker MCP に
          接続できる。信頼確認プロンプトが出る場合は機械承認可能である。
  AC-2-2: per-agent token の受け渡し・認証が成立し、broker が from 帰属を
          token から正しく付与する。
  AC-2-3: broker の bind 表ベース登録検知が spawn 後 〜30 秒で成立する。
  AC-2-4: Windows (ConPTY) での send-text に文字化け・取りこぼしがない。

結果は broker-state/ac2/result.json に保存し、RESULTS.md へ転記する。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import AGENT_ID, OBSERVER_ID, SpikeSession, log  # noqa: E402

OUT = Path(__file__).parent / "broker-state" / "ac2"
OUT.mkdir(parents=True, exist_ok=True)

# ConPTY 文字化け検証用 (日本語 + 全角記号 + 半角カナ + 絵文字 + サロゲートペア)
MOJIBAKE_PROBE = "日本語テスト：ConPTY経由①②③ｱｲｳ🎌𠮷"

results: dict[str, dict] = {}


def record(item: str, go: bool, detail: str) -> None:
    results[item] = {"go": go, "detail": detail}
    log(f"{'GO  ' if go else 'NO-GO'} {item}: {detail}")


def main() -> int:
    s = SpikeSession(state_dir=OUT / "state")
    s.start()
    s.spawn_claude()
    try:
        # ---- AC-2-1: 接続成立 + 信頼確認の機械承認可否 --------------------
        ready = s.wait_ready(timeout=120)
        (OUT / "screen-after-ready.txt").write_text(s.screen(), encoding="utf-8")
        if not ready:
            record("AC-2-1", False, "TUI が idle に到達しなかった (120s timeout)")
            return finish(s, 1)

        # ---- AC-2-3: 登録検知 (bind 表ベース、〜30s) ----------------------
        registered = s.wait_registered(timeout=30)
        from_spawn = s.obs.registered_seconds
        if registered:
            record(
                "AC-2-3", True,
                f"initialize 到達で bind 登録。spawn から {from_spawn:.1f}s "
                f"(うち起動プロンプト処理 {s.obs.ready_seconds:.1f}s)",
            )
        else:
            record("AC-2-3", False, "30s 以内に bind 登録が観測できなかった")

        prompt_note = (
            f"folder trust prompt={'出現・機械承認' if s.obs.folder_trust_prompt else '出現せず'}, "
            f"MCP trust prompt={'出現・機械承認' if s.obs.mcp_trust_prompt else '出現せず'}"
        )
        record("AC-2-1", registered, f"MCP 接続 {'成立' if registered else '不成立'}。{prompt_note}")

        # ---- AC-2-4: ConPTY send-text 文字化け検証 ------------------------
        s.type_text(MOJIBAKE_PROBE)
        time.sleep(1.5)
        scr = s.screen()
        (OUT / "screen-mojibake.txt").write_text(scr, encoding="utf-8")
        intact = MOJIBAKE_PROBE in scr.replace("\n", "")  # 折返し跨ぎは結合で吸収
        if not intact:
            # 折返しの padding 空白を除去し、連続部分文字列として再判定
            # (文字の「どこかに出現」では順序・欠落を検出できないため不可。
            #  codex review Minor 対応)
            import re
            intact = MOJIBAKE_PROBE in re.sub(r"\s+", "", scr)
        record(
            "AC-2-4", intact,
            f"probe='{MOJIBAKE_PROBE}' が入力欄に{'無傷で出現' if intact else '化け/欠落'}",
        )
        s.clear_input()
        time.sleep(1.0)

        # ---- AC-2-2: token 帰属 (Claude → observer 方向) ------------------
        s.prompt(
            f"org-broker の send_message ツールを to_id={OBSERVER_ID}, "
            "message=PING-AC2 で 1 回だけ呼んでください。他の操作は不要です。"
        )
        got = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            obind = s.broker.get_bind(s.observer_token)
            msgs = s.broker.drain(obind)
            if msgs:
                got = msgs[0]
                break
            time.sleep(2)
        (OUT / "screen-after-send.txt").write_text(s.screen(), encoding="utf-8")
        if got and got["from_id"] == AGENT_ID and "PING-AC2" in got["message"]:
            record(
                "AC-2-2", True,
                f"from_id='{got['from_id']}' (token 由来) で observer に到達",
            )
        else:
            record("AC-2-2", False, f"未到達 or 帰属不正: {got}")

        # ---- 一往復の閉じ: observer → nudge → check_messages --------------
        # journal は append-only で過去 run の分が残るため、今回 run の
        # イベントのみを enqueue 前の長さ n0 からのスライスで判定する
        # (codex review round 2 Major 対応: 全履歴走査は再実行で偽陽性)
        s.wait_state("idle", timeout=60)
        n0 = len(s.journal_events())
        sent = s.observer_send("REPLY-AC2: broker queue 経由の本文 (PTY 非経由)")
        drained = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any(
                e["event"] == "queue_drained" and e.get("agent_id") == AGENT_ID
                for e in s.journal_events()[n0:]
            ):
                drained = True
                break
            time.sleep(2)
        (OUT / "screen-after-nudge.txt").write_text(s.screen(), encoding="utf-8")
        nudge_ev = [e for e in s.journal_events()[n0:] if e["event"].startswith("nudge")]
        record(
            "AC-2-roundtrip", drained,
            f"ナッジ→check_messages 一往復 {'成立' if drained else '不成立'} "
            f"(nudge events: {[e['event'] for e in nudge_ev]})",
        )
        return finish(s, 0 if all(r["go"] for r in results.values()) else 1)
    except Exception as e:
        log(f"EXCEPTION: {e!r}")
        try:
            (OUT / "screen-exception.txt").write_text(s.screen(), encoding="utf-8")
        except Exception:
            pass
        record("AC-2-harness", False, f"harness exception: {e!r}")
        return finish(s, 2)


def finish(s: SpikeSession, code: int) -> int:
    (OUT / "result.json").write_text(
        json.dumps(
            {
                "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "results": results,
                "startup_observation": vars(s.obs),
                "go": all(r["go"] for r in results.values()) and bool(results),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    s.teardown(kill_pane=True)
    log(f"result.json written, exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
