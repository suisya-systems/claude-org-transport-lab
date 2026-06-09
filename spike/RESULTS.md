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
