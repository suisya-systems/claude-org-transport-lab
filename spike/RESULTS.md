# Phase 1 スパイク AC 判定結果

- 実施日: 2026-06-08
- 環境: Windows 11 Pro (10.0.22631) / WezTerm 20240203-110809-5046fc22 /
  Claude Code 2.1.168 / Python 3.14.5 / 検証モデル: Sonnet 4.6
- 判定スクリプト: `run_ac2.py` (02:54 run) / `run_ac1.py` (02:54 run)。
  機械可読の生データは `broker-state/{ac2,ac1}/result.json` (git 管理外、再実行で再生成可)
- codex セルフレビュー round 1 (Blocker 1 / Major 2 / Minor 1 / Nit 1)・round 2
  (Blocker 1 / Major 2 / Minor 1) の全指摘を修正し、各 round 後の再実行で全項目 GO を再確認済み:
  - round 1: state4 早漏配達判定を「観測時点の状態」→「nudge_sent イベント ts と busy 終了時刻の比較」に修正 (Blocker)。broker に Mcp-Session-Id 検証を実装 — 実 Claude クライアントは session header を正しく往復し接続チェーンは GO のまま (Major)。tools/call の引数欠落を -32602 で応答 (Major)
  - round 2: DELETE ハンドラのロック内 _journal 呼出によるデッドロックを解消 (Blocker)。DELETE の session 不一致を 404 に統一 (Major)。AC-2-roundtrip 判定を今回 run のイベントのみに限定 — append-only journal の過去 run 残留による偽陽性排除 (Major)。smoke test に DELETE 失効回帰チェックを追加 (Minor)
  - round 3: DELETE で registered も落とし、未接続 / 切断済み client を list_peers・配送先から除外 (Major)。nudge worker の check-and-set をロック下に移し、並行 send_message での NUDGE_TEXT 二重注入を排除 (Major)
  - レビューは brief の 3 ラウンド上限で打ち切り (窓口指示)。round 3 指摘の修正は
    commit 済み・全検証 green 再確認済みだが、**round 3 修正分への追レビューは未実施**
    (既知制限として記録)

## AC-2: 起動・接続チェーンの置き換え成立 — **総合 GO**

| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| 1 | `--mcp-config` 注入で spawn した対話ペインの Claude が broker MCP に接続。信頼確認プロンプトは機械承認可能 | **GO** | MCP 接続成立 (initialize 到達)。folder trust prompt が出現し `send-text --no-paste + CR` で機械承認できた。**MCP サーバー固有の信頼確認プロンプトは出現せず** (`--mcp-config` ファイル + `--strict-mcp-config` の組合せでは project `.mcp.json` 系の承認 UI は出ない実測) |
| 2 | per-agent token の受け渡し・認証成立、from 帰属が token 由来 | **GO** | 検証 Claude が `send_message` を呼び、observer 側受信メッセージの `from_id='claude-spike'` が token bind 表由来で付与された (自己申告フィールドなし)。不正 token は 401 `[token_invalid]` で拒否 (smoke test) |
| 3 | 登録検知が 〜30 秒で成立 (bind 表ベース `list_peers` poll 同型) | **GO** | spawn から **2.5 秒**で initialize 到達 → bind 表が registered に遷移。現行 3-4 の 30 秒タイムアウト感を大幅に下回る |
| 4 | Windows (ConPTY) send-text に文字化け・取りこぼしなし | **GO** | probe 文字列 `日本語テスト：ConPTY経由①②③ｱｲｳ🎌𠮷` (全角記号 / 半角カナ / 絵文字 / サロゲートペア) が入力欄に無傷で出現。ナッジ定型行 (📨 + 日本語) も全 run で無傷 |
| - | (追加検証) ナッジ → `check_messages` 一往復 | **GO** | observer → enqueue → ナッジ打鍵 → Claude が `check_messages` で本文取得 (queue_drained)。本文は PTY 非経由 |

補足 (設計書 §4.6 への実測フィードバック):
- dev-channel prompt 相当の儀式は **folder trust prompt のみ**に縮退し、機械承認可能。
- spawn 時の `--strict-mcp-config` で既存 user/project MCP の混入は遮断された
  (検証セッションの MCP surface は org-broker の 4 ツールのみ)。

## AC-1: ナッジ注入の 4 状態テスト — **全 4 状態 GO（Phase 1 ゲート通過）**

| # | 受信側の状態 | 判定 | 根拠 |
|---|---|---|---|
| 1 | idle | **GO** | defer 0 回で即時配達。ナッジが 1 メッセージとして履歴に出現し、画面・履歴に乱れなし。`check_messages` で本文取得まで成立 |
| 2 | IME 変換中 | **GO** | 2026-06-08 窓口 + ユーザーが `manual_ime_session.py` で実施。全ケース合格 (詳細は下記「AC-1 状態 2 (手動) 記録欄」)。自動化不能の根拠は不変 (get-text は PTY 文字 grid のみ観測し IME 変換窓・候補 UI を観測できない) |
| 3 | 長文入力中 (未送信複数行) | **GO** | 静止確認が `input_pending` を検知し defer (早漏配達 0 件)。未送信テキスト無傷・ナッジ混入なし・勝手送信なし。入力欄クリア後に配達され取りこぼしなし (defer-then-deliver 成立) |
| 4 | 出力ストリーミング中 | **GO** | busy 中は defer (state=busy の defer を journal で確認)。早漏配達 0 件 (`nudge_sent` の ts と busy 終了時刻の比較で判定)。出力末尾まで描画無傷。応答完了後にナッジ配達 → `check_messages` 成立 (入力キュー滞留での消失なし) |

判定ロジックの要点 (詳細は `run_ac1.py`):
- 状態 3 / 4 は「きれいに注入できた」ではなく「**defer して静止後に配達し、
  かつ取りこぼさない**」(defer-then-deliver) を合格条件にしている。
- 早漏配達 = 静止前の `nudge_sent` を journal (queue.jsonl) で検出。

### AC-1 状態 2 (手動) 記録欄

| 実施日時 | 実施者 | IME | ケース A | ケース B | ケース C (idle 誤認) | 静止後ドレイン | 判定 |
|---|---|---|---|---|---|---|---|
| 2026-06-08 | 窓口 + ユーザー | Microsoft IME (Windows 11)、`manual_ime_session.py` 経由 | GO | GO | 入力破壊なし | GO | **合格** |

実施記録 (窓口 + ユーザーによる手動実施):

- **ケース A（変換中ホールド）: GO**。ナッジ注入時に未確定文字列「きょうのてんき」が
  一瞬視覚的にずれたが即座に元の状態へ復帰。変換窓・候補・未確定文字の破壊なし、
  ナッジ文字の混入なし。一過性の再描画ずれは「入力を壊す」に当たらず合格。
- **ケース B（確定競合 / 未送信テキスト滞留）: GO**。入力欄に未送信テキストがある間、
  broker は get-text で `input_pending` を検知しナッジを defer
  （attempt 12〜26 の連続 deferred を journal で実測）。ナッジ文字の混入なし、
  未送信テキストの勝手送信なし。
- **idle 誤認の所見**: ケース A の純粋な IME 変換中（入力欄に確定テキストがまだ無い瞬間）は
  broker が idle と誤認し nudge を即送信（attempt=1）したが、入力破壊は発生せず。
  get-text が IME 変換窓を観測できない既知制約の実機確認。
- **静止後ドレイン: GO**。入力欄クリア後にナッジが配達され、TEST-1〜10 まで全メッセージ
  欠落なく受信。defer→drain の一巡が成立。
- **判定: AC-1 状態 2 = 合格**。AC-1 全 4 状態 GO。**Phase 1 ゲート通過**。
- **副次知見**: WezTerm は GUI を起こさず mux-server だけで spawn が成立する
  （GUI は `wezterm-gui connect unix` で後付け attach 可能）。adapter は GUI 非依存で動作する。

## 実装中に確定した実測知見

1. **folder trust prompt の文言** (claude 2.1.168): "Quick safety check: Is this a
   project you created or one you trust?" — Enter 1 打で承認可能 (既定選択 = Yes)。
2. **MCP trust prompt は出ない**: `--mcp-config <file>` + `--strict-mcp-config` の
   組合せでは MCP サーバーの承認 UI は出現しなかった (project `.mcp.json` 経由
   ではないため)。
3. **入力欄クリアは Ctrl+C** (1 打)。Esc は入力をクリアしない (rewind 系 UI)。
   ナッジ配達の defer 解除待ちで入力放棄を観測する際の正準操作。
4. **入力プロンプトの描画**は水平罫線に挟まれた `❯ ` 行。busy 中は
   "(esc to interrupt)" がヒント行に出る。静止確認ヒューリスティックは
   この 2 シグナルで idle / input_pending / busy を分類できた。
5. **MCP 応答は plain `application/json` で受理される** (SSE 不要)。
   `notifications/initialized` は 202 空応答が正解 (JSON-RPC 応答を返さない)。

## 総合判定

- **AC-2: GO** (4 項目 + 一往復すべて成立)
- **AC-1: 全 4 状態 GO**。自動 3 状態 (idle / 長文入力中 / ストリーミング中) +
  手動状態 2 (IME 変換中、2026-06-08 窓口 + ユーザー実施) がすべて合格。
- **Phase 1 ゲート通過** (AC-1 / AC-2 両方クリア)。Phase 2 へ進行可能。
