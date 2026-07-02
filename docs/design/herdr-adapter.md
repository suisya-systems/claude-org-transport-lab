# Herdr terminal adapter 設計 — claude-org 第 3 端末バックエンド対応

> ステータス: **design only / 実装なし**。本リポジトリにこの設計の実装は一切存在しない（設計書のみ）。実体コード（Herdr adapter）は claude-org-runtime 側の `claude_org_runtime.terminal` 層に置く計画であり、本リポジトリ（transport-lab フォーク）には持ち込まない（[`docs/non-goals.md`](../non-goals.md) §6「PTY や端末多重化器の層を持たない」と整合）。
>
> **目的**: [Herdr](https://herdr.dev)（terminal workspace / pane / agent マネージャ。Socket API + CLI を公開）を、既存の `TerminalAdapter` 実装（WezTerm / tmux）に続く **第 3 の `TerminalAdapter` 端末バックエンド**として claude-org に対応させるための設計を固定する（renga は `TerminalAdapter` の外側の legacy MCP 経路 = opt-in fallback であり、本設計の decouple 対象。`TerminalAdapter` 実装としては数えない）。具体的には (1) Herdr Socket API と Contract Set D（[`backend-interface-contract.md`](../contracts/backend-interface-contract.md)）Surface 1 / 3 / 6 および `claude_org_runtime.terminal` adapter 基底（[`spike/terminal_adapter.py`](../../spike/terminal_adapter.py) の `TerminalAdapter` Protocol）とのマッピング、(2) ギャップ分析と adapter 側で埋める設計、(3) メッセージングは broker キュー継続とする構成、(4) capability probe を先頭に置く段階的導入計画。
>
> **最重要の設計判断（結論先出し）**: **adapter 境界は `claude_org_runtime.terminal` 層に置く**。Herdr は pane control / PTY / lifecycle を担う**端末バックエンド**であり、**Surface 2（Messaging）を持たない**。よって Herdr を broker / messaging adapter 層に混ぜてはならない — 混ぜると `send_keys`（raw PTY）と `send_message`（論理メッセージ）の**非同一性要件**（Set D Surface 1.9 / 2.1）と衝突する。Herdr は既存の WezTerm / tmux と同格の `TerminalAdapter` 実装として追加し、**メッセージングは broker キューが継続**する（[§5](#5-メッセージング構成--broker-キュー継続)）。
>
> 依存ドキュメント（参照は本設計書 → 既存文書の一方向のみ）:
> - [`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md)（Contract Set D、2026-05-03 批准。本設計の要求面の正本）
> - [`docs/design/renga-decoupling.md`](./renga-decoupling.md)（org-broker + terminal adapter 設計。特に §4.7 adapter 境界・能力表、§5 Set D 差分表。本書はこの構造を Herdr 向けに延長する）
> - [`docs/design/broker-native-roles.md`](./broker-native-roles.md)（受信挙動層。特に §9 push 一次配送 / `claude/channel` sidecar。本書の sidecar 共存前提の一次参照）
> - [`spike/terminal_adapter.py`](../../spike/terminal_adapter.py)（`TerminalAdapter` Protocol = マッピング対象の adapter 基底）
> - [`docs/non-goals.md`](../non-goals.md)（§6 PTY 層、§12 HTTP 外部公開）
>
> **一次情報**: Herdr 公式ドキュメント（[socket-api](https://herdr.dev/docs/socket-api/) / [cli-reference](https://herdr.dev/docs/cli-reference/) / [integrations](https://herdr.dev/docs/integrations/) / [plugins](https://herdr.dev/docs/plugins/)）。実測依存の項目（特殊キー対応・cwd 取得・geometry 精度・event overflow・sidecar spawn argv）は**並走中の herdr-socket-spike ワーカーの capability probe 結果で確定**するものとし、本書は probe 項目への対応関係を [§6](#6-capability-probe-対応表finding-i) で参照する形にとどめる（実測前に断定しない）。
>
> 事前 Codex design review（gpt-5.5）の指摘 (a)–(i) を本書に織り込み済み。各指摘の織り込み先は [付録 A](#付録-a-codex-design-review-指摘-ai-の織り込み対応表) の対応表で追跡する。

---

## 1. 背景と位置付け

### 1.1 Herdr とは

Herdr は「terminal workspaces / panes / agents と安定した CLI・Socket API」に責務を絞った端末ワークスペースマネージャである。ローカル Unix ドメインソケット（Windows は named pipe）上の**改行区切り JSON-RPC** で pane の spawn / split / list / read / 入力 / close、workspace / tab 管理、lifecycle イベント購読、agent 状態レポートを公開する。socket path は `HERDR_SOCKET_PATH` env / `--session` フラグ / `HERDR_SESSION` env / 既定セッション（`default`）から解決される（正確な優先順位と既定ソケットの実パスは実装依存であり、本設計の判断には効かないため断定せず probe / 実装確認に委ねる）。

claude-org の観点で重要なのは、Herdr が **renga / WezTerm / tmux と同じ「端末バックエンド」カテゴリ**に属し、かつ **renga 系より豊富なネイティブ機能**（構造化 pane API・イベントストリーム・agent 検出）を持つ一方で、**renga が持つ Surface 2 論理メッセージング（`send_message` / `list_peers` / `check_messages`）に相当する機構を持たない**ことである。Herdr の `agent.send` は agent ペインへ**テキストを注入**する操作（`send_keys` / `send_text` の同類）であって、送信者帰属（`from_id` / `from_name` / `sent_at`）付きの論理メッセージ配達ではない。

### 1.2 なぜ「第 3 バックエンド」か（adapter 境界の確定）

[`renga-decoupling.md`](./renga-decoupling.md) は既に **org-broker + terminal adapter** アーキテクチャを確立し、`TerminalAdapter` Protocol（[`spike/terminal_adapter.py`](../../spike/terminal_adapter.py)）の下に WezTerm（Windows 正準）と tmux（POSIX 正準）の 2 実装を持つ。broker / harness は `TerminalAdapter` 面と `make_adapter()` ファクトリ経由でのみバックエンドに触る。

**Herdr はこの `TerminalAdapter` の第 3 実装（`HerdrAdapter`）として追加する**。これが Codex 指摘 (a) の核心である:

- **Herdr = 端末バックエンド（pane control / PTY / lifecycle）**。担うのは Set D **Surface 1（Pane control）/ Surface 3（Events）/ Surface 4（Identity 部分）/ Surface 6（Error codes）** に対応する面のみ。
- **Surface 2（Messaging）は Herdr の責務ではない**。Herdr に messaging を担わせようとすると、Set D Surface 1.9 と 2.1 が要求する **「`send_keys`（raw PTY 入力）と `send_message`（論理メッセージ）は同一ではない」** という契約に反する。`agent.send`（テキスト注入）を `send_message` に見立てると、送信者帰属の token 由来付与（なりすまし不可、[`renga-decoupling.md`](./renga-decoupling.md) §4.4）も、push 一次の in-band 配達（[`broker-native-roles.md`](./broker-native-roles.md) §9）も成立しない。
- 従って **メッセージングは broker キューが継続**し（[§5](#5-メッセージング構成--broker-キュー継続)）、Herdr は broker の下位の端末バックエンドとして pane 操作・観測・イベントのみを提供する。broker / adapter の層関係は renga / WezTerm / tmux と完全に同型で、Herdr は「もう 1 つの `make_adapter()` の分岐」にすぎない。

```
  role sessions (secretary / dispatcher / worker / curator)
        │  MCP (--mcp-config)              ▲ claude/channel push (dev-channel sidecar)
        ▼                                  │
  ┌─────────────────────────────────────────────────┐
  │  org-broker  (Surface 2 messaging / queue store / │  ← messaging はここに留まる
  │  token bind registry / poll_events 正規化)         │     (Herdr は関与しない)
  └───────────────┬─────────────────────────────────┘
                  │  TerminalAdapter Protocol (spike/terminal_adapter.py)
       ┌──────────┼──────────────┐
       ▼          ▼              ▼
    WezTerm     Tmux        HerdrAdapter  ← 本設計で追加する第 3 実装
   (Windows)  (POSIX)      (Herdr Socket API: pane.* / events.*)

  renga は TerminalAdapter の外側の legacy MCP 経路 (mcp__renga-peers__*) = opt-in fallback。
  本アーキテクチャの decouple 対象であり TerminalAdapter 実装として数えない
  (spike/terminal_adapter.py VALID_BACKENDS = wezterm / tmux。renga は含まれない)。
```

### 1.3 本書のスコープと非スコープ

- **スコープ**: `HerdrAdapter` が満たすべき面のマッピング（Surface 1 / 3 / 6 + `TerminalAdapter` Protocol）、埋めきれないギャップと adapter / registry 側の補完設計、messaging=broker 継続の構成、capability probe を先頭に置く段階的導入計画。
- **非スコープ**: 実装コードそのもの（claude-org-runtime 側）、Set D 契約本文の改訂（改訂は取り込み時に別途 amendment PR）、renga / WezTerm / tmux の既存挙動変更、Herdr の外部公開（[`non-goals.md`](../non-goals.md) §12）。本書は「未実装の将来設計」であり、記述はすべて提案・計画である。

---

## 2. Herdr Socket API 概観（マッピングに使う面）

一次情報（[socket-api](https://herdr.dev/docs/socket-api/)）から、本設計のマッピングに関与する Herdr メソッドを抜粋する（メソッド名・パラメータ名は公式ドキュメント表記の verbatim）。

**Transport / 接続**: 改行区切り JSON-RPC over local socket。認証モデルは明示されておらず、**ソケットファイルへのローカルアクセス = 権限**という前提（[§4.6](#46-error-code-マッピングfinding-f) の adapter 到達性の扱いに影響）。`ping` で疎通確認。

**Pane control 系**:
- `pane.split`（params: `direction` right\|down, `ratio`, `env`, `pane_id`）— 分割 spawn。**socket の `pane.split` に `cwd` / `command` / `name` は無い**（CLI `herdr pane split` は `--cwd` / `--env` を持つが socket 面は env のみ。ラベル付与は `pane.rename(label)` 経由であり `pane split --label` は存在しない）。
- `pane.list`（filter: `workspace_id` / `tab_id`）→ `PaneInfo[]`。`pane.get`（`pane_id`）→ `PaneInfo` + optional `foreground_cwd`。`pane.current` → focused pane。
- `pane.layout`（`pane_id`）→ `area`（外形）・pane rectangles・split ratios・`focused_pane_id`。**geometry の一次ソースは `pane.list` ではなく `pane.layout`**。
- `pane.read`（`pane_id`, `source`: visible\|recent\|recent-unwrapped\|detection, optional `lines`）→ grid ベースの画面内容。`detection` は agent 画面スナップショット。
- `pane.send_text`（`pane_id`, `text`）/ `pane.send_keys`（`pane_id`, `keys` 文字列）/ `pane.send_input`（`keys` + `text` 複合）。CLI に `herdr pane run <id> <command>`（テキスト + Enter を atomic 送信）。
- `pane.process_info`（`pane_id`）→ shell PID・foreground processes（pid / name / **argv** / **cwd**）。
- `pane.rename`（`pane_id`, `label`）。`pane.close`（`pane_id`）。`pane.wait_for_output`（`pane_id`, `pattern` 正規表現, `timeout_ms`）。
- `pane.report_agent`（`pane_id`, `source`, `agent`, `state`: working\|blocked\|idle\|done\|unknown, `message`, optional `custom_status` ≤32 字）。`pane.report_metadata`（`title` ≤80 字, `state_labels`, `ttl_ms`, `seq` 等の表示専用 override）。

**起動系（CLI）**:
- `herdr agent start <name> [--cwd PATH] [--workspace ID] [--tab ID] [--split right|down] [--env KEY=VALUE] [--focus|--no-focus] -- <argv...>` — `--` 以降を起動プロセスの argv として渡す。`--env` は反復可。
- `herdr pane split ... [--cwd PATH] [--env KEY=VALUE]` + `herdr pane run <id> "<command line>"` の 2 段でも任意コマンドを cwd / env 付きで起動できる。

**Events 系**:
- `events.subscribe`（`subscriptions`: `{type, pane_id?}[]` フィルタ配列）→ ack 後、同一コネクション上に**イベントを push し続ける長命ストリーム**。
- `events.wait` — 特定イベント状態の one-shot 待機。
- イベント型: `pane.created` / `pane.closed` / `pane.exited` / `pane.focused` / `pane.moved` / `pane.agent_detected` / `pane.output_matched` / `pane.agent_status_changed`（他に workspace.* / worktree.*）。socket-api ドキュメント（2026-07 時点）の範囲では **`since` カーソル・初回リプレイ抑止・`events_dropped` overflow 通知・timeout 上限は見当たらない**。この否定能力は §4.5 の cursor/buffer 正規化設計の基盤前提であるため、**将来 Herdr がカーソル/timeout を追加した可能性を含め probe 4（event overflow）で「再開カーソル・timeout の有無」を実測して後決めする**（[§6](#6-capability-probe-対応表finding-i)）。

**Error model**: `{error: {code, message}}`。既知コード（socket-api ドキュメントの reason/code 一覧より）: `not_found` / `invalid_params` / `platform_unsupported` / `plugin_disabled` / `zoomed_tab` / `no_neighbor` / `same_pane` / `cross_tab` / `single_pane` / `already_zoomed` / `already_unzoomed`。**Set D 語彙（`pane_not_found` / `cwd_invalid` / `name_in_use` / `split_refused` 等）とは一致しない**。

**Env 注入**: Herdr は管理下 pane に `HERDR_SOCKET_PATH` / `HERDR_PANE_ID` / `HERDR_WORKSPACE_ID` / `HERDR_TAB_ID` 等を注入。呼出側 `--env` は起動プロセスに適用されるが、**Herdr 管理変数が衝突時は優先**（[§6](#6-capability-probe-対応表finding-i) probe 5 に影響）。

**Integration モデル（注意）**: Herdr の「integration」は agent を**引数付きで起動する**仕組みではなく、PATH 上の agent を**自動検出**し、各 agent の設定ディレクトリに**ネイティブ hook を書き込んで状態を socket へ報告させる**モデルである。すなわち Herdr は agent を「通して」ではなく「囲んで」オーケストレーションする。**spawn 時 argv 注入（`--mcp-config` / dev-channel sidecar）は integration 経路ではなく、`herdr agent start -- <argv>` または `pane split + pane run` の汎用起動経路で行う**（[§5.2](#52-spawn-時の-2-系統注入finding-d)）。

---

## 3. マッピング表

Set D 各 Surface の**要求**を左に、Herdr Socket API 面、`TerminalAdapter` Protocol 面、充足状況（○ ネイティブ充足 / △ adapter 補完で充足 / ✕ 不可・要 registry または非対応）を並べる。renga / WezTerm / tmux 列との比較は [`renga-decoupling.md`](./renga-decoupling.md) §4.7.1 を参照（本表はそこに Herdr 列を延長する位置づけ）。

### 3.1 Surface 1（Pane control）↔ Herdr ↔ `TerminalAdapter`

| Set D Surface 1 | 要求（要旨） | Herdr method | `TerminalAdapter` 面 | 充足 |
|---|---|---|---|---|
| 1.1 spawn (generic) | split で pane 生成。`direction` REQUIRED、`cwd`（layout mutation 前検証）・`name`・`role`・`command` optional | `pane.split`（socket, env のみ）+ CLI `herdr pane split --cwd/--env` + `herdr pane run`（command）/ `herdr agent start -- <argv>` | `split(target, argv, cwd, direction)` | △ — cwd/command は CLI 経路併用で充足。name/role は registry 補完（[§4.2](#42-identity--registry-補完finding-b)）。cwd 前検証は adapter 側（[§4.6](#46-error-code-マッピングfinding-f)） |
| 1.2 spawn (Claude 便宜) | dev-channel flag 注入済みの Claude pane（**end behavior が REQUIRED、便宜 op は OPTIONAL**） | `herdr agent start <name> -- <claude argv...>` / `pane split + pane run` | （broker が argv 合成） | △ — Herdr に Claude 専用 helper は無いが、汎用起動経路で **`--mcp-config` + `--dangerously-load-development-channels` を broker が argv 合成**して充足（[§5.2](#52-spawn-時の-2-系統注入finding-d)） |
| 1.3 spawn (Codex 便宜) | 同上（OPTIONAL） | 同上（汎用経路） | 同上 | △ — 現行 org-start/delegate は Codex peer を spawn しないため参考。汎用経路で充足可 |
| 1.4 close | プロセス終了 + 除去。**`pane_exited` を close ごとに exactly-once**。error: `pane_not_found`/`pane_vanished`/`last_pane` | `pane.close` | `kill_pane(pane_id)` | △ — close 自体は○。`pane.closed` / `pane.exited` の二重発火を adapter が exactly-once 正規化（[§4.5](#45-events-adapter-設計finding-e)）。**`last_pane` ガードは adapter が close 発行前に現タブ pane 数を判定して合成**（[§3.3](#33-surface-6error-codes-herdr) 表） |
| 1.5 list_panes | 全 pane 列挙。**`x`/`y`/`width`/`height`（cell 単位）が REQUIRED**、`cwd`・`summary`・client kind / receive mode | `pane.list`（→ `PaneInfo[]`）+ `pane.layout`（rectangles/ratios）+ `pane.get`（`foreground_cwd`） | `list_panes()` | △ — 列挙○。**geometry は `pane.layout` から取得し cell 単位へ換算**（probe 3 で精度確定、[§4.3](#43-geometry-の-cell-単位換算finding-i-probe-3)）。summary / kind / receive_mode は registry 補完 |
| 1.6 focus | フォーカス移動（correctness 非依存、SHOULD） | `pane.focus_direction` / `workspace.focus` / `tab.focus` | （Protocol 外） | ○ — 方向指定 focus あり。harness correctness は非依存 |
| 1.7 inspect_pane | 画面 grid scrape。`lines`・`format`（text\|grid）・`include_cursor`。**REQUIRED**（観測ベース安全性の基盤） | `pane.read`（`source`, `lines`） | `get_text(pane_id, escapes)` | △ — grid scrape○。**`format=grid` は Herdr ネイティブ grid、`format=text` は行結合**。**`include_cursor` は Herdr `pane.read` に無い → ギャップ**（[§4.4](#44-inspect_pane-の-format--cursorfinding-h)） |
| 1.8 set_pane_identity | `name` / `role` の再設定。**name は空・全桁数字・衝突・`[A-Za-z0-9_-]` 外を禁止**。org-start 0.3 secretary identity recovery に必須 | `pane.rename`（`label` のみ、検証なし・role 概念なし） | （Protocol 外 / broker） | ✕→△ — **Herdr は識別を担えない**。name/role・検証・衝突検出は **broker/runtime の spawn-time registry で補完**（[§4.2](#42-identity--registry-補完finding-b)。Codex 指摘 (b)） |
| 1.9 send_keys (raw PTY) | `Enter`/`Tab`/`Shift+Tab`/`Esc`/`Backspace`/`Delete`/`Up`\|`Down`\|`Left`\|`Right`/`Home`/`End`/`PageUp`/`PageDown`/`Space`/`Ctrl+<A-Z>`。**REQUIRED**（dev-channel 承認・Shift+Tab・Ctrl+C・Esc 介入） | `pane.send_keys`（`keys` 文字列。special: enter/esc, modifier: ctrl+h/alt+x/shift+tab, function, named punct） | `send_keys(pane_id, text, keys, enter)` | △ — enter/esc/shift+tab/ctrl+&lt;x&gt; は**構文としては doc に存在**するが、**TUI への意味論的到達（Esc の modal escape / Ctrl+C の SIGINT / Shift+Tab の permission-mode トグル）と arrows/home/end/pageup/pagedown/backspace/delete/tab/space の可否は probe 1 で確定**（Enter の到達は probe 5 の承認プロンプト機械承認で担保）（[§4.1](#41-send_keys-特殊キー適合性finding-c)。Codex 指摘 (c)） |

### 3.2 Surface 3（Events）↔ Herdr

| Set D Surface 3 | 要求 | Herdr | 充足 |
|---|---|---|---|
| 3.1 poll_events `since` カーソル | opaque cursor で resume、フィルタ時もカーソル前進 | `events.subscribe` は長命 push ストリーム。**`since` カーソル無し** | △ — adapter が subscribe を ring buffer 化し**シーケンス番号をカーソルとして採番**（[§4.5](#45-events-adapter-設計finding-e)） |
| 3.1 初回「今以降」 | `since` 省略時はリプレイ無し | `events.subscribe` は購読時刻以降を push するが、adapter 起動〜呼出側の初回 poll の間に蓄積したイベントが buffer に残る | △ — adapter 補完。**初回 now =「呼出側の最初の poll 時点で既知の最新シーケンスに cursor を置きリプレイしない」**と定義し、subscribe 確立時刻から切り離す（[§4.5](#45-events-adapter-設計finding-e)） |
| 3.1 最小語彙 `pane_started`/`pane_exited`/`events_dropped` | 必須イベント型 | Herdr: `pane.created`/`pane.exited`/`pane.closed`。**`events_dropped` 相当無し** | △ — 型名を Set D 語彙へ写像。`pane.created`→`pane_started`、`pane.closed`+`pane.exited`→`pane_exited`（exactly-once 正規化）。**`events_dropped` は adapter の buffer overflow 検出で合成**（probe 4） |
| 3.1 `timeout_ms` 30s cap | ≤30000ms、超過はクランプ | Herdr subscribe に timeout 概念無し（常時 push） | △ — adapter が poll_events 面で 30s クランプを実装 |
| 3.1 cursor-loss recovery | best-effort + `list_panes` reconcile（Q9） | — | △ — buffer overflow 時 `events_dropped(count)` → 呼出側は `list_panes` reconcile。tmux adapter と同型（[`renga-decoupling.md`](./renga-decoupling.md) §7.4） |

Herdr は WezTerm / tmux より**有利**（ネイティブのイベントストリームがある）だが、Set D の**カーソル・timeout・overflow 通知の意味論は adapter 側で正規化する必要がある**（Codex 指摘 (e)）。

### 3.3 Surface 6（Error codes）↔ Herdr

列 1「adapter 出口コード」は adapter/registry が呼出側へ返す正規化後コード（Set D 6.1 語彙 + runtime 拡張）。`adapter_unavailable` のみ Set D 6.1 契約語彙の外（renga-decoupling §5 が Set D 6.2「New codes MAY be added」規定内で新設した runtime 拡張）で、他は Set D 6.1 由来。

| adapter 出口コード | 契機 | Herdr raw | adapter 写像方針 |
|---|---|---|---|
| `pane_not_found` | target 未解決 | `not_found` | `not_found`（pane-addressed op）→ `pane_not_found` |
| `pane_vanished` | 解決後に消失（race） | （明示コード無し） | op 実行中の `not_found` を race 検出して `pane_vanished` |
| `split_refused` | pane cap / MIN サイズ | （明示コード無し。`invalid_params` 等の可能性） | split 系の cap/サイズ拒否を `split_refused` に写像（probe で raw 形確認） |
| `last_pane` | 唯一タブの唯一 pane の close | （Herdr は明示コード無し） | **adapter が close 発行前に現タブ pane 数を判定**し、唯一 pane なら layout を変えず `last_pane` |
| `cwd_invalid` | cwd 不在/非ディレクトリ。**layout mutation 前** | （Herdr は cwd 検証コード無し） | **adapter が split/agent start 発行前に cwd を検証**して `cwd_invalid`（[§4.6](#46-error-code-マッピングfinding-f)。Codex 指摘 (f)） |
| `name_in_use` | 名前衝突 | （Herdr `label` に一意制約なし → Herdr は衝突コードを発行しない） | **registry の衝突検出**で `name_in_use`。`name_taken`（broker-dogfood-runbook の既存ラベル由来の異表記）は出口で **`name_in_use` に正規化**（[§4.2](#42-identity--registry-補完finding-b)） |
| `name_invalid` | 空・全桁数字・不正文字 | （Herdr 検証なし） | registry 検証で `name_invalid` |
| `invalid-params` | 入力検証失敗 | `invalid_params` | `invalid_params` → `invalid-params`（表記正規化） |
| `backend_unreachable` | broker / MCP 不通 | — | **broker MCP 到達不能**時。adapter 不通とは別コード |
| `adapter_unavailable`（Set D 6.1 外・runtime 拡張） | **端末バックエンド（Herdr socket）不通**。broker は生きている | Herdr socket 到達不能 / `server.stop` 後 | **adapter 不通を `adapter_unavailable` に分離**（broker 到達不能 `backend_unreachable` と区別。Codex 指摘 (f) の「no_backend」概念に対応。[§4.6](#46-error-code-マッピングfinding-f)） |

> **命名の整合注記**: Codex 指摘 (f) は「adapter 不通は no_backend」と表現し、実際 [`broker-dogfood-runbook.md`](../operations/broker-dogfood-runbook.md) には `[no_backend]`（注記「= adapter_unavailable」付き）表記が残る。一方で実装（`spike/broker.py`）と [`renga-decoupling.md`](./renga-decoupling.md) §5 Surface 6 新設コードは同概念を **`adapter_unavailable`** の名で採用済みである。本書は実装表記に合わせ **`adapter_unavailable` を正準**とする（指摘 (f) の要求 =「adapter 不通と broker/MCP 不通を別コードに分離する」は満たす）。runbook 側の `no_backend` 表記の整合（→ `adapter_unavailable`）は本体取り込み時の別スコープ ToDo とする。`name_taken`↔`name_in_use` は **`name_in_use` を正準表記**とし、いずれの異表記も adapter/registry 出口で `name_in_use` に正規化する。

### 3.4 Surface 4（Identity & addressing）↔ Herdr

| Set D Surface 4 | 要求 | Herdr | 充足 |
|---|---|---|---|
| 4.1 numeric id + stable name | 両識別子。全桁数字文字列は id 解釈 | Herdr は `pane_id`（native handle）+ `label`（一意制約なし） | △ — native id ↔ broker handle 対応 + name は registry。全桁数字禁止は registry 検証 |
| 4.2 single-tab MUST | 全 pane-addressed op が現タブのみ解決（Q10） | Herdr は workspace / tab / pane 階層を持ち、`pane.list` に `tab_id` フィルタ | △ — adapter が **単一 tab スコープを強制**（`tab.create` を orchestrator 用に使わず、全 pane を単一 tab に spawn）。renga/WezTerm/tmux と同型 |

### 3.5 Surface 2（Messaging）↔ Herdr — **非対応（broker 継続）**

| Set D Surface 2 | Herdr | 判断 |
|---|---|---|
| 2.1 send_message（from 帰属 push 配達） | `agent.send`（テキスト注入。帰属なし） | **broker が継続**。Herdr は関与しない（[§5](#5-メッセージング構成--broker-キュー継続)） |
| 2.2 list_peers | （無し。`agent.list` は状態列挙であって peer channel ではない） | **broker が継続** |
| 2.3 check_messages | （無し） | **broker が継続**（三状態 drain、[`broker-native-roles.md`](./broker-native-roles.md) §9.3） |
| 2.4 set_summary | `pane.report_metadata`（`title` ≤80 字）が近いが 256 字上限・意味論が別 | **broker が継続**（set_summary は broker registry の面）。`report_metadata` は表示補助に留める |

---

## 4. ギャップ分析と adapter 設計判断

マッピングで △ / ✕ となった面について、`HerdrAdapter` および broker/runtime registry 側で埋める設計を固定する。各項は Codex 指摘 (b)–(h) に対応する。

### 4.1 send_keys 特殊キー適合性（finding c）

Set D Surface 1.9 は **raw key vocabulary が REQUIRED**。Herdr `pane.send_keys` はドキュメント上、special keys（enter, esc）・modifier chords（ctrl+h, alt+x, **shift+tab**）・function keys・named punctuation を受理する一般キー構文を持つ。適合性マトリクスを次のとおり固定し、**未確認セルは probe 1 で確定するまで「未確定」とし、満たせない場合は capability gap / non-conformance として明示する**（断定しない）。

状態欄は 2 軸で読む: **構文** =「Herdr `pane.send_keys` の doc verbatim にキー名が存在するか」、**意味論到達** =「送出したキーが Claude TUI に意図した効果として届くか（probe 実測が要る）」。

| Set D 要求キー | 用途（Set D） | Herdr 対応 | 状態（構文 / 意味論到達） |
|---|---|---|---|
| `Enter`/`Return` | 送信確定・dev-channel 承認 | `enter`（special）/ `herdr pane run` の atomic Enter | 構文○ / 意味論○（dev-channel 承認 Enter の到達は probe 5 で担保） |
| `Esc`/`Escape` | modal escape・over-validation 介入 | `esc`（special） | 構文○ / 意味論△（modal escape 到達を probe 1） |
| `Shift+Tab`/`BackTab` | **permission-mode トグル** | `shift+tab`（modifier chord） | 構文○ / 意味論△（permission-mode トグル到達を probe 1） |
| `Ctrl+<A-Z>`（特に `Ctrl+C`） | interrupt | `ctrl+<x>`（modifier chord。ドキュメントに ctrl+h の例） | 構文○ / 意味論△（`Ctrl+C` の SIGINT 到達を probe 1） |
| `Up`/`Down`/`Left`/`Right` | カーソル/選択移動 | 一般キー構文（up/down/left/right 想定） | 構文△（doc 未列挙・想定）/ 意味論△（probe 1 で可否確定） |
| `Backspace`/`Delete`/`Tab`/`Space` | 編集/補完 | 一般キー構文想定 | 構文△（doc 未列挙・想定）/ 意味論△（probe 1） |
| `Home`/`End`/`PageUp`/`PageDown` | スクロール/移動 | 一般キー構文想定 | 構文△（doc 未列挙・想定）/ 意味論△（probe 1） |

`HerdrAdapter.send_keys` は `terminal_adapter.py` の `normalize_key` / `SEND_KEYS_VOCAB`（正準キー名）を Herdr ネイティブ構文（例: `Shift+Tab`→`shift+tab`、`Ctrl+C`→`ctrl+c`、`Esc`→`esc`）へ写像する薄い変換表を持つ。**probe 1 で欠落が判明したキーは、当該キーを使う harness フロー（例: PageUp スクロール）を non-conformance として明記し、代替（`pane.read` の `source=recent` でのスクロールバック取得等）を併記する**。dev-channel 承認（`send_keys(enter=true)`）・Ctrl+C interrupt・Esc 介入・Shift+Tab トグルの 4 つは harness correctness に load-bearing なため、**この 4 つのいずれかが probe で満たせない場合は Herdr backend の採用を Phase 単位で保留する**判断基準を [§7](#7-段階的導入計画) に置く。実測の割当は **Enter の到達を probe 5（sidecar spawn argv の「承認プロンプト機械承認可否」）が、Ctrl+C / Esc / Shift+Tab の到達を probe 1 が**それぞれ担う（4 キーとも実測で裏付けられ、保留ゲートに空振り分岐が無いようにする）。

### 4.2 identity / registry 補完（finding b）

Herdr は `pane.rename`（自由形式 `label`、一意制約・role 概念・検証なし）しか持たず、Set D Surface 1.8 `set_pane_identity` の契約（name の非空・非全桁数字・衝突禁止・`[A-Za-z0-9_-]` 制約、role 分離）を満たせない。また Herdr に無い **`cwd`（spawn-time identity として）/ `role` / client `kind`（push/poll）/ `receive_mode`** も同様である。

**設計判断（Codex 指摘 (b)）**: これらを **Herdr に押し込まず、broker/runtime 側の spawn-time registry で補完する**。この registry は [`renga-decoupling.md`](./renga-decoupling.md) §4.4 の **token ↔ pane/session bind 表**と同一物であり、新規機構ではなく既存 bind 表の属性拡張として実現する:

- **spawn 時**に broker が `name` / `role` / `kind` / `receive_mode` / `cwd` を registry に記録する（Herdr の native `pane_id` を key に）。`set_pane_identity` 相当（rename / reassign）は registry の更新であり、Herdr の `pane.rename`（`label`）は**人間可読の表示補助としてベストエフォート同期する**にとどめる（正本は registry）。
- **name 検証・衝突検出**（`name_invalid` / `name_in_use`）は registry が行う。全桁数字禁止（Surface 4.1: 全桁数字は id 解釈のため name にできない）・`[A-Za-z0-9_-]` 制約・現タブ内一意も registry が強制する。
- **org-start 0.3 の secretary identity recovery** は registry を正本に動く。Herdr の `label` に依存しないため、`renga --layout ops` 外起動でも registry から secretary の identity を回復できる。
- `list_panes` / `list_peers` が返す `name` / `role` / `kind` / `receive_mode` / `summary` は registry から充填し、geometry / cwd の観測値のみ Herdr（`pane.layout` / `pane.get`）から取る。

### 4.3 geometry の cell 単位換算（finding i, probe 3）

Set D Surface 1.5 は **`x`/`y`/`width`/`height` を cell 単位で全 `list_panes` レコードに要求**（balanced-split scheduling が依存）。Herdr の geometry 一次ソースは `pane.layout`（`area` 外形・pane rectangles・split ratios）であり、`layout.export` は **fractional な BSP ratio** を返す。

**設計判断**: `HerdrAdapter.list_panes` は `pane.layout` の `area`（cell 単位の外形が取れる場合）と pane rectangles / ratios から **cell 単位の `x`/`y`/`width`/`height` を算出**する。Herdr が rectangles を cell 単位で返すか、fractional ratio のみかは **probe 3（geometry 精度）で確定**する:

- **cell 単位の rect が取れる場合**: そのまま Set D レコードに載せる。
- **fractional ratio のみの場合**: `area` の cell 寸法 × ratio で cell 単位へ換算する。丸め誤差が balanced-split の choose_split（[`renga-decoupling.md`](./renga-decoupling.md) §7.4 で再利用する `claude_org_runtime.dispatcher.runner.choose_split`）に与える影響を probe 3 で実測し、許容できなければ non-conformance として split 戦略の調整を [§7](#7-段階的導入計画) に回す。

### 4.4 inspect_pane の format / cursor（finding h）

Set D Surface 1.7 `inspect_pane` は `format`（text\|grid）・`include_cursor`・`lines` を要求。Herdr `pane.read` は `source`（visible\|recent\|recent-unwrapped\|detection）と `lines` を持ち、**grid ベースの内容を返すが `include_cursor` に相当する cursor 情報を持たない**。

**設計判断（Codex 指摘 (h)）**:
- **`format=grid`**: Herdr `pane.read`（`source=visible`）のネイティブ grid 出力を Set D の grid 形（`{lines: [{row, text}]}`）へ整形する。
- **`format=text`**: grid 行を結合してテキスト化する。
- **`lines`**: `pane.read` の `lines` に直接写像。スクロールバックが要る場合は `source=recent` / `recent-unwrapped` を選択。
- **`include_cursor`**: **Herdr `pane.read` は cursor を返さないためギャップ**。adapter は `include_cursor=true` でも cursor を **`null` / 省略（best-effort 欠落）** として返し、これを**明示の capability gap** として記録する。根拠: 受信側状態判定（`classify_pane_state`、[`spike/terminal_adapter.py`](../../spike/terminal_adapter.py)）は **busy マーカー文字列と入力プロンプト行**で判定し cursor を使わない。承認プロンプト観測（dispatcher watch loop）も grid テキストで足りる。よって cursor 欠落は harness correctness を壊さない degrade である。tmux が `list-panes` の `#{cursor_x/y}` で cursor を同梱できた（[`renga-decoupling.md`](./renga-decoupling.md) §4.7.1）のに対し Herdr は劣位だが、probe で cursor の別ソース（`pane.detection` スナップショットに含まれる可能性）が判明すればそこから充填する余地を残す。

### 4.5 events adapter 設計（finding e）

Herdr `events.subscribe` は**カーソル無しの長命 push ストリーム**、`events.wait` は one-shot。Set D Surface 3.1 poll_events の意味論（`since` カーソル・初回 now・30s cap・フィルタ時カーソル前進・`events_dropped(count)` + `list_panes` reconcile）を **adapter の event buffer と cursor 正規化で構築する**（Codex 指摘 (e)）。tmux adapter の poll_events 合成（[`renga-decoupling.md`](./renga-decoupling.md) §7.4、単一 lock 下 exactly-once・`_known_panes` record map・count 付き `events_dropped`）と同型だが、Herdr は**ネイティブイベントがあるぶん list ポーリング合成より粒度・遅延が良い**。

- **単一の長命 `events.subscribe` コネクション**を adapter が保持し、受信イベントを **ring buffer** に投入、**単調増加のシーケンス番号を採番**する。このシーケンスが Set D の opaque cursor（`next_since`）になる。
- **`poll_events(since, timeout_ms, types[])`**: buffer から `since` 超のイベントを返す。`types[]` でフィルタしても**カーソルは filtered-out を含めて前進**する（Set D 要件）。フィルタ不一致イベントが long-poll 中に到来した場合は **`events: []` と前進済みカーソルで早期リターン**し、呼出側の再 poll を前提とする。`timeout_ms` 未指定時は **Set D 既定 2000ms**、指定時は **30000ms にクランプ**、`0` は非ブロッキング drain。
- **初回（`since` 省略）**: cursor を**呼出側の最初の poll 時点で既知の最新シーケンス**に置き、**それ以前のイベントはリプレイしない**。ここで基準を「adapter の subscribe 確立時刻」ではなく「呼出側の初回 poll 時点」に取る点が肝要 — さもないと adapter 起動〜初回 poll の間に buffer へ溜まったイベントを初回 poll で誤ってリプレイし、Set D の「初回リプレイ無し」に反する。
- **型名正規化**: `pane.created`→`pane_started`、`pane.exited`/`pane.closed`→**`pane_exited`（dedup で exactly-once）**。Set D は「close ごと・crash ごとに `pane_exited` を exactly-once」を要求するため、Herdr が close で `pane.closed` と `pane.exited` を両方出す場合は同一 pane の重複を単一 lock 下で 1 回に畳む。未知の Herdr 型（`pane.moved` 等）は Set D 側で non-fatal に default-branch。
- **`events_dropped` 合成**: ring buffer overflow（購読側が遅れて buffer が溢れる）を検出し、**drop 件数付きの `events_dropped` を合成**。呼出側は `list_panes` で reconcile（Set D Q9 best-effort + reconcile の範囲内）。**Herdr subscribe 自体が backpressure でイベントを落とす可能性の有無は probe 4（event overflow）で確定**し、Herdr 側 drop が観測不能なら「adapter buffer overflow のみ検出可能・Herdr socket 側 drop は list_panes reconcile 頼み」という劣化を明示する。
- **接続断の回復**: subscribe コネクションが切れたら再購読し、再購読の空白期間を `events_dropped` 相当として扱い reconcile を促す。

### 4.6 error code マッピング（finding f）

Herdr raw error を**透過せず**、adapter 出口で Set D 語彙へ写像する（[§3.3](#33-surface-6error-codes-herdr) の表を実装判断として固定）。Codex 指摘 (f) の 4 点:

1. **`cwd_invalid` は layout mutation 前**: Herdr `pane.split` / `agent start` は cwd 検証コードを持たない（あるいは mutation 後に失敗しうる）。adapter は **spawn 発行前に cwd の存在・ディレクトリ性を検証**し、不正なら **layout を一切変えずに `cwd_invalid`** を返す（Set D 「no half-mutated state on cwd_invalid」）。
2. **identity collision は `name_in_use`**: 名前衝突は registry（[§4.2](#42-identity--registry-補完finding-b)）が spawn 発行前に検出し `name_in_use`。Herdr の `label` 経路は一意制約が無いため衝突検出には使わない。
3. **adapter 不通は `adapter_unavailable`**: Herdr socket 到達不能（socket ファイル消失 / `server.stop` 後 / プロセス落ち）は **`adapter_unavailable`**（broker は生存、端末バックエンドのみ不通）。Codex 指摘 (f) の「no_backend」概念に対応するが、既存 runtime 表記に合わせ `adapter_unavailable` を採用（[§3.3](#33-surface-6error-codes-herdr) の命名注記）。
4. **broker / MCP 不通は `backend_unreachable`**: broker MCP 自体への到達不能は `backend_unreachable`。**3 と 4 を別コードに分離**することで、dispatcher / secretary の error handling が「端末は落ちたが broker は生きている（= 別 backend へ切戻し可 / 再 spawn 可）」と「broker ごと不通（= 監視ループ停止・人間エスカレーション）」を区別できる。

表記正規化: `name_taken` 等の異表記は出口で **`name_in_use` に統一**。Herdr の operational refusal（`zoomed_tab` / `same_pane` / `cross_tab` / `single_pane` / `no_neighbor` 等）は**意味に応じた既知コードへ写像**する（例: pane 数・サイズ由来の拒否 → `split_refused` / `last_pane`、入力不正 → `invalid-params`）。**未知の Herdr raw コードは `internal` 系（バグ扱い）または `invalid-params` にのみ落とし、`adapter_unavailable` へは写像しない** — `adapter_unavailable` は **socket 到達不能を実際に確認したケースに限定**する（さもないと 3・4 の adapter 不通 vs broker 不通の分離が崩れ、dispatcher が誤ったフェイルオーバを起こしうる）。adapter は既知語彙のみを出し、呼出側は Set D 6.2「未知コード non-fatal」で default-branch する。

### 4.7 pane.report_agent の扱い（finding g）

Herdr `pane.report_agent`（state: working/blocked/idle/done/unknown）は agent の自己申告状態を Herdr に報告する **optional signal** である。**設計判断（Codex 指摘 (g)）**: これを **`pane_exited`（lifecycle）や `set_summary`（人間可読サマリ）の代替にしない**。

- `pane_exited` は **プロセス終了の lifecycle 事実**（[§4.5](#45-events-adapter-設計finding-e) の event 正規化）であり、agent の「done」自己申告とは別物。worker が `report_agent(done)` してもプロセスが生きていれば `pane_exited` は出ない。
- `set_summary` は broker registry の面（[§3.5](#35-surface-2messaging-herdr--非対応broker-継続)）であり、`report_agent` の `custom_status`（≤32 字）や `report_metadata` の `title`（≤80 字）とは字数・意味論が異なる。
- `report_agent` の値は **dispatcher の confidence-graded 観測の補助信号**として使える（worker 自己申告 = report_agent、独立観測 = `inspect_pane` grid scrape。Set D 1.7 が要求する独立観測を正本とし、report_agent は cross-check の一材料）。**harness correctness は report_agent に依存しない**。

---

## 5. メッセージング構成 — broker キュー継続

### 5.1 Herdr は Surface 1/3/6 のみ、Surface 2 は broker（finding a 詳細）

[§1.2](#12-なぜ第-3-バックエンドかadapter-境界の確定) の結論を配送レベルで固定する。`HerdrAdapter` が提供するのは **pane control（Surface 1）/ events（Surface 3）/ error codes（Surface 6）/ identity の観測部分（Surface 4）** のみ。**メッセージング（Surface 2: send_message / list_peers / check_messages / set_summary）は broker が継続**し、Herdr は一切のメッセージを運ばない。

- **送信者帰属**（`from_id` / `from_name` / `sent_at`、Set D 2.1 HYBRID）は broker が **per-agent token 由来で付与**する（[`renga-decoupling.md`](./renga-decoupling.md) §4.4）。Herdr の `agent.send` を使うと帰属が付かず、なりすまし不可性も失われるため使わない。
- **queue store** は broker の `.state/broker/` subtree（[`renga-decoupling.md`](./renga-decoupling.md) §4.5）。三状態 drain（`UNDELIVERED→CLAIMED→DELIVERED`、[`broker-native-roles.md`](./broker-native-roles.md) §9.3）も broker 内で完結し、Herdr backend でも不変。
- **`list_peers`** は broker の bind 表ベース（registry、[§4.2](#42-identity--registry-補完finding-b)）。Herdr の `agent.list`（agent 検出状態の列挙）は peer channel ではないため使わない。

これにより、backend を renga / WezTerm / tmux / Herdr のいずれに差し替えても**メッセージング経路は不変**（backend 非依存、[`renga-decoupling.md`](./renga-decoupling.md) §4.7.2 の messaging tier 結論と一致）。

### 5.2 spawn 時の 2 系統注入（finding d）

受信の正準路は **push 一次**（`claude/channel` channel sidecar が in-band 注入、[`broker-native-roles.md`](./broker-native-roles.md) §9）であり、pull はフォールバック。この前提は Herdr backend でも維持する。**spawn 時に 2 系統を注入する**（2026-06-15 ratified 追補、Codex 指摘 (d)）:

1. **`--mcp-config`**: broker MCP（daemon）への接続設定。broker のツール（messaging / poll_events / registry）を role-scoped で公開。
2. **dev-channel sidecar**: `--dangerously-load-development-channels server:org-broker-channel` により **tool-less `claude/channel` sidecar**（[`spike/channel_sidecar.py`](../../spike/channel_sidecar.py)）を load。idle セッションを push で起こす。

**Herdr での注入経路**: Herdr の「integration」自動検出は argv 注入をしないため、**汎用起動経路で broker が argv を合成する**:

- `herdr agent start <name> --cwd <wd> --env K1_DAEMON_URL=... --env K1_DELIVERY_CRED=... --env K1_OWNER=... -- claude --mcp-config <path> --dangerously-load-development-channels server:org-broker-channel <他 flag>`
- または `herdr pane split --cwd <wd> --env ...` + `herdr pane run <id> "claude --mcp-config ... --dangerously-load-development-channels ..."`。

channel sidecar は env（`K1_DAEMON_URL` / `K1_DELIVERY_CRED` / `K1_OWNER`）で daemon と delivery-scoped credential を受け取る（[`spike/channel_sidecar.py`](../../spike/channel_sidecar.py)、[`broker-native-roles.md`](./broker-native-roles.md) §9.4）。**Herdr 管理 env（`HERDR_*`）は衝突時に優先されるが、`K1_*` / `--mcp-config` は Herdr 管理変数と名前空間が衝突しないため上書きされない**。**両注入が Herdr の起動経路を通って実際に効くか（argv が欠けない・env が届く・dev-channel 承認プロンプトが `send_keys(enter)` で承認できる）は probe 5（sidecar spawn argv）で確定する**。

**dev-channel 承認**: Claude 側の `Load development channel? (Y/n)` プロンプトは Claude Code の機能であり、orchestrator が `send_keys(enter=true)` で機械承認する（Set D 5.1）。これは [§4.1](#41-send_keys-特殊キー適合性finding-c) の Enter 対応（構文○）で満たされ、その TUI 到達の実測は probe 5（承認プロンプト機械承認可否）が担う。

**Herdr agent 検出との共存**: Herdr は起動した Claude を自動検出し、自前の hook を Claude 設定へ書き込んで `report_agent` 状態を得ようとする。これは **messaging とは直交**（Herdr hook = 状態レポート、our 注入 = メッセージ輸送）であり、`report_agent` は optional signal（[§4.7](#47-panereport_agent-の扱いfinding-g)）として扱うため共存に問題はない。ただし Herdr hook が Claude の MCP 設定を書き換えて `--mcp-config` と干渉しないかは probe 5 の確認対象に含める。

---

## 6. capability probe 対応表（finding i）

導入計画（[§7](#7-段階的導入計画)）の**先頭に capability probe フェーズを置く**（Codex 指摘 (i)）。実測は**並走中の herdr-socket-spike ワーカー**が担うため、本設計書は probe 項目と本書の設計判断の対応関係を示すにとどめ、**probe 結果で各ギャップの「確定 / non-conformance」を後決めする**。

| # | probe 項目 | 何を実測するか | 確定する本書の設計判断 | 満たせない場合の degrade |
|---|---|---|---|---|
| 1 | **send_keys 特殊キー** | arrows/home/end/pageup/pagedown/backspace/delete/tab/space の可否、**Shift+Tab の permission-mode トグル到達**、Ctrl+C の SIGINT 到達、Esc の modal escape 到達（Enter の到達は probe 5 が担う） | [§4.1](#41-send_keys-特殊キー適合性finding-c) の適合性マトリクスの △ セル | 欠落キーを使う harness フローを non-conformance 明記。load-bearing 4 キー（Enter=probe 5 / Ctrl+C・Esc・Shift+Tab=probe 1）欠落は Phase 保留 |
| 2 | **cwd 取得** | `pane.get.foreground_cwd` / `pane.process_info.cwd` の取得可否・正確性・遅延 | [§4.2](#42-identity--registry-補完finding-b) の registry 補完（cwd 観測）、`list_panes` の cwd 充填 | cwd 観測不能なら registry の spawn-time cwd のみを正本にし観測値欠落を明示 |
| 3 | **geometry 精度** | `pane.layout` の rect が cell 単位か fractional ratio か、換算誤差の balanced-split 影響 | [§4.3](#43-geometry-の-cell-単位換算finding-i-probe-3) の cell 換算方針 | 誤差過大なら choose_split 戦略調整 or non-conformance |
| 4 | **event overflow / events 能力** | `events.subscribe` が backpressure でイベントを落とすか、drop を adapter が検出可能か、**加えて再開カーソル・timeout cap の有無**（§2 の否定能力主張の裏取り） | [§4.5](#45-events-adapter-設計finding-e) の `events_dropped` 合成・buffer overflow 検出・cursor/timeout 正規化の要否 | Herdr 側 drop 観測不能なら list_panes reconcile 頼みを明示。もし Herdr が cursor/timeout をネイティブ提供していれば §4.5 の一部正規化は不要になる |
| 5 | **sidecar spawn argv** | `--mcp-config` + dev-channel 両注入が Herdr 起動経路を通って効くか、env（`K1_*`）が届くか、承認プロンプト機械承認可否、Herdr hook との干渉 | [§5.2](#52-spawn-時の-2-系統注入finding-d) の 2 系統注入経路 | 汎用経路（`pane split + pane run`）へ切替、または Herdr hook 無効化を前提化 |

---

## 7. 段階的導入計画

renga-decoupling の Phase 体系（Phase 3 = messaging / Phase 4 = full backend、[`renga-decoupling.md`](./renga-decoupling.md) §7）に対応させ、**Herdr 固有の Phase を H0–H2 として重ねる**。いずれもフォークで実証してから本体（claude-org-runtime）へ取り込む。既存の renga / WezTerm / tmux backend には触れない。

### 7.0 Phase H0: capability probe（先頭・必須）

- **内容**: [§6](#6-capability-probe-対応表finding-i) の 5 項目を herdr-socket-spike ワーカーが実測する。
- **完了判定**: 5 項目の実測結果が出て、本書の △ セル（[§3](#3-マッピング表) / [§4](#4-ギャップ分析と-adapter-設計判断)）が「確定 / non-conformance」に後決めされること。
- **保留基準**: **load-bearing 4 キー（Enter / Ctrl+C / Esc / Shift+Tab）のいずれかが TUI に到達しない**場合、Herdr backend の H1 以降を保留し、窓口経由で仕様縮小（当該フローの代替 or Herdr 非対応の明示）を判断する。実測の割当は **Enter = probe 5（承認プロンプト機械承認可否）/ Ctrl+C・Esc・Shift+Tab = probe 1** であり、**probe 1 と probe 5 のどちらの not-reach もこの保留ゲートを駆動する**（Enter は probe 1 の対象外だが probe 5 の失敗で同じく保留する）。

### 7.1 Phase H1: messaging tier（`HerdrAdapter` 最小面）

- **内容**: `HerdrAdapter` を `TerminalAdapter` の第 3 実装として追加し、**messaging tier が要求する最小面**（[`renga-decoupling.md`](./renga-decoupling.md) §4.7.2 (a)）= send-text 相当（Protocol の `type_text` / `send_line`、Herdr `pane.send_text` へ配線。ナッジ / 定型注入）+ grid scrape（`get_text` → `classify_pane_state`、Herdr `pane.read`）+ pane 識別（registry）+ 起動チェーン（[§5.2](#52-spawn-時の-2-系統注入finding-d) の 2 系統注入）を Herdr 面へ配線する。**メッセージング自体は broker 継続**（Herdr は運ばない、[§5](#5-メッセージング構成--broker-キュー継続)）。
- **完了判定**: broker + `HerdrAdapter` で、AC-1 相当（send-text の 4 状態非破壊）+ AC-2 相当（`--mcp-config` + dev-channel 注入 → broker 接続 → token 帰属 → `check_messages` 一往復 → registry ベース `list_peers` 登録検知）が green。既存 WezTerm / tmux の backend パラメータ切替に Herdr を 1 列追加して同一 AC が通ること。

### 7.2 Phase H2: full backend tier（ペイン操作全面）

- **内容**: spawn（split + cwd 前検証 + registry name/role）/ close（`pane_exited` exactly-once 正規化）/ `list_panes`（cell geometry 換算）/ `inspect_pane`（`pane.read`、format/lines、cursor はギャップ明示）/ `send_keys`（probe 1 で確定した語彙）/ `poll_events`（[§4.5](#45-events-adapter-設計finding-e) の cursor/buffer 正規化）を Herdr 面へ配線。
- **完了判定**（[`renga-decoupling.md`](./renga-decoupling.md) §7.4 の Herdr 版）:
  - delegate → spawn → 監視（stall 検出 / 承認待ち観測 = `inspect_pane` grid scrape）→ 完了報告 → CLOSE_PANE → retro の 1 サイクルが Herdr backend で完走。
  - `poll_events` 正規化の実効遅延が dispatcher 監視ループ（3 分 cadence）の正しさを損なわない（`pane_exited` 取りこぼしが `list_panes` reconcile で回復。Herdr はネイティブイベントがあるぶん tmux 合成より有利）。
  - balanced split が Herdr geometry（cell 換算、probe 3 確定）で現行同等（`choose_split` 再利用）。
  - error code 分離（`cwd_invalid` 前検証 / `name_in_use` / `adapter_unavailable` vs `backend_unreachable`）が dispatcher error handling で正しく分岐。
- **保留基準**: probe 3（geometry 誤差過大）/ probe 4（overflow 検出不能で監視信頼性劣化）が許容外なら、当該面を non-conformance 明記のうえ H2 スコープを縮小し窓口判断を仰ぐ。

### 7.3 本体取り込み（別スコープ）

prose 書き換え・Set D 契約改訂（Herdr を Surface 1/3/6 の準拠 backend として明記する amendment）・runtime 実装は **claude-org-runtime 側の取り込みスコープ**であり、本フォーク（ja 不可触制約）では実施しない。Herdr 列を [`renga-decoupling.md`](./renga-decoupling.md) §4.7.1 の能力表へ追加する改訂も取り込み時に行う。

---

## 8. 残存リスク / スコープ外

- **probe 依存の未確定**: [§6](#6-capability-probe-対応表finding-i) の 5 項目は herdr-socket-spike の実測待ち。本書の △ セルは probe 前に断定していない。probe 結果が想定と乖離した場合、[§4](#4-ギャップ分析と-adapter-設計判断) の該当判断を改訂する。
- **cursor 欠落**（[§4.4](#44-inspect_pane-の-format--cursorfinding-h)）: `include_cursor` は Herdr で best-effort 欠落。現行 harness の状態判定・承認観測は grid テキストで足りるため correctness は保つが、将来 cursor 必須のフローが増えると劣化する。
- **Herdr socket 認証**: Herdr socket は認証モデルを明示せず「ローカルアクセス = 権限」。broker の per-agent token 認証（Surface 2）とは層が別で、端末バックエンド層の到達制御はソケットファイル権限に依存する。broker MCP の HTTP 公開（localhost only + token、[`non-goals.md`](../non-goals.md) §12）とは独立。
- **スコープ外**: Herdr の worktree / workspace / plugin 機能の活用、Herdr integration（自動検出 hook）を messaging に転用する案（[§1.2](#12-なぜ第-3-バックエンドかadapter-境界の確定) で棄却）、multi-tab addressing（Set D 4.2 single-tab MUST を維持）、renga / WezTerm / tmux の挙動変更。

---

## 付録 A: Codex design review 指摘 (a)–(i) の織り込み対応表

| 指摘 | 要旨 | 本書の織り込み先 |
|---|---|---|
| (a) | adapter 境界は `claude_org_runtime.terminal` 層。Herdr は Surface 2 を持たないため broker adapter 層に混ぜない | [§1.2](#12-なぜ第-3-バックエンドかadapter-境界の確定) / [§5.1](#51-herdr-は-surface-136-のみsurface-2-は-brokerfinding-a-詳細) |
| (b) | set_pane_identity / cwd / role / kind / receive_mode を Herdr に押し込まず spawn-time registry で補完。name 衝突・全桁数字禁止も契約 | [§4.2](#42-identity--registry-補完finding-b) / [§3.1](#31-surface-1pane-control-herdr--terminaladapter) 1.8 行 |
| (c) | send_keys の raw key vocabulary 適合可否を明記、満たせない場合は capability gap / non-conformance | [§4.1](#41-send_keys-特殊キー適合性finding-c) / [§3.1](#31-surface-1pane-control-herdr--terminaladapter) 1.9 行 |
| (d) | channel sidecar との共存を push 一次前提で。spawn 時 `--mcp-config` + dev-channel 両注入 | [§5.2](#52-spawn-時の-2-系統注入finding-d) |
| (e) | Surface 3 events の cursor / 初回 now / 30s cap / filter 時カーソル前進 / events_dropped + reconcile を adapter の event buffer + cursor 正規化で | [§4.5](#45-events-adapter-設計finding-e) / [§3.2](#32-surface-3events-herdr) |
| (f) | error code を透過せず分離: cwd_invalid（mutation 前）/ identity collision=name_in_use / adapter 不通=（no_backend→）adapter_unavailable / broker 不通=backend_unreachable。name_taken↔name_in_use 正規化 | [§4.6](#46-error-code-マッピングfinding-f) / [§3.3](#33-surface-6error-codes-herdr) |
| (g) | pane.report_agent は optional signal。pane_exited / set_summary の代替にしない | [§4.7](#47-panereport_agent-の扱いfinding-g) |
| (h) | pane.read を inspect_pane 相当とする際の format=grid / include_cursor の扱い | [§4.4](#44-inspect_pane-の-format--cursorfinding-h) |
| (i) | 導入計画の先頭に capability probe（5 点）。herdr-socket-spike の実測に対応づけ | [§6](#6-capability-probe-対応表finding-i) / [§7.0](#70-phase-h0-capability-probe先頭必須) |

---

## 改訂履歴

- 2026-07-03: 初版（design only）。Herdr を claude-org 第 3 端末バックエンドとして対応させる設計。Herdr Socket API と Set D Surface 1/3/6 + `TerminalAdapter` Protocol のマッピング表、ギャップ分析（identity registry 補完 / send_keys 適合 / geometry cell 換算 / inspect_pane cursor / events cursor 正規化 / error code 分離 / report_agent optional）、messaging=broker 継続の構成、capability probe を先頭に置く段階的導入計画（H0–H2）を固定。事前 Codex design review（gpt-5.5）指摘 (a)–(i) を全反映（付録 A 対応表）。実測依存項目は並走 herdr-socket-spike の probe 結果で後決めとし断定しない（Refs #27）。
