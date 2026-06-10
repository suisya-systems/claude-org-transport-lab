# -*- coding: utf-8 -*-
"""スピナー再現ハーネス — 実 Claude を待たず、同位置連続再描画で IME 共存を実機検証する。

目的（ime-backend-parity-spike / Refs #6 #9）:
  Claude Code の応答生成中スピナー（「✻ Cogitating… (Ns)」等）が行う
  **同じ位置での連続再描画**を、ANSI カーソル制御（DECSC/DECRC・CUP・EL）で再現する
  （実 Claude の内部実装シーケンスは未確認のため、本ハーネスは「代表的な同位置連続再描画刺激」を
  生成するものであって、Claude のバイト列を忠実に複製するものではない）。
  人間がこのハーネスを **tmux** と **WezTerm 素** の各 backend で起動し、入力欄に日本語を
  IME 変換しながらタイプして、スピナー再描画が変換窓のアンカーを奪うか目視判定する。

  機構の根拠は [`mechanism.md`](./mechanism.md)、実走手順と GO/NO-GO テンプレは
  [`manual-ac-ime-parity.md`](./manual-ac-ime-parity.md) を参照。

設計上の要点（なぜこの再描画が IME を壊しうるか — mechanism.md §3/§4 に対応）:
  - スピナー行は **カーソル保存（DECSC ESC7）→ スピナー行へ移動（CUP）→ 上書き → 復元（DECRC ESC8）**
    の往復、または **絶対 CUP で往復**して、~10Hz でカーソルを入力欄から動かして戻す。
  - IME 窓（変換窓・候補窓）は「アプリが報告するカーソルセル」に錨を打つため、この往復に
    追従して揺れる/飛ぶ可能性がある。本ハーネスはその刺激そのものを生成する。
  - `--cursor-mode save|cup` で 2 つの再描画様式を切り替え、どちらが IME を崩すか切り分けられる。

課金制約との関係: 本ハーネスは **実 Claude を一切起動しない**（純粋な ANSI 描画のみ）。
  Python 標準ライブラリのみ。POSIX（WSL2/Linux/macOS）で TTY 入力を取り、Windows ネイティブ
  （py -3）では入力なしのアニメーションのみで動く（IME overlay は端末が描くので観測は可能）。

使い方:
  python spinner_harness.py                      # 既定: streaming 状態・save モード
  python spinner_harness.py --state ime          # IME 変換中の実走（streaming と同じ刺激＋案内）
  python spinner_harness.py --state idle         # スピナー停止（対照群）
  python spinner_harness.py --state long-input   # スピナー停止・長文入力の対照
  python spinner_harness.py --cursor-mode cup    # 絶対 CUP 往復（より厳しい刺激）
  python spinner_harness.py --hz 20 --no-input   # アニメーションのみ（入力を取らない）
  python spinner_harness.py --selftest           # TTY 不要の自己診断（CI/サンドボックス用）
"""

from __future__ import annotations

import argparse
import codecs
import sys
import threading
import time
import unicodedata

ESC = "\x1b"


# ---------------------------------------------------------------------------
# ANSI シーケンス・ビルダー（純関数 — selftest で検証可能）
# ---------------------------------------------------------------------------
def cup(row: int, col: int) -> str:
    """カーソル絶対移動（1-based）。CSI row;col H。"""
    return f"{ESC}[{row};{col}H"


def el_to_eol() -> str:
    """行末まで消去。CSI K（= CSI 0 K）。"""
    return f"{ESC}[K"


SAVE_CURSOR = f"{ESC}7"      # DECSC: カーソル位置・属性を保存
RESTORE_CURSOR = f"{ESC}8"   # DECRC: 保存位置へ復元
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"
ALT_SCREEN_ON = f"{ESC}[?1049h"
ALT_SCREEN_OFF = f"{ESC}[?1049l"
CLEAR_SCREEN = f"{ESC}[2J"


def cell_width(text: str) -> int:
    """端末セル幅の概算（East Asian Wide/Fullwidth は 2 セル）。

    入力欄カーソル列の追跡に使う（cup モードでの復帰先計算）。完全な wcwidth では
    ないが、ASCII と CJK の幅判定には十分。結合文字は 0 とみなす。
    """
    w = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


# ---------------------------------------------------------------------------
# スピナー表現（Claude Code 風）
# ---------------------------------------------------------------------------
SPINNER_GLYPHS = ["·", "✢", "✳", "∗", "✻", "✽", "✻", "∗", "✳", "✢"]
GERUNDS = ["Cogitating", "Pondering", "Ruminating", "Musing", "Deliberating", "Percolating"]
# streaming 状態で transcript に流す擬似出力（日本語・英語を混在させ視覚的に動かす）
STREAM_LINES = [
    "broker queue store にメッセージを enqueue しています…",
    "terminal adapter の能力表を参照中: send-text / get-text / list-panes",
    "Considering the IME composition anchor under same-position redraw.",
    "tmux はホスト端末 (Windows Terminal) に描画を委譲します。",
    "WezTerm 素は IMM32 で候補窓をカーソル相対に配置します。",
    "DECSC/DECRC のカーソル往復が変換窓に与える影響を観測中。",
    "✻ The quick brown fox jumps over the lazy dog. 0123456789",
    "日本語入力中もスピナーは同じ位置で再描画され続けます。",
]


def spinner_text(frame: int, elapsed_s: float, tokens: int) -> str:
    """スピナー 1 行の本文（Claude Code のステータス行を模す）。"""
    glyph = SPINNER_GLYPHS[frame % len(SPINNER_GLYPHS)]
    word = GERUNDS[(int(elapsed_s) // 4) % len(GERUNDS)]
    return f"{glyph} {word}… ({elapsed_s:.0f}s · ↑ {tokens} tokens · esc to interrupt)"


# ---------------------------------------------------------------------------
# ハーネス本体
# ---------------------------------------------------------------------------
class SpinnerHarness:
    def __init__(self, args: argparse.Namespace, rows: int, cols: int):
        self.args = args
        self.rows = rows
        self.cols = cols
        self.spinner_row = rows - 1          # スピナー行（入力欄の 1 つ上 = Claude 風）
        self.input_row = rows                # 入力欄（最下行）
        self.transcript_rows = rows - 3      # 上部 transcript 領域（1..rows-3）
        self.sep_row = rows - 2
        self.prompt = "❯ "
        self._base_cells = cell_width(self.prompt)
        self.input_cells = self._base_cells   # 入力欄カーソルの現在列（1-based 末尾）
        self.input_buf: list[str] = []        # 確定済み入力文字（幅追跡・Backspace 用）
        # 入力バイトの逐次 UTF-8 デコーダ。raw byte はそのままエコーし、
        # 完成した文字だけを幅追跡へ反映する（1 byte ずつの誤 decode による mojibake を避ける）。
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.transcript: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._frame = 0
        self._tokens = 0
        self._stream_i = 0
        self._spinner_on = args.state in ("streaming", "ime")

    # ---- 描画プリミティブ（呼び出し側で self._lock を保持すること） ----
    def _w(self, s: str) -> None:
        """ANSI/テキストを UTF-8 バイトで書く（raw byte エコーと順序を保つため buffer に統一）。"""
        sys.stdout.buffer.write(s.encode("utf-8"))

    def _wb(self, b: bytes) -> None:
        """バイト列を端末へ書く（完成した UTF-8 文字の再エンコード列。mojibake しない）。"""
        sys.stdout.buffer.write(b)

    def _flush(self) -> None:
        sys.stdout.buffer.flush()

    def _repaint_static(self) -> None:
        """初期画面（banner / 区切り / 入力プロンプト）を描く。"""
        self._w(CLEAR_SCREEN)
        banner = self._banner_lines()
        for i, line in enumerate(banner[: self.transcript_rows], start=1):
            self._w(cup(i, 1) + el_to_eol() + line[: self.cols])
        self._w(cup(self.sep_row, 1) + el_to_eol() + "─" * self.cols)
        self._w(cup(self.input_row, 1) + el_to_eol() + self.prompt)
        self._draw_spinner_locked(initial=True)
        # 実カーソルを入力欄末尾に置く（IME はここに錨を打つ）
        self._w(cup(self.input_row, self.input_cells + 1))
        self._flush()

    def _banner_lines(self) -> list[str]:
        st = self.args.state
        cm = self.args.cursor_mode
        return [
            f"== スピナー再現ハーネス ==  state={st}  cursor-mode={cm}  hz={self.args.hz}",
            "tmux / WezTerm 素 の両 backend で起動し、下の ❯ 入力欄に日本語を IME 変換しながら",
            "タイプして、スピナー再描画が変換窓のアンカーを奪うか目視判定してください。",
            "  Ctrl+U: 入力欄クリア   Ctrl+C: 終了   （入力欄は 1 行モデル: Enter/折返しは無視）",
            "（手順と GO/NO-GO テンプレは manual-ac-ime-parity.md を参照）",
            "",
        ]

    def _draw_spinner_locked(self, initial: bool = False) -> None:
        """スピナー行を同位置で再描画する（IME を揺らす中心刺激）。"""
        elapsed = time.monotonic() - self._t0
        if self._spinner_on:
            body = spinner_text(self._frame, elapsed, self._tokens)
        elif initial:
            # 非スピナー状態は state ごとに表示を変える（idle と long-input を区別する）
            if self.args.state == "long-input":
                body = "(長文入力中 — スピナー停止・対照群: 下の ❯ に端末幅に近い長い 1 行の日本語を変換入力)"
            else:
                body = "(idle — スピナー停止・対照群)"
        else:
            return
        text = body[: self.cols]
        if self.args.cursor_mode == "save":
            # DECSC/DECRC 往復: 保存 → スピナー行へ → 上書き → 復元
            self._w(SAVE_CURSOR + cup(self.spinner_row, 1) + el_to_eol() + text + RESTORE_CURSOR)
        else:
            # 絶対 CUP 往復: スピナー行へ → 上書き → 入力欄カーソルへ戻す（追跡列を使用）
            self._w(cup(self.spinner_row, 1) + el_to_eol() + text
                    + cup(self.input_row, self.input_cells + 1))

    def _repaint_transcript_locked(self) -> None:
        """transcript 領域を再描画（save/restore でカーソルを乱さない）。"""
        rows = self.transcript[-self.transcript_rows:]
        out = [SAVE_CURSOR]
        base = len(self._banner_lines())
        for i, line in enumerate(rows):
            r = base + 1 + i
            if r >= self.sep_row:
                break
            out.append(cup(r, 1) + el_to_eol() + line[: self.cols])
        out.append(RESTORE_CURSOR)
        self._w("".join(out))

    # ---- アニメーション・スレッド ----
    def _animate(self) -> None:
        period = 1.0 / max(1, self.args.hz)
        last_stream = 0.0
        while not self._stop.is_set():
            with self._lock:
                self._frame += 1
                if self._spinner_on:
                    self._tokens += 7  # 擬似トークン加算
                self._draw_spinner_locked()
                now = time.monotonic()
                if self._spinner_on and now - last_stream > 0.7:
                    self.transcript.append(STREAM_LINES[self._stream_i % len(STREAM_LINES)])
                    self._stream_i += 1
                    self._repaint_transcript_locked()
                    last_stream = now
                self._flush()
            time.sleep(period)

    def _echo_byte(self, ch: bytes) -> None:
        """入力バイト 1 つを食わせ、UTF-8 として完成した文字だけをエコー＋幅追跡する（lock 下で呼ぶ）。

        完成文字を UTF-8 へ再エンコードして書くので mojibake しない（確定済み日本語も正しく
        入力欄に入る — 手動 AC の中心観点 (c)）。本ハーネスの入力欄は **単一行モデル**である:
        改行・タブ等の制御文字は無視し、端末幅に達したら以降の文字を無視する。これにより
        折返し・複数行による「実カーソル位置」と「追跡列 input_cells」の desync を構造的に防ぐ
        （cup モードの復帰先・Backspace 消去位置が常に正しい単一行に閉じる）。長文の対照は
        「端末幅に近い長い 1 行」で行う（複数行編集はこのハーネスの対象外）。
        """
        for c in self._decoder.decode(ch):
            if unicodedata.category(c).startswith("C"):  # 制御文字（改行/タブ等）は無視＝単一行を保つ
                continue
            w = cell_width(c)
            if self.input_cells + w >= self.cols:        # 端末幅に達したら無視（折返し desync 防止）
                continue
            self._wb(c.encode("utf-8"))
            self.input_buf.append(c)
            self.input_cells += w

    # ---- 入力スレッド（POSIX raw mode）----
    def _read_input_posix(self) -> None:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while not self._stop.is_set():
                ch = sys.stdin.buffer.read(1)
                if not ch:
                    break
                b = ch[0]
                if b == 0x03:                      # Ctrl+C
                    self._stop.set()
                    break
                with self._lock:
                    if b == 0x15:                  # Ctrl+U: 入力欄クリア
                        self.input_buf.clear()
                        self.input_cells = self._base_cells
                        self._decoder.reset()
                        self._w(cup(self.input_row, 1) + el_to_eol() + self.prompt
                                + cup(self.input_row, self.input_cells + 1))
                    elif b in (0x7F, 0x08):        # Backspace: 直近 1 文字を幅ぶん戻す
                        if self.input_buf:
                            c = self.input_buf.pop()
                            self.input_cells -= cell_width(c)
                            self._w(cup(self.input_row, self.input_cells + 1) + el_to_eol())
                    else:
                        self._echo_byte(ch)
                    self._flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    # ---- ライフサイクル ----
    def run(self) -> int:
        self._w(ALT_SCREEN_ON + HIDE_CURSOR)
        # 入力欄では実カーソルを見せたいので、初期描画後にカーソルを表示し直す
        try:
            with self._lock:
                self._repaint_static()
            self._w(SHOW_CURSOR)
            self._flush()
            anim = threading.Thread(target=self._animate, daemon=True)
            anim.start()
            if self.args.no_input or not sys.stdin.isatty():
                # アニメーションのみ（入力を取らない）。Ctrl+C か duration で終了。
                deadline = self._t0 + self.args.duration if self.args.duration else None
                while not self._stop.is_set():
                    if deadline and time.monotonic() > deadline:
                        break
                    time.sleep(0.1)
            else:
                self._read_input_posix()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            time.sleep(0.05)
            self._w(SHOW_CURSOR + ALT_SCREEN_OFF)
            self._flush()
        return 0


# ---------------------------------------------------------------------------
# 自己診断（TTY 不要 — CI/サンドボックスで描画ロジックを検証）
# ---------------------------------------------------------------------------
def selftest() -> int:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        ok = ok and cond
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")

    check("cup(1,1)", cup(1, 1) == "\x1b[1;1H")
    check("el", el_to_eol() == "\x1b[K")
    check("DECSC/DECRC", SAVE_CURSOR == "\x1b7" and RESTORE_CURSOR == "\x1b8")
    check("cell_width ascii", cell_width("abc") == 3)
    check("cell_width cjk", cell_width("日本") == 4)
    check("cell_width mixed", cell_width("a日") == 3)
    st = spinner_text(4, 12.0, 140)
    check("spinner_text glyph", st.startswith("✻ "))
    check("spinner_text fields", "12s" in st and "140 tokens" in st and "esc to interrupt" in st)
    # save モードのスピナー描画に DECSC/DECRC/CUP/EL が含まれること
    h = SpinnerHarness(argparse.Namespace(state="streaming", cursor_mode="save", hz=10,
                                          no_input=True, duration=0), rows=24, cols=80)
    buf: list[str] = []
    h._w = buf.append  # type: ignore[method-assign]
    h._draw_spinner_locked()
    seq = "".join(buf)
    check("save-mode uses DECSC", SAVE_CURSOR in seq and RESTORE_CURSOR in seq)
    check("save-mode moves to spinner row", cup(h.spinner_row, 1) in seq)
    # cup モードは絶対 CUP 往復で入力欄へ戻る
    h2 = SpinnerHarness(argparse.Namespace(state="streaming", cursor_mode="cup", hz=10,
                                           no_input=True, duration=0), rows=24, cols=80)
    buf2: list[str] = []
    h2._w = buf2.append  # type: ignore[method-assign]
    h2._draw_spinner_locked()
    seq2 = "".join(buf2)
    check("cup-mode returns to input row", cup(h2.input_row, h2.input_cells + 1) in seq2)
    check("cup-mode no DECSC", SAVE_CURSOR not in seq2)
    # UTF-8 確定入力のエコー往復: 1 byte ずつ食わせても mojibake せず raw byte が戻ること
    # （旧 latin-1 decode のリグレッション検出。手動 AC 観点 (c) をハーネスが壊さない保証）。
    h3 = SpinnerHarness(argparse.Namespace(state="ime", cursor_mode="save", hz=10,
                                           no_input=True, duration=0), rows=24, cols=80)
    raw: list[bytes] = []
    h3._wb = raw.append  # type: ignore[method-assign]
    for byte in "日本ABc".encode("utf-8"):
        h3._echo_byte(bytes([byte]))
    check("utf8 echo roundtrip (no mojibake)", b"".join(raw) == "日本ABc".encode("utf-8"))
    check("utf8 input buffer chars", h3.input_buf == ["日", "本", "A", "B", "c"])
    check("utf8 width tracked (CJK=2)", h3.input_cells == h3._base_cells + 2 + 2 + 1 + 1 + 1)
    # 単一行モデル: 制御文字（改行/タブ）は無視され、追跡列・バッファが乱れないこと
    cells_before = h3.input_cells
    for byte in b"\r\n\t":
        h3._echo_byte(bytes([byte]))
    check("control bytes ignored (single-line)",
          h3.input_buf == ["日", "本", "A", "B", "c"] and h3.input_cells == cells_before)
    # 端末幅キャップ: cols を超える入力は無視され input_cells が cols 未満に閉じること
    h4 = SpinnerHarness(argparse.Namespace(state="ime", cursor_mode="cup", hz=10,
                                           no_input=True, duration=0), rows=24, cols=20)
    for _ in range(40):
        h4._echo_byte("あ".encode("utf-8"))
    check("width cap keeps single line", h4.input_cells < h4.cols)
    print("\n自己診断:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def detect_size() -> tuple[int, int]:
    try:
        import shutil
        sz = shutil.get_terminal_size(fallback=(80, 24))
        return max(8, sz.lines), max(20, sz.columns)
    except Exception:
        return 24, 80


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Claude スピナー再現ハーネス（IME 共存検証）")
    p.add_argument("--state", choices=["idle", "long-input", "streaming", "ime"],
                   default="streaming",
                   help="AC-1 4 状態。streaming/ime はスピナー稼働、idle/long-input は停止（対照）")
    p.add_argument("--cursor-mode", choices=["save", "cup"], default="save",
                   help="再描画のカーソル様式。save=DECSC/DECRC 往復, cup=絶対 CUP 往復")
    p.add_argument("--hz", type=int, default=10, help="スピナー再描画頻度（既定 10Hz）")
    p.add_argument("--no-input", action="store_true",
                   help="入力を取らずアニメーションのみ（非 TTY/Windows ネイティブ向け）")
    p.add_argument("--duration", type=float, default=0.0,
                   help="--no-input 時の自動終了秒数（0=Ctrl+C まで）")
    p.add_argument("--selftest", action="store_true", help="TTY 不要の自己診断のみ実行")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    rows, cols = detect_size()
    return SpinnerHarness(args, rows, cols).run()


if __name__ == "__main__":
    sys.exit(main())
