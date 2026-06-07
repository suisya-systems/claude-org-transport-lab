# -*- coding: utf-8 -*-
"""AC 検証ハーネス: broker + WezTerm adapter + 実 Claude TUI セッションの結線。

課金制約 (設計書 §1-1 / §2): spawn する Claude は対話型 TUI のみ。
`claude -p` / headless 起動は禁止。検証対話は最小トークンで行う。

スパイク注意 (advisor 指摘): 検証用 Claude は CLAUDE.md の無い中立 scratch
ディレクトリで spawn する。リポジトリ内 cwd で spawn すると fork の
secretary CLAUDE.md を継承してしまい判定が汚染される。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from broker import Broker  # noqa: E402
from wezterm_adapter import (  # noqa: E402
    PaneRef,
    WezTermAdapter,
    classify_pane_state,
)

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

AGENT_ID = "claude-spike"
OBSERVER_ID = "observer"

# 検証用 Claude が呼ぶ MCP ツールを事前許可し、許可プロンプトで停止させない。
# (MCP サーバー自体の信頼確認プロンプトの有無は別問題で、AC-2-1 の実測対象)
ALLOWED_TOOLS = ",".join(
    f"mcp__org-broker__{t}"
    for t in ("send_message", "check_messages", "list_peers", "set_summary")
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class StartupObservation:
    """spawn 直後の起動プロンプト観測結果 (AC-2-1 の実測記録)。"""

    folder_trust_prompt: bool = False
    mcp_trust_prompt: bool = False
    approved_mechanically: bool = False
    ready_seconds: float | None = None
    registered_seconds: float | None = None


class SpikeSession:
    """1 本の検証セッション: broker 起動 → Claude TUI spawn → 観測。"""

    def __init__(self, state_dir: Path, model: str = "sonnet"):
        self.adapter = WezTermAdapter()
        self.broker = Broker(state_dir=state_dir, adapter=self.adapter)
        self.model = model
        self.pane: PaneRef | None = None
        self.scratch: Path | None = None
        self.token: str | None = None
        self.observer_token: str | None = None
        self.obs = StartupObservation()

    # ------------------------------------------------------------------ up
    def start(self) -> None:
        self.broker.start()
        log(f"broker listening on {self.broker.url}")
        # 観測用エージェント (pane なし)。token 帰属検証の対向に使う。
        # MCP を経由しない server-side 合成エージェントなので明示登録する
        self.observer_token = self.broker.issue_token(
            OBSERVER_ID, OBSERVER_ID, "secretary"
        )
        self.broker.register_local(self.observer_token)

    def spawn_claude(self) -> None:
        """中立 scratch dir で対話型 Claude TUI を WezTerm 新規ウィンドウに spawn。"""
        assert self.token is None, "already spawned"
        self.scratch = Path(tempfile.mkdtemp(prefix="broker-spike-"))
        self.token = self.broker.issue_token(AGENT_ID, AGENT_ID, "worker")
        cfg_path = self.scratch / "mcp-config.json"
        cfg_path.write_text(
            json.dumps(self.broker.mcp_config_for(self.token)), encoding="utf-8"
        )
        claude_exe = shutil.which("claude")
        if not claude_exe:
            raise FileNotFoundError("claude CLI not found in PATH")
        argv = [
            claude_exe,
            "--mcp-config", str(cfg_path),
            "--strict-mcp-config",          # 確定事項 (3): 既存 MCP の混入遮断
            "--allowedTools", ALLOWED_TOOLS,
            "--model", self.model,
        ]
        t0 = time.monotonic()
        self.pane = self.adapter.spawn(argv, cwd=str(self.scratch), new_window=True)
        self.broker.bind_pane(self.token, self.pane.pane_id)
        log(f"claude spawned: pane_id={self.pane.pane_id} "
            f"window_id={self.pane.window_id} scratch={self.scratch}")
        self._spawn_t0 = t0

    # --------------------------------------------------------- startup loop
    def wait_ready(self, timeout: float = 90.0) -> bool:
        """起動プロンプト (folder trust / MCP trust) を機械承認し idle 到達を待つ。

        AC-2-1: 信頼確認プロンプトが出る場合、orchestrator が機械承認可能で
        あること (人間の手作業が必要なら不合格)。
        """
        assert self.pane is not None
        deadline = time.monotonic() + timeout
        last_approve = 0.0
        while time.monotonic() < deadline:
            screen = self.adapter.get_text(self.pane.pane_id)
            low = screen.lower()
            now = time.monotonic()
            # 実測 (claude 2.1.168): "Quick safety check: Is this a project you
            # created or one you trust?" + "❯ 1. Yes, I trust this folder"
            if ("yes, i trust this folder" in low
                    or "is this a project you created" in low
                    or "do you trust the files" in low):
                self.obs.folder_trust_prompt = True
                if now - last_approve > 2.0:
                    log("folder trust prompt detected -> approving (Enter)")
                    self.adapter.send_enter(self.pane.pane_id)
                    self.obs.approved_mechanically = True
                    last_approve = now
            elif ("mcp server" in low and ("trust" in low or "approve" in low or
                                           "use this" in low)):
                self.obs.mcp_trust_prompt = True
                if now - last_approve > 2.0:
                    log("MCP trust prompt detected -> approving (Enter)")
                    self.adapter.send_enter(self.pane.pane_id)
                    self.obs.approved_mechanically = True
                    last_approve = now
            elif classify_pane_state(screen) == "idle":
                self.obs.ready_seconds = now - self._spawn_t0
                log(f"claude ready (idle) in {self.obs.ready_seconds:.1f}s")
                return True
            time.sleep(1.0)
        return False

    def wait_registered(self, timeout: float = 30.0) -> bool:
        """AC-2-3: broker bind 表ベースの登録検知 (〜30 秒)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.broker.find_registered(AGENT_ID):
                self.obs.registered_seconds = time.monotonic() - self._spawn_t0
                log(f"agent registered on broker in "
                    f"{self.obs.registered_seconds:.1f}s after spawn")
                return True
            time.sleep(0.5)
        return False

    # ------------------------------------------------------------ utilities
    def screen(self) -> str:
        assert self.pane is not None
        return self.adapter.get_text(self.pane.pane_id)

    def state(self) -> str:
        return classify_pane_state(self.screen())

    def type_text(self, text: str) -> None:
        """入力欄へ paste (送信しない)。"""
        assert self.pane is not None
        self.adapter.send_text(self.pane.pane_id, text, no_paste=False)

    def submit(self) -> None:
        assert self.pane is not None
        self.adapter.send_enter(self.pane.pane_id)

    def prompt(self, text: str, settle: float = 0.3) -> None:
        """1 プロンプト送信 (最小トークンで)。"""
        self.type_text(text)
        time.sleep(settle)
        self.submit()

    def clear_input(self) -> None:
        """入力欄の未送信テキストを破棄。

        実測 (claude 2.1.168): Esc は入力をクリアしない (rewind 系 UI)。
        Ctrl+C 1 打で入力欄クリアになる (2 連打は exit なので 1 回のみ)。
        """
        assert self.pane is not None
        self.adapter.send_text(self.pane.pane_id, "\x03", no_paste=True)

    def wait_state(self, want: str, timeout: float = 60.0, interval: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state() == want:
                return True
            time.sleep(interval)
        return False

    def observer_send(self, message: str) -> dict:
        """observer (pane なし) から claude-spike へ送信 → ナッジ配達が走る。"""
        bind = self.broker.get_bind(self.observer_token)
        assert bind is not None
        return self.broker.enqueue(bind, AGENT_ID, message)

    def journal_events(self) -> list[dict]:
        path = self.broker.state_dir / "queue.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(ln)
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    # ---------------------------------------------------------------- down
    def teardown(self, kill_pane: bool = True) -> None:
        if kill_pane and self.pane is not None:
            self.adapter.kill_pane(self.pane.pane_id)
            self.pane = None
        self.broker.stop()
