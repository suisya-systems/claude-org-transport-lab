# -*- coding: utf-8 -*-
"""WezTerm terminal adapter (Phase 1 spike, minimal surface).

設計 SoT: docs/design/renga-decoupling.md §4.7 (adapter 境界と能力表)。
スパイク要求面 (事前 codex design review 確定事項 (1)):
  spawn / send-text / get-text / list の 4 面。

設計上の固定事項:
- 全 `wezterm cli` 呼び出しで `--pane-id` を明示する (確定事項 (4))。
  省略時は WEZTERM_PANE / フォーカス先にフォールバックし誤配送の温床になるため。
- adapter は spawn した pane の window_id / tab_id / pane_id を保持する。
- 承認打鍵 (Enter 相当) は send-text --no-paste + CR で行う (確定事項 (1))。
  send-text 既定は bracketed paste 動作のため CR が Enter として解釈されない。
- spike は自分が spawn した pane のみ操作する。既存 pane (renga 等) には触らない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

WEZTERM_DEFAULT_EXE = r"C:\Program Files\WezTerm\wezterm.exe"

# ナッジ定型 1 行 (docs/design/renga-decoupling.md §4.3)。本文は PTY を通さない。
NUDGE_TEXT = "📨 新着あり。check_messages を実行"


def find_wezterm() -> str:
    """PATH 優先、無ければ winget 既定の絶対パス (CLAUDE.md 記載) を使う。"""
    exe = shutil.which("wezterm")
    if exe:
        return exe
    if os.path.exists(WEZTERM_DEFAULT_EXE):
        return WEZTERM_DEFAULT_EXE
    raise FileNotFoundError(
        "wezterm not found in PATH nor at " + WEZTERM_DEFAULT_EXE
    )


@dataclass
class PaneRef:
    """spawn した pane の追跡情報。毎回 --pane-id を明示するために保持する。"""

    pane_id: int
    tab_id: int | None = None
    window_id: int | None = None


@dataclass
class WezTermAdapter:
    exe: str = field(default_factory=find_wezterm)
    timeout: float = 15.0

    # ------------------------------------------------------------------ util
    def _cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.exe, "cli", "--no-auto-start", *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"wezterm cli failed ({proc.returncode}): {' '.join(args)}\n"
                f"stderr: {proc.stderr.strip()}"
            )
        return proc

    # ------------------------------------------------------------------ list
    def list_panes(self) -> list[dict]:
        """`wezterm cli list --format json` の生エントリ一覧。"""
        proc = self._cli("list", "--format", "json")
        return json.loads(proc.stdout)

    def pane_exists(self, pane_id: int) -> bool:
        return any(p["pane_id"] == pane_id for p in self.list_panes())

    # ----------------------------------------------------------------- spawn
    def spawn(
        self,
        argv: list[str],
        cwd: str | None = None,
        new_window: bool = True,
    ) -> PaneRef:
        """新しい pane を spawn し PaneRef を返す。

        spike は別 WezTerm ウィンドウで検証する (renga の現行組織ペインに
        触らない) ため new_window=True が既定。
        """
        args = ["spawn"]
        if new_window:
            args.append("--new-window")
        if cwd:
            args += ["--cwd", cwd]
        args += ["--", *argv]
        proc = self._cli(*args)
        pane_id = int(proc.stdout.strip())
        ref = PaneRef(pane_id=pane_id)
        # window_id / tab_id を list から補完して保持する (確定事項 (4))
        for p in self.list_panes():
            if p["pane_id"] == pane_id:
                ref.tab_id = p["tab_id"]
                ref.window_id = p["window_id"]
                break
        return ref

    # ------------------------------------------------------------- send-text
    def send_text(self, pane_id: int, text: str, no_paste: bool = False) -> None:
        """pane へテキスト送出。--pane-id 明示必須。

        no_paste=False (既定): bracketed paste として送る。入力欄に文字列を
          置くだけで Enter にはならない (改行も paste 内改行として扱われる)。
        no_paste=True: 生のキー入力として送る。"\r" が Enter として解釈される。
        """
        args = ["send-text", "--pane-id", str(pane_id)]
        if no_paste:
            args.append("--no-paste")
        args += ["--", text]
        self._cli(*args)

    def send_enter(self, pane_id: int) -> None:
        """Enter 1 打。承認プロンプトの機械承認等に使う (確定事項 (1))。"""
        self.send_text(pane_id, "\r", no_paste=True)

    def send_line(self, pane_id: int, text: str, settle: float = 0.15) -> None:
        """1 行送出 + Enter。ナッジ注入の正準形 (本文は通さない)。

        text 本体は paste で置き、確定の CR のみ --no-paste で送る。
        こうすると text 中の特殊文字がキー解釈されない。
        """
        self.send_text(pane_id, text, no_paste=False)
        time.sleep(settle)  # paste 反映と Enter の競合を避ける小休止
        self.send_enter(pane_id)

    # -------------------------------------------------------------- get-text
    def get_text(self, pane_id: int, escapes: bool = False) -> str:
        """pane の画面テキスト取得 (grid scrape)。--pane-id 明示必須。"""
        args = ["get-text", "--pane-id", str(pane_id)]
        if escapes:
            args.append("--escapes")
        proc = self._cli(*args)
        return proc.stdout

    # ------------------------------------------------------------------ kill
    def kill_pane(self, pane_id: int) -> None:
        """spawn した検証 pane の後始末 (kill-pane)。spike 内部用。"""
        self._cli("kill-pane", "--pane-id", str(pane_id), check=False)


# ---------------------------------------------------------------------------
# 画面状態ヒューリスティック (AC-1 自動判定の根拠)
# ---------------------------------------------------------------------------

# Claude Code TUI が応答生成中に表示する割り込みヒント
_BUSY_MARKERS = ("esc to interrupt", "ctrl+c to stop", "esc to cancel")
# スピナーのグリフ (Claude Code 2.x 系で観測される回転文字)
_SPINNER_CHARS = set("✻✶✳✢·∗*+✽")


def classify_pane_state(screen: str) -> str:
    """get-text の画面テキストから受信側状態を分類する。

    返り値: "busy" | "input_pending" | "idle" | "unknown"

    実測較正 (claude 2.1.168 / WezTerm 20240203):
    - idle 時の入力プロンプトは水平罫線に挟まれた "❯ " 行
      (旧バージョンの "│ > │" 枠形式もフォールバックで残す)。
    - 応答生成中は画面下部に "(esc to interrupt)" 等のヒントが出る。

    限界 (spike/manual-ime-test.md にも明記): get-text は PTY 内の文字 grid
    のみを観測する。IME の変換窓・候補 UI は OS 側のオーバーレイであり
    ここからは観測できない。よって IME 変換中の判定は自動化対象外。
    """
    lines = [ln.rstrip() for ln in screen.splitlines()]
    # 1) busy: 応答生成中ヒントが画面下部にある
    tail = "\n".join(lines[-20:]).lower()
    if any(m in tail for m in _BUSY_MARKERS):
        return "busy"

    # 2) 入力プロンプト行を下から探す ("❯ ..." / "│ > ... │" / "> ...")
    prompt_content: str | None = None
    for ln in reversed(lines):
        s = ln.strip()
        if s.startswith("❯"):
            prompt_content = s[1:].strip()
            break
        if s.startswith("│") and s.endswith("│") and len(s) > 2:
            inner = s[1:-1].strip()
            if inner.startswith(">"):
                prompt_content = inner[1:].strip()
                break

    if prompt_content is None:
        return "unknown"
    if prompt_content:
        return "input_pending"
    return "idle"


def wait_for_state(
    adapter: WezTermAdapter,
    pane_id: int,
    want: str,
    timeout: float = 30.0,
    interval: float = 1.0,
) -> bool:
    """pane が目的状態になるまで poll。到達で True。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if classify_pane_state(adapter.get_text(pane_id)) == want:
            return True
        time.sleep(interval)
    return False
