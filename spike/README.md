# renga 依存解消スパイク (org-broker + terminal adapter: WezTerm / tmux)

設計 SoT: [`docs/design/renga-decoupling.md`](../docs/design/renga-decoupling.md)
§4 (broker / adapter 設計)・§7.1 (Phase 1 AC)・§4.7 (backend 能力表)。

このディレクトリは**使い捨て前提のスパイク**であり、本体の実装ではない
(broker / adapter の実体は Phase 3 以降に claude-org-runtime 側へ置く計画)。
本体の `.state/` / ポート / workers_dir には一切触れない (自己完結)。

- **Phase 1**: org-broker + WezTerm adapter (Windows)。AC-1 / AC-2 green。
- **Phase 2** (Issue #2): tmux adapter (POSIX 正準 backend) を第二実装として追加し、
  ハーネスを backend パラメータ化。POSIX (tmux/WSL2) と Windows (WezTerm) の両 backend で
  AC-1 / AC-2 green。詳細は [`RESULTS.md`](./RESULTS.md) の Phase 2 節。

## 構成

| ファイル | 役割 |
|---|---|
| `broker.py` | org-broker プロトタイプ。localhost (127.0.0.1) HTTP MCP サーバー + per-agent token 認証 + broker queue store + ナッジ配達 (静止確認 defer)。Python stdlib のみ。backend 非依存 |
| `terminal_adapter.py` | **backend 共有基盤**。`TerminalAdapter` Protocol (intent 面: spawn / get_text / type_text / send_enter / send_line / send_interrupt / kill_pane / list_panes) + `classify_pane_state` (画面状態ヒューリスティック、backend 非依存) + `make_adapter(backend)` ファクトリ + `NUDGE_TEXT` / `PaneRef` |
| `wezterm_adapter.py` | WezTerm terminal adapter (spawn / send-text / get-text / list、`--pane-id` 全呼出明示)。Enter/Ctrl+C は send-text の小細工 (`--no-paste` + CR/ETX) で出す |
| `tmux_adapter.py` | tmux terminal adapter (spawn=new-session / send-keys / capture-pane / list-panes、target `%N` 明示)。Enter/Ctrl+C/1 行は一級 `send-keys` で素直に出す。専用 socket `-L claude-org-spike` で既存 tmux サーバーと分離。`python tmux_adapter.py` で無課金の自己診断 (cat を spawn) |
| `harness.py` | AC 検証ハーネス (broker + adapter + 実 Claude TUI の結線、起動プロンプト機械承認)。`SpikeSession(..., backend=...)` で backend 選択 |
| `mcp_smoke_test.py` | MCP プロトコル層の合成クライアント検証 (Claude 不要・無課金、backend 非依存) |
| `run_ac2.py` | AC-2 (起動・接続チェーン) 自動検証。`--backend {wezterm,tmux}` |
| `run_ac1.py` | AC-1 のうち自動 3 状態 (idle / 長文入力中 / ストリーミング中) の自動判定。`--backend {wezterm,tmux}` |
| `run_ac9.py` | **AC-9 (WezTerm backend 実機 AC, Issue #9)**。実 WezTermAdapter で 6 面 + ライフサイクル + イベント + 画面状態観測 + 1 サイクルを実機往復 (無課金 probe)。接続先は **headless `wezterm-mux-server`** で **GUI ウィンドウは出ない** (= tmux と同格。可視化条件は [`ac9-wezterm-evidence.md`](./ac9-wezterm-evidence.md) §5 / #540)。`py -3 run_ac9.py` |
| `wezterm_probe.py` | AC-9 用の無課金 probe。実 WezTerm pane で claude 2.1.168 較正描画 (idle / 承認待ち / busy) を再現し、inspect→classify を実 get-text で成立させる (実 Claude 不起動) |
| `ac9-wezterm-evidence.md` | AC-9 の Issue #9 成果物。geometry defect 発見経緯・修正・通過証跡・argv attestation・tmux 差分表 |
| `manual-ime-test.md` | AC-1 状態 2 (IME 変換中) の手動テスト手順書 (broker ナッジ注入の輸送層検証) |
| `manual_ime_session.py` | 同手動テスト用の対話セッション起動スクリプト |
| `ime-parity/` | **IME × スピナー backend parity スパイク** (ime-backend-parity-spike, Refs #6 #9)。tmux 素 vs WezTerm 素 で日本語 IME 入力 + Claude スピナー描画が共存するかを再検証。機構解明 md + スピナー再現ハーネス + 手動 AC テンプレ (4 状態 GO/NO-GO)。propose-only。詳細は [`ime-parity/README.md`](./ime-parity/README.md) |
| `probe_startup.py` | TUI 描画採取用 probe (較正用・使い捨て) |
| `RESULTS.md` | AC 判定結果の記録 (go/no-go)。Phase 1 (WezTerm) + Phase 2 (tmux) |
| `broker-state/` | broker queue store + 画面ダンプ等の実行時生成物 (git 管理外) |

## backend 選択

`--backend {wezterm,tmux}` で明示。省略時は OS から自動選択する
(POSIX = tmux / Windows = wezterm。環境変数 `SPIKE_BACKEND` でも上書き可)。

- **tmux** (POSIX 正準 backend): tmux 3.4+ / `claude` CLI / Python 3.x。
  detached session で動くため GUI / display は不要 (WSL2 / Linux / macOS / CI 向き)。
- **WezTerm** (Windows): WezTerm (20240203-110809 以降) / `claude` CLI。
  PATH に無い場合は `C:\Program Files\WezTerm\wezterm.exe` を自動で使う。

## 実行手順

```bash
cd spike

# 1. プロトコル層 (無課金・Claude 不要・backend 非依存)
python mcp_smoke_test.py

# --- POSIX (tmux) ---
python run_ac2.py --backend tmux   # AC-2: 起動・接続チェーン (detached tmux session に spawn)
python run_ac1.py --backend tmux   # AC-1 自動 3 状態
python tmux_adapter.py             # tmux adapter の無課金自己診断 (cat を spawn)

# --- Windows (WezTerm) — PowerShell では py -3 ---
py -3 run_ac2.py --backend wezterm # AC-2: headless mux に spawn (GUI ウィンドウは出ない)
py -3 run_ac1.py --backend wezterm # AC-1 自動 3 状態
py -3 run_ac9.py                   # AC-9: WezTerm backend 実機 AC (6 面+1 サイクル, 無課金 probe)

# AC-1 状態 2 (IME) — 手動。manual-ime-test.md の手順に従う
py -3 manual_ime_session.py
```

- spawn される Claude は**対話型 TUI セッションのみ** (`claude -p` / headless は
  課金制約により禁止。設計書 §1-1)。検証対話は最小トークン。
- 検証用 Claude は CLAUDE.md の無い一時 scratch ディレクトリ
  (`tempfile.mkdtemp(prefix="broker-spike-")`、POSIX では `/tmp/broker-spike-*`、
  Windows では `%TEMP%\broker-spike-*`) で spawn される (リポジトリの secretary
  CLAUDE.md を継承させないため)。
- 各スクリプトは終了時に検証 pane を kill する。失敗時の画面ダンプは
  `broker-state/{ac1,ac2}/screen-*.txt` に残る。

## AC 判定結果の記録様式

各 run は `broker-state/{ac1,ac2}/result.json` に機械可読の判定を書き出す。
人間向けの正本は [`RESULTS.md`](./RESULTS.md) に転記する:

- 項目ごとに **GO / NO-GO** と判定根拠 (1 行)
- NO-GO の場合は原因と緩和案 (設計書 §4.3 の静止確認 defer 等) を添える。
  **勝手に AC を緩めない** (AC-1 は計画中止の足切り条項)

## スパイクとしての既知の簡略化 (本実装スコープとの境界)

設計書 §4.4 / 事前 codex design review 確定事項 (2) に基づく意図的な簡略化:

1. **token は長寿命 + static headers 固定**: TTL / 失効・再発行 / headersHelper は
   Phase 3 の本実装スコープ。`--mcp-config` の headers に発行済み token を静的に
   埋める (env 参照は config parse 時失敗リスクがあるため不使用)。
2. **revoke は未実装**: bind 表の `revoked` フラグと `[token_revoked]` 拒否経路は
   あるが、pane_exited 連動の自動 revoke はスパイク対象外。
3. **ナッジ defer 枯渇後の再配達なし**: defer 最大 30 回 (60 秒) 枯渇で
   `nudge_failed` を journal に記録するのみ。次回 enqueue 時に再 trigger される。
   本実装では設計書 §4.3 の再ナッジ + エスカレーション経路が必要。
4. **queue store は in-memory + JSONL journal**: 永続キューの復旧 (broker 再起動時)
   は対象外。
5. **dispatcher / secretary 向け surface (ペイン操作系) は未実装**: スパイクは
   worker 面 (send_message / check_messages / list_peers / set_summary) のみ。
6. **イベント (poll_events 相当) は未実装**: Phase 4 スコープ。

## 本体との分離 (設計書 §7.5)

- broker は OS が割り当てる空きポート (port=0) で起動し、固定ポートを占有しない。
- 書き込みは `spike/broker-state/` のみ。本体の `.state/` / state.db には触れない。
- 検証 pane は隔離環境に spawn し、既存の renga 組織ペインには触れない
  (adapter は自分が spawn した pane_id のみ操作する)。WezTerm は headless mux 上の
  新規 **論理**ウィンドウ (`cli spawn --new-window`。GUI ウィンドウは画面に出ない。
  詳細 [`ac9-wezterm-evidence.md`](./ac9-wezterm-evidence.md) §5 / #540)、
  tmux は専用 socket (`-L claude-org-spike`) 上の新規 detached session を使い、
  既存 tmux サーバーとも分離する。

## Phase K1（push 一次配送の批准前 HARD ゲート / Issue #22）

設計 SoT: [`broker-native-roles.md`](../docs/design/broker-native-roles.md) §9.5 /
[`ja-migration-plan.md`](../docs/design/ja-migration-plan.md) §8 K1 行。

**tool-less** な `claude/channel` stdio サーバー（ツール宣言ゼロ・`experimental{claude/channel}`
のみ）を `--dangerously-load-development-channels` で load し、idle セッションを **能動 poll なしに**
push で起こせるかの実機ゲート。判定の正本は [`RESULTS.md`](./RESULTS.md) の Phase K1 節。

| ファイル | 役割 |
|---|---|
| `k1_daemon.py` | push 一次配送 daemon（配送ライフサイクル §9.3 + delivery-scoped credential §9.4）。stdlib・localhost・隔離 state-dir |
| `channel_sidecar.py` | **tool-less** `claude/channel` stdio MCP サーバー（K1 の核心）。claim→push→confirm |
| `run_k1.py` | AC-1/2/4 実機ハーネス（実 claude TUI を tmux に spawn・反証可能な idle-wake 観測） |
| `run_k1_coexist.py` | AC-3 実機ハーネス（org-broker-channel + 隔離した実 claude-peers を同居 load） |
| `k1_smoke.py` | 配管スモーク（無課金・claude 不要） |

```bash
# 無課金・決定的（CI 常設）
python3 -m unittest discover -s tests -p "test_k1_channel.py"
python3 spike/k1_smoke.py

# 実機（WSL2 / tmux・課金あり・最小トークン）
cd spike
python3 run_k1.py --model sonnet            # AC-1/2/4: tool-less load + idle wake + 課金中立
python3 run_k1_coexist.py --model sonnet    # AC-3: renga(claude-peers 隔離実体) と coexist
```

実機検証は WSL2 / tmux（本番ホスト WezTerm 実機 AC は別 Issue #9）。broker daemon は
repo 外 WSL パス（`/tmp/claude/broker-k1-spike/*`）を `--state-dir` で渡し、本番 ja `.state/`・
本番 `~/.claude-peers.db` に一切触れず、検証後に破棄する。
