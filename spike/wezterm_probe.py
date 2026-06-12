# -*- coding: utf-8 -*-
"""lab#9 無課金 probe: 実 WezTerm pane で Claude TUI の 3 画面状態を再現する。

run_ac9.py が spawn する使い捨てプロセス。**実 Claude を起動しないため無課金**
(課金中立スコープ: 窓口/ユーザー判断 2026-06-13 で probe-only AC を承認。実 Claude TUI の
実証は AC-2 Phase 1 で既済 + #515 本番サイクルに委譲)。

目的: broker.inspect_pane → classify_pane_state が **実 WezTerm get-text** に対して
idle / input_pending / busy を判定できることを実機で示す (FakeAdapter の合成画面ではなく、
実 PTY に描画された画面を WezTerm cli get-text で scrape して判定する)。描画する較正画面は
terminal_adapter.classify_pane_state が前提とする claude 2.1.168 の実測描画に合わせてある:
- idle:          末尾に空の "❯ " プロンプト行
- input_pending: "❯ <未送信テキスト>" (プロンプトに内容がある)
- busy:          画面下部に "(esc to interrupt)" ヒント

stdin から 1 行ずつコマンドを受ける (broker.send_keys → adapter.send_keys 経由):
  idle / input / busy   … 対応する状態へ再描画
  quit                  … 終了
  その他の非空行         … echo として表示 (send_keys の文字往復検証用)
stdin が閉じる (pane kill) と EOF でループを抜けて終了する。
"""

from __future__ import annotations

import sys

sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RULE = "─" * 30  # 水平罫線 (─)
# フレームを viewport 末尾へ押し下げる前置改行数。classify_pane_state の busy 判定は
# 末尾 20 行 (`lines[-20:]`) のみを走査するため、実 Claude TUI と同様にヒント行が画面
# 下部に来るよう content をボトム寄せにする。これをしないと 36 行 viewport の上部に
# 描いた "(esc to interrupt)" が tail-20 の外に落ち busy が unknown 判定になる
# (lab#9 実機較正で発見)。viewport が高くても余剰改行はスクロールで吸収される。
_LEAD_BLANKS = 30


def render(state: str, echo: str | None = None) -> None:
    """画面全体をクリアして指定状態の較正画面を **viewport 下部** に描く。

    \x1b[2J\x1b[H で grid をクリア + cursor home し (前状態の残骸が
    classify_pane_state の末尾走査を汚さないように)、前置改行で content を下部へ
    押し下げる (busy ヒントを tail-20 内に収める)。WezTerm は ANSI 対応。
    """
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.write("\n" * _LEAD_BLANKS)
    print(RULE)
    print(f"[lab9-probe] state={state}")
    if echo is not None:
        print(f"echo> {echo}")
    print(RULE)
    if state == "busy":
        # busy 判定は "(esc to interrupt)" の有無のみ (スピナーグリフは点滅で取りこぼす)
        print("応答を生成中…")  # 応答を生成中…
        print("  (esc to interrupt)")
    elif state == "input_pending":
        # 承認待ち相当: プロンプトに未送信テキストがある
        print("❯ 未送信の承認応答が入力欄にあります")
    else:  # idle: 空の ❯ プロンプト
        print("❯ ")
    print(RULE, flush=True)


_ALIASES = {
    "idle": "idle",
    "input": "input_pending",
    "input_pending": "input_pending",
    "busy": "busy",
}


def main() -> int:
    state = "idle"
    render(state)
    for raw in sys.stdin:
        cmd = raw.strip()
        if cmd == "quit":
            break
        if cmd in _ALIASES:
            state = _ALIASES[cmd]
            render(state)
        elif cmd:
            render(state, echo=cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
