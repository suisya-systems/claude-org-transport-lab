# スパイク AC 判定結果

Phase 1（WezTerm / Windows）と Phase 2（tmux / POSIX）の AC 判定を記録する。
**Phase 2 の結果は本ファイル末尾の [Phase 2 節](#phase-2-tmux-backend--posix-wsl2--issue-2) を参照。**
以下はまず Phase 1（WezTerm）の記録。

## Phase 1 スパイク AC 判定結果（WezTerm / Windows）

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

## 総合判定（Phase 1 / WezTerm）

- **AC-2: GO** (4 項目 + 一往復すべて成立)
- **AC-1: 全 4 状態 GO**。自動 3 状態 (idle / 長文入力中 / ストリーミング中) +
  手動状態 2 (IME 変換中、2026-06-08 窓口 + ユーザー実施) がすべて合格。
- **Phase 1 ゲート通過** (AC-1 / AC-2 両方クリア)。Phase 2 へ進行可能。

---

## Phase 2 (tmux backend / POSIX WSL2 / Issue #2)

- 実施日: 2026-06-09
- 環境: WSL2 (Linux 6.6 / Ubuntu) / **tmux 3.4** / Claude Code (PATH の `claude`) /
  検証モデル: Sonnet / 検証用 Python: claude-org-ja の `.venv` (CPython 3.x)
- backend 抽象化: Phase 1 の WezTerm 専用ハーネスを backend パラメータ化した。
  共有面は [`terminal_adapter.py`](./terminal_adapter.py)（`TerminalAdapter` Protocol /
  `classify_pane_state` / `make_adapter` ファクトリ）に集約し、
  [`tmux_adapter.py`](./tmux_adapter.py) を第二実装として追加。
  `broker.py` / `harness.py` / `run_ac1.py` / `run_ac2.py` は backend 非依存化し、
  `--backend {wezterm,tmux}`（省略時は OS 自動: POSIX=tmux / Windows=wezterm）で切替。
- 判定スクリプト: `python mcp_smoke_test.py`（無課金）/
  `python run_ac2.py --backend tmux`（14:48 run）/
  `python run_ac1.py --backend tmux`（14:49 run）。
  機械可読データは `broker-state/{ac2,ac1}/result.json`（git 管理外、再実行で再生成可、`backend` フィールドに `TmuxAdapter` を記録）。

### tmux backend が WezTerm より素直な点（Issue #2 の活用ポイント）

| 操作 | WezTerm | tmux | 効果 |
|---|---|---|---|
| Enter（承認 / submit） | `send-text --no-paste` + `\r` | `send-keys Enter` | 小細工不要 |
| Ctrl+C（入力クリア） | `send-text --no-paste` + `\x03` | `send-keys C-c` | 小細工不要 |
| 1 行ナッジ注入 | paste + settle + CR | `send-keys -l -- <text>` + `Enter` | 一級プリミティブ |
| cursor 位置取得 | `get-text` 単体では不可（要別取得） | `list-panes -F` の `#{cursor_x/y}` に同梱 | tmux 優位 |
| GUI / display | mux-server のみで spawn 可 | detached session で標準動作 | tmux は CI 向き |

未送信複数行テキスト（AC-1 状態 3）のみ bracketed paste（tmux `paste-buffer -p`）を使う。
これは「改行を行ごとの submit に化けさせない」ための処理で、WezTerm でも同じ理由で必要（backend 差ではなく TUI 入力欄のセマンティクス）。`classify_pane_state` は受信側の Claude TUI が同一描画のため backend 非依存に共有でき、tmux `capture-pane` の scrape でも妥当だった（実測）。

### AC-2: 起動・接続チェーンの置き換え成立（tmux）— **総合 GO**

| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| 1 | `--mcp-config` 注入で spawn した対話ペインの Claude が broker MCP に接続。信頼確認は機械承認可能 | **GO** | detached tmux session に Claude TUI を spawn → initialize 到達。folder trust prompt が出現し `send-keys Enter` で機械承認。MCP trust prompt は出現せず（`--strict-mcp-config` 整合） |
| 2 | per-agent token の受け渡し・認証成立、from 帰属が token 由来 | **GO** | 検証 Claude が `send_message` を呼び、observer 受信の `from_id='claude-spike'` が token bind 表由来 |
| 3 | 登録検知が 〜30 秒で成立 | **GO** | spawn から **2.0 秒**で initialize 到達 → bind 表 registered |
| 4 | PTY send-text に文字化け・取りこぼしなし | **GO** | probe `日本語テスト：PTY経由①②③ｱｲｳ🎌𠮷`（全角記号 / 半角カナ / 絵文字 / サロゲートペア）が入力欄に無傷で出現（tmux `paste-buffer` の UTF-8 ラウンドトリップ） |
| - | (追加検証) ナッジ → `check_messages` 一往復 | **GO** | observer → enqueue → `send-keys` ナッジ → Claude が `check_messages` で本文取得（queue_drained）。本文は PTY 非経由 |

### AC-1: ナッジ注入の状態テスト（tmux）— **自動 3 状態 GO**

| # | 受信側の状態 | 判定 | 根拠 |
|---|---|---|---|
| 1 | idle | **GO** | defer 0 回で即時配達。ナッジが 1 メッセージとして履歴に出現、画面・履歴に乱れなし |
| 2 | IME 変換中 | **対象外（自動）** | Phase 1 と同じく自動化不能（grid scrape は PTY 文字 grid のみ観測し IME 変換窓・候補 UI を観測できない）。手動手順 [`manual-ime-test.md`](./manual-ime-test.md)。WSL2/tmux の手動 IME 検証は別途（窓口判断）|
| 3 | 長文入力中（未送信複数行） | **GO** | 静止確認が `input_pending` を検知し defer（早漏配達 0 件）。未送信テキスト無傷・ナッジ混入なし・勝手送信なし。クリア後に配達（defer-then-deliver 成立） |
| 4 | 出力ストリーミング中 | **GO** | busy 中は defer（state=busy の defer を journal で確認）。早漏配達 0 件（`nudge_sent` の ts と busy 終了時刻の比較）。出力末尾無傷。応答完了後にナッジ配達 → drain 成立 |

## 総合判定（Phase 2 / tmux）

- **AC-2: GO**（4 項目 + 一往復すべて成立、tmux 3.4 / WSL2）。
- **AC-1: 自動 3 状態 GO**（idle / 長文入力中 / ストリーミング中）。状態 2（IME 変換中）は
  Phase 1 同様に自動化対象外（手動）。
- **完了基準達成**: POSIX（tmux）/ Windows（WezTerm）の両 backend で AC-1（自動 3 状態）/
  AC-2 が green。能力表（[`docs/design/renga-decoupling.md`](../docs/design/renga-decoupling.md) §4.7）を
  実測値に固定し、messaging（Phase 3）/ full backend（Phase 4）の能力境界を 2 表に分離（§4.7.2）。

### 既知制限（Phase 2）

- **IME 状態 2 の tmux 手動検証は未実施**: 自動化不能の根拠は backend 非依存で不変。WSL2/Linux の
  日本語 IME 環境での手動実機確認は本タスクのスコープ外（窓口判断）。Phase 1 で Windows/Microsoft IME での
  手動合格は記録済み。
- **full backend tier（Phase 4 面）は未検証**: 本スパイクが両 backend で実証したのは
  messaging tier（send-text + grid scrape + 起動チェーン）。spawn / inspect_pane / poll_events の
  配線替えと実効遅延は Phase 4 スコープ（§4.7.2 (b)）。
- **codex セルフレビューの追レビュー範囲**: 本 Phase 2 差分のレビューは PR 本文に記載。

---

## Phase 3（メッセージング移行 / broker 配線 / Issue #3）

- 実施日: 2026-06-09
- 環境: WSL2（Linux 6.6）/ Python 3.x（claude-org-ja `.venv`）/ 検証モデル: Opus
- 検証方式: **B（broker queue 統合ハーネス / 無課金・決定的・CI 可）**。窓口経由のユーザー判断
  （2026-06-09）。実 Claude 4 ペインの課金実証（方式 A）や本体取り込みスコープの prose 書き換えは
  行わない。実セッション往復のリアルさ（PTY ナッジ打鍵・起動チェーン・UTF-8 文字化けなし）は
  Phase 1/2 の AC-1 / AC-2 が実 Claude TUI で既証（本ファイル上記）。Phase 3 は「broker 側が
  full cycle を構造的に支えられること」を [`run_ac3.py`](./run_ac3.py)（FakeAdapter で受信側の
  idle/busy/input_pending と pane 生死を決定的に駆動）で実証する。
- 判定スクリプト: `python run_ac3.py`（GO/NO-GO + `broker-state/ac3/result.json`）。
  CI 常設: [`tests/test_broker_phase3.py`](../tests/test_broker_phase3.py)（`unittest discover -s tests`
  が拾う。14 ケース green）。
- 実装差分: [`broker.py`](./broker.py) に token ライフサイクル本実装（`issue_token(ttl=)` /
  `authorize` / `revoke_token` / `revoke_pane` / `reap_exited_panes` / `close_pane` / `suspend`、
  新エラーコード `token_revoked` / `token_expired`）。`AgentBind` に `expires_at` / `revoked_reason` /
  `is_active()` / `auth_error()` を追加。

### AC-3: メッセージング移行の 1 委譲サイクル完走 — **総合 GO**

| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| AC-3-cycle | 6 経路全数往復 + token 由来 from 帰属 | **GO** | DELEGATE（secretary→dispatcher）/ ack（secretary→worker）/ 完了報告（worker→secretary）/ 判断仰ぎ（worker→secretary）/ CURATE_DONE（curator→dispatcher）/ retro gate（dispatcher→secretary）の 6 経路を 4 役割 token bind で全数往復。各 `from_id` が token bind 由来で正しく、ナッジ配達 + at-most-once drain（2 回目空）が全経路で成立 |
| AC-3-nudge | 静止確認 defer が busy / input_pending と共存、静止後配達 | **GO** | busy / input_pending の宛先で defer を 3 回記録 → idle 遷移後に `nudge_sent` 1 回・打鍵行が `NUDGE_TEXT`（本文は PTY 非経由）。idle 宛は defer 0 回で即時配達。早漏配達は構造的に不可（`_nudge_worker` は classify==idle のときのみ `send_line`） |
| AC-3-spoof | なりすまし送信が構造的に不可能 | **GO** | `call_tool` の arguments に `from_id` / `from_name` を偽装注入しても broker は token bind 由来で上書きし無視。`enqueue` 署名は `from_bind`（token 由来）のみで自己申告 from 文字列を受けない。revoke 済み token での送信は `[token_revoked]` で拒否 |
| AC-3-lifecycle | token ライフサイクル本実装 | **GO** | pane_exited（`reap_exited_panes`）/ `close_pane` で即時 revoke → 以後 `token_revoked` で全呼出拒否・list_peers / 配送先から消滅。TTL 超過で `token_expired`。`suspend` で全 token revoke → resume は別 token を再発行（旧 token 再利用不可、bind 表整合） |

### 帰属（なりすまし不可能性）の構造的根拠

- `enqueue(from_bind, to_id, message)` は **送信者の token bind のみ**を受け、`from_id` / `from_name` /
  `sent_at` を bind から付与する。クライアント自己申告フィールドを採る経路が API 署名レベルで存在しない。
- `to_id` は宛先解決にのみ使われ、`from` には一切影響しない（「他 agent の to_id を騙る」試行をしても、
  付与される from は常に送信者 token の agent_id）。
- 不正 / 失効 / TTL 超過 token は `authorize()` が HTTP 層（`do_POST`）で 401 + `[token_*]` として弾く。
  直呼び経路（server-side 合成役割）でも `enqueue` 冒頭の `from_bind.auth_error()` で失効送信者を拒否する。

### attention watcher 通知経路（§7.3 の「壊れないこと」）

- attention watcher（`/org-attention-start`）は **`.state/attention.json` / pending_decisions を監視して
  OS 通知する独立サイドカー**であり、renga チャネルのメッセージ注入を消費しない（state ファイル監視）。
  したがって broker への messaging 配線替えと **直交**しており、watcher 経路は本移行で改変されない。
- messaging 層で「通知経路が壊れない」に相当する保証は、**判断仰ぎ（escalation）が宛先 busy / 長文入力中でも
  ナッジ defer-then-deliver で確実に届くこと**であり、AC-3-nudge が実証する。

## 総合判定（Phase 3 / messaging）

- **AC-3: 全 4 項目 GO**（cycle / nudge / spoof / lifecycle）。**完了基準達成**: フォーク組織の
  messaging 1 委譲サイクル（6 経路）が renga チャネル不使用で broker queue を一巡し、全 from が
  token 由来で正しく付き、なりすましが構造的に不可能、token ライフサイクル（bind/revoke/TTL/再発行）が
  本実装で成立。無課金・決定的・CI 可・prose 非破壊の規律を維持。

### 既知制限（Phase 3）

- **方式 B の合成役割**: 4 役割は実 Claude セッションではなく token bind された合成役割（observer と同型）。
  token bind・帰属・queue・ナッジ機構は本物だが、「実 4 セッションが同時に喋った」課金実証（方式 A）は
  本体取り込みスコープに送る（ユーザー判断）。実セッション往復の実在性は Phase 2 AC-2 で既証。
- **prose 書き換え（分類 (a)）・契約改訂（Set D Surface 2/5・Set C inventory・non-goals §12）は未実施**:
  設計書 §7.3 が「本体取り込み時の同時変更」と位置付ける作業であり、ja 不可触制約（Epic #6 完動ゲート前）
  により本フォークでは行わない。本 Phase の成果物は broker 側の能力実証に閉じる。
- **TTL 既定値は None（失効なし）**: 設計書 §4.4 は「セッション寿命より長い TTL + 退役時 revoke」を基本とし
  TTL を保険と位置付ける。既定は長寿命（None）で、退役 revoke を一次担保とする。実運用 TTL 値の確定は
  本体取り込み時に行う。
- **codex セルフレビュー（full 検証深度、計 4 ラウンド・収束）**: 各ラウンドが並行性エッジを 1〜2 件ずつ
  収束方向に拾い、全件修正コミット済み。最終 round 4 は Blocker 0 / 残 Major 0（全エッジ解消）。
  - **round 1**（Major 2 / Minor 2 / Nit 1）: `call_tool` 冒頭で `auth_error()` 再検証（stale bind 直呼びの
    素通り遮断）/ `revoke_token` で当該 agent の未読キュー破棄 + `_nudge_worker` に `is_active()` ガード /
    idle nudge 検証を `nudge_sent`+`NUDGE_TEXT` まで強化 / `close_pane` は kill 失敗時に誤 revoke しない /
    `broker.py` 冒頭の旧記述更新。併せてハーネスの journal 同期 race を `_wait_event` で解消。
  - **round 2**（Major 2 / Minor 1 / Nit 1）: `_nudge_worker` の TOCTOU を send_line 直前のロック下
    active+pending 再確認で縮小 / TTL 失効時のキュー継承を `issue_token` の新規ライフサイクル検出で遮断 /
    `close_pane` を kill 後 `pane_exists` 生存確認に強化 + 回帰テスト追加。
  - **round 3**（Major 1）: nudge thread 再利用 race（dedup が `agent_id` 単位 `is_alive()` のみ）を
    新規ライフサイクル時の dedup エントリ破棄で遮断 + 決定的回帰テスト（pre-fix で空配達を再現確認）。
  - **round 4 / 最終**（Major 1 / Minor 1）: nudge dedup を token 有効性込みに強化し「生存するが宛先 token が
    失効済みの dying worker」を信用しない（同一 agent_id に別有効 token が残るケースを遮断）/ ハーネス
    `wait_nudge` を「過去に nudge があるか」から baseline 件数増加待ちに強化（同一 pane 2 通目以降の
    再発火退行を検出）+ 両者の決定的回帰テスト（pre-fix で空配達を再現確認）。
  - 窓口判断によりレビューは round 4 を最終ラウンドとして打ち止め（フォーク spike・完了基準は全 green 達成済み）。

---

## Phase 4（ペイン操作移行 / full backend adapter / Issue #4）

- 実施日: 2026-06-10
- 環境: WSL2（Linux 6.6）/ Python 3.12（claude-org-ja `.venv`）/ tmux 実機あり / 検証モデル: Opus
- 検証方式: **B（broker queue 統合ハーネス / 無課金・決定的・CI 可）を主**とし、窓口経由の
  人間判断（2026-06-10）で **実 tmux smoke** を追加。SoT [§7.4](../docs/design/renga-decoupling.md) の
  「該当 backend 実機で 1 サイクル完走」要件を、本 Linux/WSL2 環境では WezTerm 実機不可のため
  **正準 backend の tmux に読み替え**（Phase 2 の tmux 実機 AC 前例に沿う。WezTerm 実機 AC は follow-up）。
- 判定スクリプト: `python run_ac4.py`（GO/NO-GO + `broker-state/ac4/result.json`。`--no-real-tmux` で
  FakeAdapter のみ）。CI 常設: [`tests/test_broker_phase4.py`](../tests/test_broker_phase4.py)
  （FakeAdapter 5 検証 + 権限分離 / poll_events 境界 / send_keys 検証の単体、計 15 ケース green。
  実 tmux smoke は sandbox の unix socket 制約のため CI から除外）。
- 事前 codex design review 1 周（Blocker 1 / Major 5 / Minor 3）を実装前に全反映
  （[`spike/phase4-design-note.md`](./phase4-design-note.md)）。最重要は **balanced split の現行同等性**:
  現行 renga の split SoT は `claude_org_runtime.dispatcher.runner.choose_split`（doc prose は runtime と
  drift 済み: `_ROLE_PRIORITY` dispatcher=4・`SECRETARY_MIN_WIDTH=120`）であり、**broker は choose_split を
  再利用**して「現行同等」を再実装ではなく同一関数で構造的に保証した。

### 実装差分

- [`broker.py`](./broker.py): **role-scoped tool 公開**（messaging tier=worker/curator / ops tier=
  dispatcher/secretary。`tools/list` フィルタ + `call_tool` の `[tool_forbidden]` 二重遮断）。
  **ペイン操作 6 面**（`spawn_agent` / `close_pane` / `list_panes`(geometry) / `inspect_pane` /
  `send_keys` / `poll_events`）+ `set_pane_identity`（Surface 1.8 継承）を MCP surface に追加。
  **poll_events 合成**（list_panes 差分から `pane_started`/`pane_exited`/`events_dropped` を単一 lock 下で
  exactly-once 合成、`_known_panes` を record map にして exit 後も name/role/agent_id を payload に保持、
  初回 since=None は baseline、ring trim は count 付き `events_dropped`）。native pane id ↔ broker handle
  対応（MCP 面は handle で話し native を露出しない）。balanced split は `choose_split` 再利用。
- [`terminal_adapter.py`](./terminal_adapter.py): `TerminalAdapter` Protocol に `split` / `send_keys` を追加。
  `SEND_KEYS_VOCAB` / `normalize_key`（Set D §1.9 キー語彙の backend 横断正準）。
- [`tmux_adapter.py`](./tmux_adapter.py) / [`wezterm_adapter.py`](./wezterm_adapter.py): `split`
  （tmux `split-window -h/-v` / WezTerm `split-pane --horizontal`）・`send_keys`（tmux 一級キー名 /
  WezTerm 制御コード）を実装。WezTerm 側は Windows 専用のため本環境では parity 実装。

### AC-4: ペイン操作 6 面 + 監視 1 サイクル完走 — **総合 GO**

| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| AC-4-surface | 6 面 + identity が ops tier で往復、worker は構造的遮断 | **GO** | dispatcher token で 6 面往復。worker token では pane 操作が `tools/list` に出ず（4 面のみ）、`call_tool` も `[tool_forbidden]`。`spawn_agent` の MCP 応答に token 非露出（漏洩面限定）。未知キーは `[invalid-params]` |
| AC-4-events | poll_events 合成（baseline / 取りこぼし回復 / events_dropped count / meta） | **GO** | 初回 since=None は履歴 replay 無し。baseline→spawn→since 付き poll で `pane_started`（name/role/agent_id/handle 付き）観測。**broker 非経由の直 kill 取りこぼしが list_panes reconcile で `pane_exited` 回復**（meta 保持）。ring trim で count 付き `events_dropped` |
| AC-4-split | balanced split が現行同等 + capacity 検出 | **GO** | `claude_org_runtime.choose_split` 再利用。geometry 正規化（`left/top`↔`x/y`・`active`↔`focused`）後の判定が SoT と一致。候補空で `[split_capacity_exceeded]`（spawn 中止 = escalate 相当） |
| AC-4-cycle | delegate→spawn→監視→完了報告→CLOSE_PANE→retro 完走 | **GO** | delegate(secretary→dispatcher) → spawn_agent(balanced) → 監視(`inspect_pane` で承認待ち=input_pending / stall=連続 busy を**自己申告に依らず独立観測**) → 完了報告(token 由来 from) → CLOSE_PANE(`close_pane` で token revoke + `pane_exited`) → retro gate の 1 サイクルが renga 不使用で完走 |
| AC-4-cadence | 3 分 cadence の取りこぼし回復 | **GO** | worker クラッシュ（broker 非経由・イベント直接喪失）が次 cadence の list_panes reconcile で `pane_exited` 回復し、`reap_exited_panes` で token も revoke（監視ループの正しさを損なわない） |
| AC-4-real-tmux | 実 tmux で 6 面往復（無課金 smoke） | **GO** | 実 tmux で spawn / split / list_panes(geometry) / send_keys / inspect / poll_events(`pane_started`+`pane_exited`) / close を `cat` プロセスで往復実証（Claude 不要・無課金） |

## 総合判定（Phase 4 / pane control）

- **AC-4: 全 6 項目 GO**（surface / events / split / cycle / cadence / real-tmux）。**完了基準達成**:
  backend のみ（renga 不使用）で delegate → spawn → 監視（stall 検出 / 承認待ち観測）→ 完了報告 →
  CLOSE_PANE → retro の 1 委譲サイクルが AC harness で完走。poll_events ポーリング合成の取りこぼしが
  list_panes reconcile で回復し dispatcher 監視ループ（3 分 cadence）の正しさを損なわない。balanced split が
  backend geometry で現行（renga）と同等（`choose_split` 再利用で構造的保証）。dispatcher 向け broker MCP
  最小 surface を確定（worker/curator 非公開の権限分離）。無課金・決定的・CI 可・prose 非破壊の規律を維持。

### codex セルフレビュー（full 検証深度）

- **実装前 design review 1 周**（Blocker 1 / Major 5 / Minor 3）: 着手前に全反映（balanced split を
  `choose_split` 再利用に切替、poll_events 合成の lock/payload/baseline/events_dropped 詰め、tool_forbidden
  wire 形状・role 信頼境界の明記）。詳細は [`phase4-design-note.md`](./phase4-design-note.md)。
- **commit 後 self-review round 1**（Blocker 2 / Major 5 / Minor 1 / Nit 1）: 全 Blocker / Major を修正
  コミットで解消。
  - **Blocker**: (1) `set_pane_identity` の可変 `role` でのツール権限昇格 → **不変 `auth_role`**（issue_token
    確定）を権限 tier の唯一の根拠にし、表示 `role` と分離。(2) `spawn_agent` に token→worker の接続経路が
    無い → **token 先発行 + per-agent `--mcp-config`（0600）を起動 argv に注入**（§4.6 段階 1）+ split 失敗時
    revoke。
  - **Major**: pane_exited 合成時に token 未 revoke → **pane_exited で即時 revoke**（保留集合経由、§4.4）/
    native id を MCP payload に露出（handle 取り違え）→ **handle のみ露出**（list_panes / events / spawn 応答
    から native 除去）/ pane exit 時の handle 未掃除（native 再利用で stale）→ **exit で handle 対応を掃除** /
    split 失敗 `[io_error]` が adapter 例外文字列（将来 token-bearing argv）を素通し → **サニタイズ**（journal
    のみ）/ poll_events が `_lock` 保持で `list_panes` I/O → **I/O を `_lock` 外**へ（`_reconcile_lock` で
    合成を直列化、exactly-once 維持）。
  - **exactly-once の整理（Major への回答）**: Set D §3.1 の exactly-once は「イベント **emit** が close/crash
    ごとに 1 回」であり（`_diff_emit_locked` が単一 lock + `_reconcile_lock` 直列化で担保。回帰テストで反復
    reconcile でも pane_exited が 1 回を確認）、「1 reader へ 1 回 deliver」ではない。`poll_events` は renga と
    同型の **replayable な cursor 読み出し**（caller が next_since で前進）であり、cursor モデルとして正しい。
  - 回帰テスト追加（`tests/test_broker_phase4.py`）: 権限昇格不可 / crash→token revoke /
    config 注入 + split 例外時 revoke + 例外サニタイズ / payload に native id 非露出 / emit 1 回。
- **self-review round 2**（Major 2）: `close_pane` の MCP 応答が native `pane_id` を返していた → handle のみに /
  `spawn_agent` の `agent_id` が config ファイル名に無検証（path traversal で state_dir 外へ token 入り config）
  → `is_filename_safe([A-Za-z0-9_-])` で発行・書込み前に `[name_invalid]`。
- **self-review round 3**（Major 1 / Minor 2）: `spawn_agent` が同一 active `agent_id`/`name` の二重 spawn を許し
  inbox（agent_id 単位 queue）共有で message 横取り → `[name_in_use]` 拒否 / `close_pane` が pane 残存でも
  ok:true → pane_exists 確認で残存時 ok:false / `is_filename_safe` の `str.isalnum()` が Unicode 英数字を通す
  → ASCII 明示集合。
- **self-review round 4 / 最終**（Major 1）: 二重 spawn 拒否が check-then-act で並行 spawn race が残存 →
  **重複判定 + 予約を `issue_token(reject_if_active=True)` の単一ロック下に閉じ**、ThreadingHTTPServer 配下の
  並行二重発行を構造的に断つ（並行 12 スレッド発射で発行 1 回・有効 bind 1 本の決定的回帰テスト追加）。
  窓口判断により round 4 を絶対最終ラウンドとして打ち止め。残 Minor 1 件（`close_pane` は adapter 不通時に
  close 意図を尊重して ok:true ＝ 既存 `close_pane` 内部 API の「生存判定不能を退役扱いしない」方針と統一）は
  設計上の許容として残置（PR 既知制限に明記）。
- 収束: round1(Blocker2/Major5) → round2(0/2) → round3(0/1) → round4(0/1、修正済) → **Blocker/Major 残 0**。
  回帰テスト計 25 ケース green。

### 既知制限（Phase 4）

- **WezTerm 実機 AC は未実施（follow-up: Issue #9）**: 本環境は Linux/WSL2 のため WezTerm 実機不可。正準
  backend の tmux で実機 smoke を通した（人間判断で承認）。WezTerm の `split` / `send_keys` は parity 実装に
  留まり、Windows 環境での実機検証は別途 follow-up（Issue #9）。
- **実 tmux smoke は CI 非常設**: sandbox の unix socket 制約のため CI（`unittest discover`）からは除外。
  CI は FakeAdapter の決定的 15 ケースで常設化し、実 tmux smoke は `run_ac4.py` の手動ランナーで実証。
- **方式 B の合成役割**: 4 役割は実 Claude セッションではなく token bind された合成役割。pane 操作・
  geometry・poll_events 合成・権限分離は本物だが、実 Claude TUI 往復の実在性は Phase 1/2 AC で既証。
- **prose 書き換え（分類 (a)）・契約改訂（Set D Surface 1/3/4・Surface 8 案）は未実施**: 設計書 §7.4 が
  「本体取り込み時の同時変更」と位置付ける作業であり、ja 不可触制約（Epic #6 完動ゲート前）により本フォーク
  では行わない。本 Phase の成果物は broker 側の能力実証に閉じる。
- **balanced split の runtime 依存**: `choose_split` は `claude_org_runtime`（pyproject 既存依存）を lazy
  import する。未導入環境では `spawn_agent` の balanced split が失敗する（messaging 面は影響なし）。
- **`close_pane` の adapter 不通時の成否（codex round 4 残 Minor、設計上許容）**: kill 後に `pane_exists` が
  例外（adapter 不通）になるケースは、生存判定不能のため close 意図を尊重して ok:true を返す。これは既存
  `close_pane` 内部 API の「生存判定不能を退役扱いしない」方針と統一した挙動であり、adapter 健全時は pane 残存を
  ok:false で正しく弾く。adapter 不通という別事象は `poll_events` の reconcile が回復経路を持つ。

---

## Phase 5 / AC-5（完動ゲート dogfood / Issue #5 / Epic #6 最終ゲート）

- 実施日: 2026-06-10
- 環境: WSL2（Linux 6.6）/ Python 3.12（claude-org-ja `.venv`）/ tmux 3.4 実機 / 検証モデル: Opus（harness）+
  **実 Claude worker = Sonnet**（active 1 サイクル）/ codex 0.129.0
- 検証方式: **方式 B（FakeAdapter / 無課金・決定的・CI 可）を主**とし、(i) 実 tmux cat プローブ 2 サイクル smoke
  （無課金）と (ii) **実 Claude worker を active で 1 サイクル**回す真の end-to-end dogfood を追加（窓口経由で
  **人間が token コストを承知の上で承認**、2026-06-10。active サイクルは 1 回のみ）。
- 設計ノート: [`spike/ac5-design-note.md`](./ac5-design-note.md)（実装前 codex design review 1 周 = Blocker 2 /
  Major 7 / Minor 3 / Nit 1 を全反映）。最重要修正: (B1) AC-5-resume を既存 `suspend()`=未読 queue 破棄
  （`broker.py` `revoke_token`）に整合 → 「破棄 + 新 token / 新 queue 成立 + stale 非継承」へ再設計（isolation 証明が強化）、
  (B2) stall を観測だけでなく「観測後の broker 成立物 = escalation enqueue」まで必須 assert 化。
- 判定スクリプト: `python run_ac5.py`（方式 B のみ / 無課金・in-sandbox 可）、
  `AC5_REAL_CLAUDE=1 python run_ac5.py --real-tmux`（実機 dogfood / sandbox 無効）。
  CI 常設: [`tests/test_broker_dogfood.py`](../tests/test_broker_dogfood.py)（FakeAdapter 6 検証 + headless flag guard、
  `unittest discover` が拾う。**実 tmux smoke / 実 Claude active は CI 非常設** = unix socket / 課金のため）。

### Issue #5 完了基準（4 項目）と判定 — **総合 GO**

| # | 完了基準 | 判定 | 根拠（AC-5 検証項目） |
|---|---|---|---|
| 1 | backend(tmux)のみ・renga 不使用で 委譲サイクルを**複数回**完走 | **GO** | AC-5-multi: 単一 broker / adapter 上で **3 サイクル連続完走**（delegate→spawn→監視→完了報告→CLOSE_PANE→retro）。実機は cat 2 サイクル + 実 Claude active 1 サイクル |
| 2 | 障害系 broker 成立: stall検出 / escalation / handover / resume | **GO** | AC-5-stall / -escalation / -handover / -resume（下表） |
| 3 | 課金中立の実測（対話 TUI のみ・ヘッドレスに落ちない） | **GO** | AC-5-billing（argv builder 構造 / CI）+ 実 Claude の **ps 実 argv** が `claude --mcp-config --strict-mcp-config --allowedTools --model sonnet`（`-p`/`--print`/`--headless`/`--output-format` 非含有）+ 起動直後 idle `❯` 対話 TUI 描画 |
| 4 | 設計書最終版（tmux 格上げ + Phase 結果反映） | **GO** | [`docs/design/renga-decoupling.md`](../docs/design/renga-decoupling.md) を本 AC-5 結果で最終化（後述 §設計書最終化） |

### AC-5 検証項目（方式 B / FakeAdapter）— 全 6 項目 GO

| # | 項目 | 判定 | 根拠 |
|---|---|---|---|
| AC-5-multi | 3 サイクル連続完走 + cross-cycle isolation | **GO** | 各サイクルで delegate→spawn(balanced)→監視(inspect_pane で承認待ち/stall 独立観測)→完了報告(token 由来 from)→CLOSE_PANE(token revoke + pane_exited)→retro gate を完走。**native id 再利用を強制**しても handle は別採番・**旧 handle は pane_not_found**（新 pane に誤対応しない）・inbox / token / event cursor がサイクル間で漏れない・各サイクル終了時に全関係 inbox empty・二重 spawn は `[name_in_use]` |
| AC-5-stall | 連続 busy 独立観測 → stall 判定 → **escalation enqueue** | **GO** | dispatcher が `inspect_pane` で busy を threshold(3) 連続観測 → stall 判定（idle/input_pending は誤検出なし）→ **観測後の成立物として secretary へ escalation を broker enqueue**（from=dispatcher, token 由来） |
| AC-5-escalation | defer-then-deliver + 帰属 + 人間返答の worker 転送(at-most-once) | **GO** | 判断仰ぎが secretary busy 中 `nudge_deferred`（打鍵されず）→ idle 復帰で配達（from=worker, token 由来）→ 人間返答を secretary→worker へ broker 転送 → worker 側 1 通 drain・2 回目空（at-most-once） |
| AC-5-handover | ops tier 引き継ぎ + 監視 cursor 不喪失 | **GO** | secretary が ops tier `inspect_pane(dispatcher)` + `send_keys(/clear・/dispatcher-resume)` で**ペインを閉じず**引き継ぎ（dispatcher の `pane_exited` を emit しない・list 残存）。handover 中に発生した worker の `pane_exited` を **handover 前 cursor** から取りこぼさない |
| AC-5-resume | suspend(全revoke+未読破棄) → token 再発行 → stale 非継承 | **GO** | `suspend()` が全 token revoke（戻り値=revoke 数）+ 旧 token は `token_revoked`・失効 token からの送信も拒否 + suspend 前の未読を破棄（既存方針）→ resume は別 token を再発行（旧 token 再利用不可）→ 新 queue は空（旧 lifecycle 未読の**非継承**）→ 新 token で送受信成立 |
| AC-5-billing | 対話 TUI argv builder の構造保証 | **GO** | spawnable 各 role（worker/curator）の `spawn_agent` launch argv が `claude --mcp-config <0600 path>` のみで、`-p`/`--print`/`--headless`/`--output-format`/`--input-format` を構造的に含まない。平文 token も argv 非露出（0600 config path 参照） |

### 実機 dogfood（実 tmux + 実 Claude active 1 サイクル）— **GO**

人間承認（2026-06-10）に基づき、token コストを承知の上で **実 Claude worker を active で 1 サイクルのみ**実行した。

- **委託（broker 経由 / renga 不使用）**: synthetic dispatcher token から実 Claude worker（`claude-spike`, role=worker）へ
  broker queue で DELEGATE を enqueue → ナッジ配達 → worker が `check_messages` で受領。
- **実作業**: 実 Claude（Sonnet, 対話 TUI）が実 turn を実行（`2+2` を計算）。
- **完了報告（broker 経由 / token 由来 from）**: worker が `send_message(to_id='observer', …)` → broker queue →
  observer(secretary 相当) が `from_id='claude-spike'`（**token bind 由来・自己申告ではない**）で受領。
  本文 = `完了報告: 2+2=4 / dogfood active cycle 完走`。
- **クローズ**: `close_pane` で worker pane 退役 + token revoke（`closed=['claude-spike']`、以後 `token_revoked`）。
- **起動チェーン実測**: folder trust prompt を `send-keys Enter` で**機械承認** → 対話 TUI idle 到達 **2.0s** /
  broker 登録 **2.0s**（Phase 1/2 AC-2 の実測と整合）。
- **課金中立の実測（attestation）**:
  - 起動直後 idle で `❯` プロンプトの**対話 TUI 描画を観測**（ヘッドレス print-and-exit なら描画されない）。
  - 実行中 claude プロセスの **ps 実 argv** =
    `claude --mcp-config <0600> --strict-mcp-config --allowedTools mcp__org-broker__{send_message,check_messages,list_peers,set_summary} --model sonnet`。
    `-p`/`--print`/`--headless`/`--output-format`/`--input-format` を**一切含まない**（= ヘッドレスに落ちていない実測証跡）。
  - 機械可読の証跡: `broker-state/ac5/active-evidence.json`（git 管理外、再実行で再生成）。

### 総合判定（Phase 5 / AC-5 完動ゲート）

- **AC-5: 全 6 項目 GO**（multi / stall / escalation / handover / resume / billing）+ **実機 dogfood GO**
  （cat 2 サイクル smoke + 実 Claude active 1 サイクル完走）。
- **Issue #5 完了基準 4 項目すべて GO**。フォーク組織が **backend(tmux)のみ・renga 不使用**で 委譲サイクルを
  複数回完走し、障害系4種が broker 経由で成立し、課金中立（対話 TUI のみ・ヘッドレス非該当）を実測で確認、
  設計書を最終化。**Epic #6（Plan B / renga 依存解消）完動ゲート = GO**（フォーク側足切り通過）。
- 規律維持: 方式 B は無課金・決定的・CI 可・prose 非破壊。実 Claude は人間承認の 1 サイクルのみ（最小コスト）。

### codex セルフレビュー（full 検証深度）

- **実装前 design review 1 周**（Blocker 2 / Major 7 / Minor 3 / Nit 1）: 着手前に全反映（[`ac5-design-note.md`](./ac5-design-note.md)）。
- **commit 後 self-review**: 本 AC-5 差分（`run_ac5.py` / `test_broker_dogfood.py` / 文書）を `codex exec` 直打ちでレビューし、
  Blocker / Major を修正コミットで解消（詳細は PR 本文）。

### 既知制限（Phase 5 / AC-5）

- **実 Claude は active 1 サイクルのみ**: 複数サイクルの実 Claude 往復は実在性が Phase 1/2 AC-1/AC-2 で既証のため
  行わない（新規の構造的証拠は増えず token のみ消費）。複数サイクルの構造実証は方式 B（FakeAdapter, 3 サイクル）が担う。
- **課金中立は「対話 TUI / 非ヘッドレス」の実測 attestation**であり、課金メーター値の検証ではない（idle ❯ 描画 + 実 argv に
  headless flag なし、を実測する。active turn の課金は人間承認済み）。
- **実機分（実 tmux smoke / 実 Claude active）は CI 非常設**: sandbox の unix socket 制約 + 課金のため。CI は
  FakeAdapter の決定的 7 ケースで常設化（`tests/test_broker_dogfood.py`）。
- **実 Claude spawn は Phase 1/2 proven の対話起動（`adapter.spawn` + `--allowedTools`）を再利用**: balanced split
  経由の spawn_agent は cat プローブ smoke で実機実証済み（実 Claude での balanced split は本ゲートの要件外）。
- **prose 書き換え・契約改訂（Set D/C・non-goals §12）は本体取り込みスコープ**: ja 不可触制約（Epic #6 完動ゲート前）
  により本フォークでは行わない（Phase 3/4 と同じ）。
- **WezTerm 実機 AC は Issue #9（Windows 専用）**。本ゲートの backend は tmux 実機で実証（Phase 2/4 と同じ人間承認読み替え）。
