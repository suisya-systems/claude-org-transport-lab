# -*- coding: utf-8 -*-
"""起動シーケンス観測 probe (使い捨て): TUI 描画の実物を採取して較正する。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import SpikeSession, log  # noqa: E402

OUT = Path(__file__).parent / "broker-state" / "probe"
OUT.mkdir(parents=True, exist_ok=True)

s = SpikeSession(state_dir=OUT / "state")
s.start()
s.spawn_claude()
try:
    for i in range(40):  # ~80 秒観測
        time.sleep(2)
        scr = s.screen()
        (OUT / f"screen-{i:02d}.txt").write_text(scr, encoding="utf-8")
        log(f"t+{(i + 1) * 2}s state={s.state()} registered={bool(s.broker.find_registered('claude-spike'))}")
finally:
    (OUT / "final.txt").write_text(s.screen(), encoding="utf-8")
    s.teardown(kill_pane=False)  # pane は残して目視確認可能に
    log("probe done (pane kept alive)")
