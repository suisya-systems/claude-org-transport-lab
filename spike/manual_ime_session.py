# -*- coding: utf-8 -*-
"""AC-1 状態 2 (IME 変換中) 手動テスト用の対話セッション。

使い方は spike/manual-ime-test.md を参照。
コンソール側で Enter を押すたびに、5 秒後に observer → claude-spike 宛の
メッセージが enqueue され、ナッジ配達 (静止確認 + defer) が走る。
配達の経過は broker journal (nudge_deferred / nudge_sent / queue_drained) を
リアルタイム表示する。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import SpikeSession, log  # noqa: E402

OUT = Path(__file__).parent / "broker-state" / "manual-ime"
OUT.mkdir(parents=True, exist_ok=True)


def tail_journal(s: SpikeSession, stop: threading.Event) -> None:
    seen = 0
    while not stop.is_set():
        evs = s.journal_events()
        for e in evs[seen:]:
            if e["event"].startswith(("nudge", "message_", "queue_")):
                log(f"journal: {e['event']} "
                    + " ".join(f"{k}={v}" for k, v in e.items()
                               if k not in ("ts", "event")))
        seen = len(evs)
        time.sleep(0.5)


def main() -> int:
    s = SpikeSession(state_dir=OUT / "state")
    s.start()
    s.spawn_claude()
    if not s.wait_ready(timeout=120) or not s.wait_registered(timeout=30):
        log("セッション起動に失敗しました。run_ac2.py で接続チェーンを確認してください。")
        s.teardown(kill_pane=True)
        return 2
    log("準備完了。spawn された WezTerm ウィンドウで日本語 IME を有効にしてください。")
    stop = threading.Event()
    t = threading.Thread(target=tail_journal, args=(s, stop), daemon=True)
    t.start()
    n = 0
    try:
        while True:
            input(
                "\n=== Enter でナッジ配達を予約 (5 秒後に enqueue)。"
                "終了は Ctrl+C ===\n"
            )
            n += 1
            log("5 秒後に enqueue します。WezTerm 側で IME 状態を作ってください…")
            time.sleep(5)
            s.observer_send(f"IME-TEST-{n}: 手動テスト用メッセージ")
            log(f"enqueue 済み (IME-TEST-{n})。ナッジ配達の journal を観察してください。")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop.set()
        log("終了します。検証ペインを閉じ、broker を停止します。")
        s.teardown(kill_pane=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
