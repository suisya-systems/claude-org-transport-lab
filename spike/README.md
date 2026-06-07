# renga 依存解消 Phase 1 スパイク (org-broker + WezTerm adapter)

設計 SoT: [`docs/design/renga-decoupling.md`](../docs/design/renga-decoupling.md)
§4 (broker / adapter 設計)・§7.1 (Phase 1 AC)。

このディレクトリは**使い捨て前提のスパイク**であり、本体の実装ではない
(broker / adapter の実体は Phase 3 以降に claude-org-runtime 側へ置く計画)。
本体の `.state/` / ポート / workers_dir には一切触れない (自己完結)。

## 構成

| ファイル | 役割 |
|---|---|
| `broker.py` | org-broker プロトタイプ。localhost (127.0.0.1) HTTP MCP サーバー + per-agent token 認証 + broker queue store + ナッジ配達 (静止確認 defer)。Python stdlib のみ |
| `wezterm_adapter.py` | WezTerm terminal adapter 最小実装 (spawn / send-text / get-text / list の 4 面、`--pane-id` 全呼出明示) + 画面状態ヒューリスティック |
| `harness.py` | AC 検証ハーネス (broker + adapter + 実 Claude TUI の結線、起動プロンプト機械承認) |
| `mcp_smoke_test.py` | MCP プロトコル層の合成クライアント検証 (Claude 不要・無課金) |
| `run_ac2.py` | AC-2 (起動・接続チェーン) 自動検証 |
| `run_ac1.py` | AC-1 のうち自動 3 状態 (idle / 長文入力中 / ストリーミング中) の自動判定 |
| `manual-ime-test.md` | AC-1 状態 2 (IME 変換中) の手動テスト手順書 |
| `manual_ime_session.py` | 同手動テスト用の対話セッション起動スクリプト |
| `probe_startup.py` | TUI 描画採取用 probe (較正用・使い捨て) |
| `RESULTS.md` | AC 判定結果の記録 (go/no-go) |
| `broker-state/` | broker queue store + 画面ダンプ等の実行時生成物 (git 管理外) |

## 実行手順

前提: Windows / WezTerm (20240203-110809 以降) / `claude` CLI / Python 3.x (`py -3`)。
WezTerm が PATH に無い場合は `C:\Program Files\WezTerm\wezterm.exe` を自動で使う。

```powershell
cd spike

# 1. プロトコル層 (無課金・Claude 不要)
py -3 mcp_smoke_test.py

# 2. AC-2: 起動・接続チェーン (対話型 Claude TUI を新規 WezTerm ウィンドウに spawn)
py -3 run_ac2.py

# 3. AC-1 自動 3 状態
py -3 run_ac1.py

# 4. AC-1 状態 2 (IME) — 手動。manual-ime-test.md の手順に従う
py -3 manual_ime_session.py
```

- spawn される Claude は**対話型 TUI セッションのみ** (`claude -p` / headless は
  課金制約により禁止。設計書 §1-1)。検証対話は最小トークン。
- 検証用 Claude は CLAUDE.md の無い一時 scratch ディレクトリ
  (`%TEMP%\broker-spike-*`) で spawn される (リポジトリの secretary CLAUDE.md を
  継承させないため)。
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
- 検証 pane は新規 WezTerm ウィンドウに spawn し、既存の renga 組織ペインには
  触れない (adapter は自分が spawn した pane_id のみ操作する)。
