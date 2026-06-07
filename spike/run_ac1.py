# -*- coding: utf-8 -*-
"""AC-1 自動判定: ナッジ注入の 4 状態テストのうち自動化可能な 3 状態 (設計書 §7.1 AC-1)。

自動化境界 (事前 codex design review 確定事項 (5)):
  状態 1 (idle) / 状態 3 (長文入力中) / 状態 4 (出力ストリーミング中) は
  get-text ヒューリスティックで自動判定する。
  状態 2 (IME 変換中) のみ手動手順書 (spike/manual-ime-test.md) で人間が実施
  する。根拠: get-text は PTY 内の文字 grid のみを観測し、IME の変換窓・
  候補 UI (OS 側オーバーレイ) を観測できないため。

状態 3 / 4 の合否は「きれいに注入できた」ことではなく
「静止確認が defer し、静止後に配達され、かつ取りこぼさない」こと
(defer-then-deliver) で判定する。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import AGENT_ID, SpikeSession, log  # noqa: E402
from wezterm_adapter import NUDGE_TEXT  # noqa: E402

OUT = Path(__file__).parent / "broker-state" / "ac1"
OUT.mkdir(parents=True, exist_ok=True)

results: dict[str, dict] = {}

# 状態 3 用の未送信長文 (複数行)。誤送信検知のためユニークなマーカーを含む。
UNSENT_MARKER = "UNSENT-AC1-MARKER"
UNSENT_TEXT = (
    f"これは未送信の長文入力です {UNSENT_MARKER}\n"
    "2 行目: この文章は送信してはならない\n"
    "3 行目: ナッジはこの入力が消えるまで defer されるべきである"
)


def record(item: str, go: bool, detail: str) -> None:
    results[item] = {"go": go, "detail": detail}
    log(f"{'GO  ' if go else 'NO-GO'} {item}: {detail}")


def events_since(s: SpikeSession, n0: int) -> list[dict]:
    return s.journal_events()[n0:]


def wait_event(s: SpikeSession, n0: int, name: str, timeout: float = 90.0, pred=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in events_since(s, n0):
            if (e["event"] == name and e.get("agent_id") == AGENT_ID
                    and (pred is None or pred(e))):
                return e
        time.sleep(1.0)
    return None


def main() -> int:
    s = SpikeSession(state_dir=OUT / "state")
    s.start()
    s.spawn_claude()
    try:
        if not s.wait_ready(timeout=120) or not s.wait_registered(timeout=30):
            record("AC-1-setup", False, "セッション起動に失敗 (AC-2 系を参照)")
            return finish(s, 2)
        log("session ready — starting state tests")

        # ============ 状態 1: idle ======================================
        n0 = len(s.journal_events())
        s.observer_send("STATE1: idle 配達テスト本文")
        sent_ev = wait_event(s, n0, "nudge_sent", timeout=30)
        drained = wait_event(s, n0, "queue_drained", timeout=90)
        scr = s.screen()
        (OUT / "screen-state1.txt").write_text(scr, encoding="utf-8")
        deferred = [e for e in events_since(s, n0) if e["event"] == "nudge_deferred"]
        nudge_in_history = NUDGE_TEXT in scr
        ok = bool(sent_ev) and bool(drained) and not deferred
        record(
            "AC-1-state1-idle", ok,
            f"nudge_sent={bool(sent_ev)} (defer 0 回), drain={bool(drained)}, "
            f"ナッジ 1 行が履歴に出現={nudge_in_history}",
        )
        s.wait_state("idle", timeout=60)

        # ============ 状態 3: 長文入力中 =================================
        n0 = len(s.journal_events())
        s.type_text(UNSENT_TEXT)          # paste (未送信のまま置く)
        time.sleep(1.0)
        scr_before = s.screen()
        state_seen = s.state()
        s.observer_send("STATE3: 長文入力中 defer テスト本文")
        # defer が観測されるまで待つ (静止確認が input_pending を検知すること)
        defer_ev = wait_event(s, n0, "nudge_deferred", timeout=20)
        time.sleep(4.0)                   # defer 継続中の挙動を観測
        scr_during = s.screen()
        (OUT / "screen-state3-during.txt").write_text(scr_during, encoding="utf-8")
        premature = [e for e in events_since(s, n0) if e["event"] == "nudge_sent"]
        text_intact = all(
            ln.split()[0] in scr_during.replace("\n", "")
            for ln in (UNSENT_MARKER,)
        ) and UNSENT_MARKER in scr_during
        nudge_mixed_in = NUDGE_TEXT in scr_during.split("STATE1")[-1] and bool(premature)
        # 入力欄を空にする → 配達されること (defer-then-deliver、取りこぼし無し)
        s.clear_input()
        sent_after = wait_event(s, n0, "nudge_sent", timeout=60)
        drained3 = wait_event(s, n0, "queue_drained", timeout=90)
        time.sleep(2.0)
        scr_after = s.screen()
        (OUT / "screen-state3-after.txt").write_text(scr_after, encoding="utf-8")
        # 未送信テキストが勝手に送信されていないこと: クリア後の画面に
        # マーカーが残存しない (送信済みなら会話履歴に現れる)
        not_autosent = UNSENT_MARKER not in scr_after
        ok = (
            state_seen == "input_pending"
            and bool(defer_ev)
            and not premature
            and text_intact
            and not nudge_mixed_in
            and bool(sent_after)
            and bool(drained3)
            and not_autosent
        )
        record(
            "AC-1-state3-long-input", ok,
            f"state={state_seen}, defer={bool(defer_ev)}, 入力中の早漏配達={len(premature)}件, "
            f"未送信文無傷={text_intact}, クリア後配達={bool(sent_after)}, "
            f"drain={bool(drained3)}, 勝手送信なし={not_autosent}",
        )
        s.wait_state("idle", timeout=60)

        # ============ 状態 4: 出力ストリーミング中 =======================
        n0 = len(s.journal_events())
        s.prompt(
            "1 から 40 までの整数を 1 行に 1 つずつ、コードブロックやツールを"
            "使わず本文として出力してください。前置きと後書きは不要です。"
        )
        if not s.wait_state("busy", timeout=30, interval=0.5):
            record("AC-1-state4-streaming", False,
                   "busy (応答生成中) 状態を捕捉できなかった")
            return finish(s, 1)
        s.observer_send("STATE4: ストリーミング中 defer テスト本文")
        # 静止確認が busy を検知して defer したこと (state=busy の defer に限定)
        defer_ev4 = wait_event(
            s, n0, "nudge_deferred", timeout=20,
            pred=lambda e: e.get("state") == "busy",
        )
        # 生成完了 (busy 解消) の時刻を記録する。早漏配達の判定は
        # 「観測時点の状態」ではなく nudge_sent イベントの ts と busy 終了
        # 時刻の比較で行う (codex review Blocker 対応: 観測時点フィルタでは
        # busy 中の誤配達を idle 復帰後の journal 読みで見逃す)。
        busy_end_wall: float | None = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if s.state() != "busy":
                busy_end_wall = time.time()
                break
            time.sleep(0.5)
        sent4 = wait_event(s, n0, "nudge_sent", timeout=90)
        # 0.75s は busy 終了観測の poll 遅れ (0.5s 間隔) を吸収する許容幅
        premature4 = [
            e for e in events_since(s, n0)
            if e["event"] == "nudge_sent" and e.get("agent_id") == AGENT_ID
            and busy_end_wall is not None and e["ts"] < busy_end_wall - 0.75
        ]
        drained4 = wait_event(s, n0, "queue_drained", timeout=120)
        time.sleep(2.0)
        scr4 = s.screen()
        (OUT / "screen-state4.txt").write_text(scr4, encoding="utf-8")
        # 出力破壊の検査: 数列の終端 (38,39,40) が揃って出力されていること
        flat = scr4.replace(" ", "")
        output_intact = all(f"\n{n}" in flat for n in ("38", "39", "40"))
        ok = (
            bool(defer_ev4)
            and busy_end_wall is not None
            and not premature4
            and bool(sent4)
            and bool(drained4)
            and output_intact
        )
        record(
            "AC-1-state4-streaming", ok,
            f"defer(busy)={bool(defer_ev4)}, busy 終了観測={busy_end_wall is not None}, "
            f"busy 中の早漏配達={len(premature4)}件 (ts 比較), "
            f"完了後配達={bool(sent4)}, drain={bool(drained4)} (取りこぼし無し), "
            f"出力末尾無傷={output_intact}",
        )
        return finish(s, 0 if all(r["go"] for r in results.values()) else 1)
    except Exception as e:
        log(f"EXCEPTION: {e!r}")
        try:
            (OUT / "screen-exception.txt").write_text(s.screen(), encoding="utf-8")
        except Exception:
            pass
        record("AC-1-harness", False, f"harness exception: {e!r}")
        return finish(s, 2)


def finish(s: SpikeSession, code: int) -> int:
    (OUT / "result.json").write_text(
        json.dumps(
            {
                "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "results": results,
                "go_3states": all(r["go"] for r in results.values()) and bool(results),
                "note": "状態 2 (IME 変換中) は手動: spike/manual-ime-test.md",
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
