# renga 依存解消（案 B）— org-broker / terminal adapter 設計

> ステータス: **design only / 実装なし**。本リポジトリにこの設計の実装は一切存在しない。実験はフォークで行い、broker / adapter の実体は claude-org-runtime 側に置く計画である。
> 本ドキュメントは「未実装の将来設計」であり、以下の記述はすべて**提案・計画**である。現行動作（renga 経由）との対比は [§2「現状とこの設計の関係」](#2-現状とこの設計の関係) を参照。
> 一次入力: ユーザー・窓口間の設計合意ノート（2026-06-07、リポジトリ未コミットの運用ノート `notes/renga-decoupling-design-input-2026-06-07.md`）および Codex design review（同日、`tmp/` 配下の未コミットノート）。いずれも git 管理外のためこのブランチからは参照できないが、**そこで確定した制約・合意事項は [§1](#1-背景と確定制約本設計が覆さない前提) に本文として転記済み**であり、本設計書は単体で読める。本設計はこれらの確定制約を覆さない。
> 依存ドキュメント（参照は本設計書 → 既存文書の一方向のみ。既存文書側から本設計書への参照追加は行わない）:
> - [`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md)（Contract Set D、2026-05-03 批准。本設計の土台）
> - [`docs/contracts/state-semantics-contract.md`](../contracts/state-semantics-contract.md)（Set F。state.db SoT の正準）
> - [`docs/contracts/state-schema-contract.md`](../contracts/state-schema-contract.md)（Set C。`.state/` ファイル台帳）
> - [`docs/non-goals.md`](../non-goals.md)（特に §6 PTY 層、§12 HTTP 外部公開）
> - [`docs/design/core-harness-extraction.md`](./core-harness-extraction.md)（design only ヘッダと layer 整理の先例）

---

## 1. 背景と確定制約（本設計が覆さない前提）

以下 3 点はユーザー・窓口間で確定済みの制約であり、本設計はこの枠内で組み立てる。

1. **課金制約 — ヘッドレス化は不成立**: 2026-06-15 から `claude -p` / Agent SDK の使用は対話利用と分離された「Agent SDK 月間クレジット」（Max 20x で $200/月）に計上され、超過分は API 従量課金になる（出典: code.claude.com/docs/en/headless、support.claude.com 記事 15036540）。本組織のワーカー使用量では確実に超過するため、**全エージェントは対話型 TUI セッションのまま**とする。エージェントを headless 化して renga 依存を消す案はこの時点で棄却済み。
2. **IME 制約 — WezTerm 素のままは不成立**: 単一ペインでも Claude Code のスピナー描画（「✻ Cogitated...」等)が IME 変換窓のアンカーを奪う（ユーザー実測）。renga は hardware-cursor caret 制御でこの問題を解決している。よって**人間が日本語入力する端末（窓口ペイン）は renga を継続使用**する。「renga を排除して WezTerm 素に戻す」案もこの時点で棄却済み。
3. **採用方針 = 案 B**: 組織の輸送層（メッセージング・spawn・観測）**だけ**を org-broker + terminal adapter で renga 非依存化し、renga は「組織が要求する必須前提」から「ユーザーの端末選択」に降格する計画。renga 故障時の縮退運転先が WezTerm バックエンド。**renga の排除が目的ではない**。

進め方も合意済み: 配線替え（`mcp__renga-peers__*` → broker ツール）の薄い差分は**フォークで実験**し、成功したらフェーズ単位（メッセージング → ペイン操作）で本体に取り込む。broker デーモン + terminal adapter の実体コードは claude-org-runtime（既存の別パッケージ）または新規リポジトリに置き、本リポジトリには持ち込まない（[`docs/non-goals.md`](../non-goals.md) §6「PTY や端末多重化器の層を持たない」と整合）。dispatcher の決定的処理の Python 化は**本設計のスコープ外**（将来課題としてのみ記載、[§9](#9-スコープ外将来課題)）。

## 2. 現状とこの設計の関係

**現行動作（実装済み・運用中）**: 本リポジトリの組織運用は renga-peers MCP サーバー（renga 0.18.0+、14 ツール）を唯一の輸送層として動作している。エージェント間メッセージングは renga のチャネル注入（`<channel source="renga-peers">` の in-band 配達）、ペイン操作・観測は `spawn_claude_pane` / `list_panes` / `inspect_pane` / `send_keys` / `poll_events` / `close_pane` 等で行われる。この現行面は [`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md)（Set D）が抽象バックエンド契約として批准済みである。

**この設計（未実装の提案）**: 輸送層を org-broker デーモン + terminal adapter に置き換える計画。本リポジトリに broker / adapter の実装は存在せず、`.claude/skills/` / `.dispatcher/` / `tools/` の現行 prose・コードは引き続き renga-peers を呼ぶ。フォークでの実験が成功し、フェーズ単位の取り込み判断（[§7](#7-phase-計画と移行完了判定基準)）を通過するまで、本体の挙動は一切変わらない。

| 観点 | 現行（renga 経由、実装済み） | 提案（broker 経由、未実装） |
|---|---|---|
| メッセージ配達 | renga サーバーがチャネル注入（Claude には in-band push、Codex にはナッジ + pull） | broker queue store に蓄積し、**全エージェント pull 化**（ナッジ 1 行注入 + `check_messages`）する計画 |
| 送信者帰属 | renga サーバーが pane 由来で `from_id` / `from_name` を付与 | broker が **per-agent token** から付与する計画（自己申告にしない点は同じ） |
| ペイン操作 | 全ロールが同一 MCP サーバーのツール群にアクセス可能（許可スキーマで絞る） | worker / curator にはメッセージング面のみ公開し、ペイン操作は broker 内部 + dispatcher / secretary 向け最小公開とする計画（[§4.2](#42-broker-mcp-surface役割別公開面)） |
| エージェント接続 | spawn 時に `--dangerously-load-development-channels server:renga-peers` 注入 + 承認プロンプト | spawn 時に `--mcp-config` で broker MCP（localhost HTTP）を注入する計画（[§4.6](#46-起動フローの置き換え)） |
| 端末バックエンド | renga 必須 | renga / WezTerm を adapter で差し替え可能にする計画（人間入力端末は renga 継続が既定） |
| 人間の日本語入力 | renga の hardware-cursor caret 制御 | 変更なし（renga 継続。broker はこの層に関与しない） |

## 3. `mcp__renga-peers__*` 呼出箇所の棚卸し

Phase 2（棚卸し・契約整合）の先行実施として、リポジトリ内の全参照を 3 分類で固定する（2026-06-07 時点、`grep -rE "mcp__renga-peers__"` による全数調査）。**配線替えの対象は (a) のみ**であり、(b) は許可スキーマの再宣言、(c) は文書更新で追随する。

### 3.1 分類 (a): 運用上の呼出記述（配線替え対象）

ロール prose / スキルに書かれた、実行時に実際に MCP 呼出として発火する記述。呼出主体（ロール）×ツールのマトリクス:

| ツール | 窓口 (secretary) | ディスパッチャー | キュレーター | ワーカー |
|---|---|---|---|---|
| `send_message` | ● ack / 指示・転送・suspend 通知（`CLAUDE.md`、org-delegate / org-escalation / org-pull-request / org-suspend / org-retro / skill-audit / dispatcher-handover 起点） | ● escalate / DELEGATE_COMPLETE / nudge / retro gate（`.dispatcher/CLAUDE.md`、spawn-flow / worker-monitoring / pane-close） | ● CURATE_DONE 等の報告（`.curator/CLAUDE.md`、org-curate） | ● 完了・進捗・判断仰ぎ報告（worker brief テンプレート群） |
| `check_messages` | ● CI_COMPLETED 受領（org-pull-request）、suspend / resume 時 drain | ● 監視ループの worker 自己報告受信 | — | —（受信は renga の in-band push） |
| `list_peers` | ● 起動・resume 時の peer 確認 | ● worker 登録待ち（spawn-flow 3-4） | — | ●（窓口の自動発見。brief 記載） |
| `list_panes` | ● 起動・suspend・attention 系 | ● balanced split 入力 / 監視 reconcile | — | — |
| `inspect_pane` | ● dispatcher prompt poll（handover 経路）、org-delegate Step 5 介入 | ● 承認待ち / stall 観測（worker-monitoring） | — | — |
| `send_keys` | ● dispatcher `/clear` → `/dispatcher-resume` 打鍵、dev-channel 承認、Esc 介入 | ● dev-channel 承認（spawn-flow 3-3b）、Shift+Tab / Ctrl+C 介入 | — | — |
| `poll_events` | ● org-suspend の pane_exited 確認 | ● pane_started / pane_exited 監視（cursor 永続化: `.state/dispatcher-event-cursor.txt`） | — | — |
| `spawn_claude_pane` | ● dispatcher / curator 起動（org-start）、再派遣 | ● worker spawn（spawn-flow 3-2）、オンデマンド curator spawn | — | — |
| `spawn_pane` | ● attention watcher 起動（org-attention-start） | — | — | — |
| `close_pane` | ● org-suspend / org-attention-stop | ● CLOSE_PANE 処理（pane-close）、curator 退役 | — | — |
| `set_pane_identity` / `set_summary` | ● org-start Step 0.3 自己修復、secretary-resume | ● dispatcher-resume | — | — |
| `focus_pane` / `new_tab` | （人間向け補助。運用 prose 上の必須呼出なし） | — | — | — |

主な所在ファイル（呼出記述を含む運用文書）: `CLAUDE.md`、`.claude/skills/{org-start,org-delegate,org-escalation,org-pull-request,org-suspend,org-retro,org-curate,org-attention-start,org-attention-stop,secretary-resume,dispatcher-handover,dispatcher-resume,skill-audit}/SKILL.md`、`.claude/skills/org-delegate/references/{ack-template,instruction-template,pane-layout,renga-error-codes,worker-claude-template,claude-org-self-edit}.md`、`.dispatcher/CLAUDE.md`、`.dispatcher/references/{spawn-flow,worker-monitoring,pane-close}.md`、`.curator/CLAUDE.md`、`tools/templates/worker_brief_{normal,self_edit}.md`。

ここから読み取れる配線替えの規模感:

- **ワーカー / キュレーターの必要面は最小**: `send_message`（+ 窓口発見の `list_peers`、受信の `check_messages` 相当）のみ。ペイン操作は一切呼ばない。→ Phase 3（メッセージング移行）だけでワーカー / キュレーターは renga ツール非依存にできる見込み。
- **ペイン操作の呼出主体は dispatcher と secretary に集中**: spawn / close / inspect / send_keys / poll_events は両ロールに限られる。→ Phase 4（ペイン操作移行）の影響範囲はこの 2 ロールの prose に閉じる。

### 3.2 分類 (b): 許可スキーマ / 設定宣言（呼出箇所ではない）

ツール名が allowlist エントリとして列挙されているだけのもの。配線替え時は broker ツール名での再宣言が必要:

- `.claude/settings.json`（14 ツールを allow 宣言）
- `tools/org_extension_schema.json`（ロール別 allow 宣言）
- `.claude/skills/org-setup/references/permissions.md`（スキーマの文書化）

### 3.3 分類 (c): ドキュメント / コメント / fixture 参照（挙動に関与しない）

- `docs/` 配下の契約・設計・運用文書（`docs/contracts/backend-interface-contract.md` ほか、`docs/getting-started.md`、`docs/verification.md`、`docs/operations/`、`docs/legacy/`、`docs/internal/` 等）
- Python ツール内の docstring / コメント参照: `tools/dispatcher_retro_gate.py`、`tools/gen_delegate_payload.py`、`tools/peer_notify.py`（いずれも **MCP を呼ぶコードではない**。Python プロセスからは MCP ツールに到達できないため、Claude セッション向けの指示文言を生成・説明しているのみ）
- テスト fixture: `tools/test_org_setup_prune.py`（allowlist 文字列として 1 件）

> 補足: リポジトリルートの `send_plan.json`（未コミットの運用生成物）にも参照が含まれるが、git 管理外のため棚卸し対象から除外する。

## 4. 提案アーキテクチャ: org-broker + terminal adapter

### 4.1 全体像

```
                   （人間）
                      │ 日本語入力は renga ペインで継続（IME 制約）
   ┌──────────────────┴───────────────────────────────┐
   │ 端末バックエンド（renga ／ 縮退時 WezTerm。adapter で差し替え）│
   │  ┌────────┐ ┌──────────┐ ┌────────┐ ┌────────┐  │
   │  │secretary│ │dispatcher │ │curator │ │worker-*│  │
   │  └───┬────┘ └────┬─────┘ └───┬────┘ └───┬────┘  │
   └──────┼───────────┼───────────┼──────────┼───────┘
          │ MCP(HTTP, localhost only, per-agent token)
          ▼           ▼           ▼          ▼
   ┌─────────────────────────────────────────────────┐
   │ org-broker デーモン（claude-org-runtime 側に実装する計画）│
   │  - broker queue store（.state/broker/ 専用 subtree）   │
   │  - token 発行・帰属付与・role-scoped ツール公開          │
   │  - ナッジ配達（terminal adapter 経由の 1 行打鍵）        │
   │  ┌─ terminal adapter（差し替え可能）─────────────┐ │
   │  │ renga adapter ／ WezTerm adapter ／ (将来 tmux) │ │
   │  └───────────────────────────────────────────┘ │
   └─────────────────────────────────────────────────┘
```

- 各エージェントは spawn 時に `--mcp-config` で broker の MCP サーバー（localhost HTTP）を注入され、per-agent token で認証される計画。
- 送信者帰属（`from`）は broker が token から付与し、自己申告にしない（renga のサーバー帰属モデルの再現 = 偽装防止）。
- ペイン操作（spawn / send-text / close / 画面取得 / イベント）は broker が adapter 経由で実行する。adapter は renga / WezTerm を差し替え可能にする。

### 4.2 broker MCP surface（役割別公開面）

現行 renga-peers が全ロールに同一ツール群を見せ、許可スキーマ（分類 (b)）で絞っているのに対し、broker は **token の role scope でツール公開自体を変える**計画。インジェクションを踏んだワーカーが窓口ペインへ直接打鍵する経路（`send_keys`）を、許可設定ではなく**構造的に**断つことが狙い。

| ツール（提案名） | worker / curator | dispatcher | secretary | broker 内部のみ |
|---|---|---|---|---|
| `send_message` | ○ | ○ | ○ | |
| `check_messages` | ○ | ○ | ○ | |
| `list_peers` | ○ | ○ | ○ | |
| `set_summary` | ○ | ○ | ○ | |
| `list_panes`（geometry 付き） | — | ○ | ○ | |
| `inspect_pane`（grid scrape） | — | ○ | ○ | |
| `send_keys`（raw PTY） | — | ○ | ○ | |
| `poll_events`（cursor 付き long-poll） | — | ○ | ○ | |
| `close_pane` | — | ○ | ○ | |
| `spawn_agent`（= 現行 `spawn_claude_pane` 相当） | — | ○ | ○ | |
| `spawn_pane`（generic） | — | — | ○（attention watcher 用） | |
| `set_pane_identity` | — | ○ | ○ | |
| ナッジ注入（配達の内部機構） | — | — | — | ●（ツールとして公開しない） |

- **M1: dispatcher 向け最小 surface** は、現行契約で correctness 必須とされる面（Set D で REQUIRED の `list_panes`(geometry) / `inspect_pane` / `send_keys`、および監視ループが依存する `poll_events` / `close_pane`）+ spawn 系 + メッセージング系、で固定する。上表 dispatcher 列がその列挙である。
- **重要な整理**: 「ペイン操作を排除する」のではない。ナッジ配達自体が send-text（raw 打鍵）を内部機構として必要とするため、**broker がペイン操作の信頼された保持者になり、worker / curator からのみ到達不能にする**、という境界の張り替えである。dispatcher / secretary は現行どおりペイン操作を持つ（持たなければ監視・介入・suspend が成立しない）。
- secretary の公開面は dispatcher とほぼ同一とする（org-start の dispatcher 起動、attention watcher の spawn/close、handover 経路の `send_keys` + `inspect_pane`、org-suspend の close/poll が現行運用で必要なため。[§3.1](#31-分類-a-運用上の呼出記述配線替え対象) の棚卸しに対応）。
- renga の `focus_pane` / `new_tab` は人間向け補助であり（Set D でも非必須）、broker MCP の初期 surface からは**外す**提案とする。必要になった時点で追加する。

### 4.3 窓口への割り込み配達（ナッジ。最難関・足切り対象）

MCP は要求応答型であり、対話中の Claude セッションへ push できない。renga のチャネル注入（in-band push）の代替として、以下の 2 段構えを提案する:

1. **ナッジ**: broker が terminal adapter（WezTerm なら `wezterm cli send-text`）で宛先ペインに定型 1 行「📨 新着あり。check_messages を実行」+ Enter を打鍵する。
2. **本文取得**: 受信側は broker の `check_messages` で本文を取得する。**本文は PTY を通らない**（長文・制御文字・マルチバイトの混線リスクをナッジ定型 1 行に閉じ込める）。

設計上の緩和策（Phase 1 で実証する）:

- **注入前の入力欄静止確認**: broker はナッジ打鍵の前に grid scrape で宛先ペインの入力欄が空（プロンプト静止）であることを確認し、空でなければ defer + 再試行する。これは現行の dispatcher handover 経路（`/clear` 後にプロンプト空を 1 秒間隔 poll してから次を打鍵する手順、`CLAUDE.md` 記載）と同型の既知テクニックである。
- **ナッジの冪等性**: 配達は「未読あり」の通知であって本文ではないため、重複注入してもキュー消費は `check_messages` 側で一度きり。取りこぼし時は再ナッジで回復する。

> **足切り条項**: この機構が Phase 1 スパイクの合格条件（[§7.1](#71-phase-1-スパイクwezterm--windows中止判断点)）を満たさない場合、**計画ごと棚上げする**。ナッジ配達は本設計全体の成立条件である。

### 4.4 per-agent token のライフサイクル

送信者帰属と role-scoped 公開面の根拠となる token の一生を以下のとおり提案する:

| 局面 | 提案する挙動 |
|---|---|
| **発行** | broker が spawn 要求を受けた時点で生成し、spawn 時に環境変数で個別発行する（一次入力の合意事項）。`--mcp-config` で渡す接続設定はこの env を参照する。token は `{agent_id, role, pane_id, session_id}` に bind される |
| **bind** | token ↔ pane/session の対応表は broker のみが保持する。`from` 帰属・role scope 判定・宛先解決はすべてこの bind 表から導出し、クライアント自己申告を採らない |
| **revoke（pane 退役）** | adapter の `pane_exited` イベント受領時、および broker 経由の `close_pane` 成功時に即時 revoke する。退役済み pane の token による呼出は `token_revoked` エラー（[§5 Surface 6](#surface-6-エラーコード語彙--継承--新設)）で拒否する。子プロセス等に env が漏洩していても、pane 退役後は使えない |
| **TTL** | 発行時に TTL を付す（既定値は Phase 1 で実測の上決定。長時間セッション運用のため「セッション寿命より長い TTL + 退役時 revoke」を基本とし、TTL は失効漏れの保険と位置付ける） |
| **suspend / resume** | `/org-suspend` 相当で全 token を revoke し、resume 時の再 spawn で**再発行**する。suspend をまたいだ token 再利用は不可とする（resume 時の pane id 変動と bind 表の整合を保つため） |
| **格納と漏洩面** | token はホスト内の env / broker bind 表のみに存在し、queue store・ログ・journal には平文で書かない。env 経由の子プロセス漏洩は revoke-on-exit + TTL + localhost bind で被害面を限定する（さらに絞る場合は per-agent の mcp-config 一時ファイル（0600）経由の受け渡しを Phase 1 で比較検討する） |

### 4.5 broker queue store（`.state/broker/` 専用 subtree）

broker の書き込み領域は **`.state/broker/` 専用 subtree に限定**し、この領域を「**broker queue store**」と命名する（「メッセージストア」という呼称は使わない — state.db との混同を避けるため）。

- broker queue store は **state.db ではなく、events テーブルでもない**。[`docs/contracts/state-semantics-contract.md`](../contracts/state-semantics-contract.md)（Set F）が定める state.db SoT（runs / org_sessions / events / worker_dirs）および [`docs/contracts/state-schema-contract.md`](../contracts/state-schema-contract.md)（Set C）のファイル台帳とは**衝突させない**。broker は state.db に一切書かない。
- broker queue store の唯一の書き手は broker デーモンである。逆に、既存の state writer（StateWriter / journal_append 系）は `.state/broker/` に書かない。所有権は「subtree 単位で一人の書き手」で対称に切る。
- 中身（提案）: 配達待ちキュー、配達済みカーソル、token bind 表（または bind は in-memory + 再起動時再構築）、ナッジ配達の試行ログ。形式（SQLite 別ファイル `queue.db` か JSONL か）は実装時に決定する。
- 監査イベント（例: 配達失敗のエスカレーション）を組織の journal に残したい場合は、broker が直接書くのではなく、既存の sanctioned writer（`tools/journal_append.*`）を呼ぶ**運用側**が記録する。broker の責務は輸送に限定する。
- **Set C 改訂が必要**: [`docs/contracts/state-schema-contract.md`](../contracts/state-schema-contract.md)（Set C）は `.state/` 配下の永続ファイル群全体を契約対象としているため、「衝突させない」だけでは足りず、**`.state/broker/` subtree の新設自体が Set C の state files inventory への追加改訂（path / format / owner=broker / readers / migration）にあたる**。Phase 3 取り込み時の契約改訂 PR に Set C 改訂を含める（[§7.3](#73-phase-3-メッセージング移行messaging-adapter)）。本設計書はその改訂提案であり、Set C 本文は変更しない。

### 4.6 起動フローの置き換え

現行の正道（`.dispatcher/references/spawn-flow.md` Step 3-2〜3-5）と提案の対比:

| 段階 | 現行（renga、実装済み） | 提案（broker、未実装） |
|---|---|---|
| 1. spawn | `spawn_claude_pane(...)` — renga が `--dangerously-load-development-channels server:renga-peers` を合成 | dispatcher が broker の `spawn_agent(...)` を呼ぶ → broker が token を発行し、adapter 経由で pane を spawn。Claude 起動 args に `--mcp-config <broker接続設定>`（+ 必要なら `--strict-mcp-config`）を合成する計画 |
| 2. 起動確認 | `poll_events` で `pane_started` を最大 3 秒待つ | 同等（broker の `poll_events` が adapter のイベントを正規化して返す） |
| 3. チャネル承認 | `send_keys(enter=true)` で「Load development channel?」プロンプトを承認 | **dev-channel prompt は存在しない**（dev-channel flag を使わないため）。`--mcp-config` 注入されたサーバーに対する Claude Code 側の信頼確認プロンプトの有無は **Phase 1 の実測確認項目**とする。プロンプトが出る場合は現行同様 `send_keys(enter=true)` 承認を after_spawn 列に残す |
| 4. 登録待ち | `list_peers` に worker が現れるまで 2 秒間隔リトライ（最大 30 秒） | **正規経路は現行と同型を維持**: worker 側 Claude の MCP クライアントが broker に接続（initialize handshake）した時点で broker が bind 表を「登録済み」に遷移させ、dispatcher は broker の `list_peers`（bind 表ベース）に worker が現れるまで現行 3-4 と同じ poll で待つ計画。補助として broker が `agent_ready` イベントを emit し、`poll_events` 派で待つ latency 改善経路も提供する（optional。正規の待ち方は `list_peers` poll であり、`agent_ready` に依存しない） |
| 5. 指示送信 | `send_message(to_id="worker-{task_id}", ...)` — renga がチャネル注入 | `send_message` → broker queue store 投入 → ナッジ配達（worker は起動直後で入力欄が空のため、静止確認は即時通過する想定） → worker が `check_messages` で本文取得 |

dev-channel prompt の消滅により、現行 spawn-flow の 3-3b（Enter 承認）と「承認しないと list_peers 待ちがタイムアウトする」結合が解消される見込みである一方、新たに「MCP サーバー接続の信頼確認」という未知数が入る。**段階 1・3・4 の置き換え成立（spawn → 接続 → 帰属 → 配達の一往復）は Phase 1 スパイクの合格条件 AC-2** とする（[§7.1](#71-phase-1-スパイクwezterm--windows中止判断点)）。3-3b / 3-4 相当の儀式を置き換えられないまま残る場合、Phase 2 以降に進まない。

### 4.7 terminal adapter の境界と能力表

adapter は「messaging adapter（Phase 3 が要求する最小能力）」と「full backend adapter（Phase 4 が要求する全能力）」の **2 段階で別物として定義**する。「adapter で何でも差し替え可能」という主張はしない — バックエンドごとに能力差があり、差は下表のとおり埋まらないものもある。

> **Phase 2 更新（2026-06-08〜09）**: tmux を第二 backend として実装（[`spike/tmux_adapter.py`](../../spike/tmux_adapter.py)）し、WezTerm 用 AC-1 / AC-2 ハーネスを backend パラメータ化（[`spike/terminal_adapter.py`](../../spike/terminal_adapter.py) に共有面を集約）。**POSIX（tmux 3.4 / WSL2）と Windows（WezTerm）の両 backend で AC-1（自動 3 状態）/ AC-2（接続チェーン）が green**（[`spike/RESULTS.md`](../../spike/RESULTS.md) の Phase 2 節）。これにより tmux 列は「将来・参考」から **実装済み・実測値** に格上げした。下表の tmux 列は Phase 2 の実測に基づく。

#### 4.7.1 backend 横断の能力比較（全能力）

| 能力 | renga | WezTerm（`wezterm cli`） | tmux（`tmux`、Phase 2 実装） | 要求フェーズ |
|---|---|---|---|---|
| pane への send-text（ナッジ注入） | ○ `send_keys` | ○ `send-text`（既定 bracketed paste） | ○ `send-keys`（一級プリミティブ） | **Phase 3（messaging）** |
| 制御打鍵モデル（Enter / Ctrl-C 等） | ○（高レベル API） | △ `send-text --no-paste` + `CR` / ETX（paste 既定のため小細工要） | ○ `send-keys Enter` / `C-c` / `-l`（小細工不要。Phase 2 実測） | **Phase 3（messaging）** |
| grid scrape（静止確認 defer の状態判定） | ○ | ○ `get-text` | ○ `capture-pane -p`（Phase 2 実測） | **Phase 3（messaging）** |
| pane 識別の安定名管理 | ○（name/role をサーバー管理） | △（pane id のみ。name↔id 対応表を adapter が保持） | △（pane id `%N`。同左を adapter が保持） | **Phase 3（messaging）** |
| headless / GUI 非依存運用 | ○ | △（GUI 非必須。mux-server のみで spawn 可。Phase 1 実測） | ○（detached session で標準動作。display 不要。Phase 2 実測） | 運用前提 |
| split spawn（cwd / command 指定） | ○ | ○ `split-pane` | ○ `split-window` / `new-session`（spike は new-session で検証） | Phase 4 |
| geometry 付き list_panes（cell 単位 rect） | ○ | ○ `list --format json`（rows/cols/位置） | ○ `list-panes -F`（`#{pane_left/top/width/height}`。Phase 2 実測） | Phase 4 |
| grid scrape（`inspect_pane` 相当） | ○ | ○ `get-text` | ○ `capture-pane`（Phase 2 実測） | Phase 4 |
| cursor 位置付き scrape | ○（`include_cursor`） | △（`get-text` 単体では不可。別途取得・要検証） | ○（`list-panes -F` の `#{cursor_x/y}` を同一呼出で取得。Phase 2 実測。WezTerm より優位） | Phase 4 |
| cursor 付き poll_events（lifecycle イベント） | ○（long-poll + next_since） | ✕ **ネイティブのイベントストリームなし** → adapter が list ポーリングから `pane_started` / `pane_exited` を**合成**（粒度・遅延は劣化） | △（`pane-died` 等の hooks で部分対応。spike は WezTerm と同じく list ポーリング合成。hooks 併用で改善余地あり） | Phase 4 |
| single-tab addressing（Set D §4.2 MUST） | ○（サーバーが強制） | △（tab 概念あり。adapter がスコープ強制を実装） | △（session / window 単位で同様。adapter がスコープ強制） | Phase 4 |
| IME-safe caret（hardware cursor 制御） | ○ | ✕ | ✕ | **対象外**（人間入力端末は renga 継続の根拠） |

#### 4.7.2 adapter 2 段階の能力境界（messaging / full backend）

「adapter で何でも差し替え可能」とは主張しない代わりに、**どの面が Phase 3 で要り、どの面が Phase 4 で要るか**を 2 表に分離して固定する。Phase 1 + Phase 2 のスパイクが実証したのは **messaging tier（+ 起動チェーン）のみ**であり、full backend tier の配線替え（spawn / inspect / poll_events）は Phase 4 スコープで未検証である。

**(a) messaging adapter（Phase 3 が要求する最小面）** — Phase 1（WezTerm）+ Phase 2（tmux）で両 backend 実証済み:

| 要求面 | WezTerm | tmux | 充足状況 |
|---|---|---|---|
| send-text（定型 1 行ナッジ注入） | ○ paste + `--no-paste` CR | ○ `send-keys -l` + `Enter` | 両 backend green（AC-1 / AC-2 roundtrip） |
| grid scrape（静止確認 defer 用の idle/busy/input_pending 判定） | ○ `get-text` | ○ `capture-pane -p` | 両 backend green（AC-1 全状態 / `classify_pane_state` 共有） |
| pane 識別（name↔id 対応） | △ adapter 保持 | △ adapter 保持 | adapter レイヤで充足 |
| 起動チェーン（`--mcp-config` 注入 + 信頼確認の機械承認 + 登録検知） | ○ | ○ | 両 backend green（AC-2） |

→ **結論**: messaging 移行に必要な面は **send-text + grid scrape + pane 識別 + 起動チェーン** に閉じ、renga / WezTerm / tmux いずれも充足。**メッセージング移行は backend 非依存**（Phase 1 + 2 で WezTerm / tmux 両実証）。

**(b) full backend adapter（Phase 4 が要求する追加面）** — 本スパイク未検証（配線替えは Phase 4 スコープ）:

| 要求面 | WezTerm | tmux | 差異・劣化 |
|---|---|---|---|
| split spawn（geometry 指定の balanced split） | ○ `split-pane` | ○ `split-window` | balanced split の現行同等性は Phase 4 で要実測（[§7.4](#74-phase-4-ペイン操作移行full-backend-adapter)） |
| geometry 付き list_panes | ○ | ○（Phase 2 で形は実測） | 同等 |
| grid scrape（`inspect_pane`） | ○ | ○ | 同等 |
| cursor 付き scrape | △（別取得・要検証） | ○（`list-panes` 同梱） | **tmux 優位** |
| poll_events（lifecycle、`pane_started`/`pane_exited`） | ✕ → ポーリング合成 | △ hooks + ポーリング合成 | 両者とも合成。Set D Q9 の best-effort + reconcile 許容内だが監視遅延が増える。tmux は hooks 併用の改善余地 |
| single-tab addressing（MUST） | △ adapter 強制 | △ adapter 強制 | 同等 |

→ **結論**: Phase 4 面は両 backend で概ね充足するが、**`poll_events` のポーリング合成の実効遅延**（dispatcher 監視ループの正しさ）と **balanced split の現行同等性**は未実測。Phase 4 の取り込み判断（[§7.4](#74-phase-4-ペイン操作移行full-backend-adapter)）で実測する。

- **full backend adapter**(Phase 4): `poll_events` の合成は両 backend でイベント粒度・遅延の劣化を伴う。Set D §3.1 が cursor-loss を best-effort + `list_panes` reconcile で許容している（Q9）ため、**ポーリング合成でも契約違反にはならない**が、dispatcher 監視ループの実効遅延が renga 比で増える。
- WezTerm / tmux いずれかの常駐が新たな前提になる（依存の付け替え側面）。**Phase 2 で tmux 第二実装を成立させたことにより「adapter 境界があるため二次移行は安価」という整理は机上ではなく実証になった**（同一ハーネス・同一 AC が backend パラメータ切替で両系 green）。POSIX 系は tmux、Windows 系は WezTerm を正準 backend とする運用が可能。

## 5. Contract Set D との整合（差分表）

[`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md)（2026-05-03 批准）に対する本設計の位置付けを Surface 単位で固定する。**本節は「改訂提案」であり、批准済み契約の本文は変更しない**。改訂の実施は、フォーク実験の成功後にフェーズ取り込みと同時に正式な契約改訂 PR（Set D の amendment 手続き）として別途行う計画である。二重正本を作らないため、**改訂が批准されるまでの正本は現行 Set D 本文**である。

| Set D Surface | 区分 | 差分の要点 |
|---|---|---|
| Surface 1: Pane control（1.1–1.9） | **継承**（公開境界のみ改訂提案） | 操作のセマンティクス（spawn / close / list_panes geometry / inspect_pane / send_keys / set_pane_identity、エラーコード、冪等性）は全て継承。変更は「誰に見せるか」のみ: worker / curator からは到達不能とし、dispatcher / secretary + broker 内部に限定する（[§4.2](#42-broker-mcp-surface役割別公開面)）。1.2 の dev-channel flag 注入義務は Surface 5 の改訂と連動して `--mcp-config` 注入義務に置き換える提案 |
| Surface 2: Messaging（2.1–2.4） | **改訂提案**（本設計で最大の変更点） | 2.1 の push-mode in-band 配達（Claude 宛チャネル注入）を廃し、**全受信者を pull-mode に統一**する提案（ナッジ + `check_messages`。現行 Codex 宛の pull 経路の一般化）。帰属フィールド `from_id` / `from_name` / `sent_at` の**意味論は継承**（HYBRID 正規性のまま）するが、**付与機構は改訂**: renga サーバーの pane 由来付与 → broker の token 由来付与となる計画。2.2 `list_peers` / 2.3 `check_messages`（at-most-once drain）/ 2.4 `set_summary` は意味論継承（実装主体が broker に変わるのみ） |
| Surface 3: Events（3.1） | **継承** | cursor-based long-poll、初回「今以降」セマンティクス、最小イベント語彙（`pane_started` / `pane_exited` / `events_dropped`）、30 秒 cap、best-effort + reconcile（Q9）を全て継承。broker が adapter イベントを正規化して同一面で提供する。WezTerm backend ではイベントがポーリング合成になるが、Q9 の best-effort 許容の範囲内（[§4.7](#47-terminal-adapter-の境界と能力表)）。補助イベント `agent_ready`（[§4.6](#46-起動フローの置き換え)）は「MAY emit + 未知 type は non-fatal」の既存規定内の optional 追加とし、**harness の正規の登録待ちはこれに依存しない**（`list_peers` poll が正道。Surface 2.2 継承の範囲で閉じる） |
| Surface 4: Identity & addressing（4.1–4.3） | **継承** | numeric id + stable name、全桁数字 = id 解釈、single-tab MUST（Q10）を継承。adapter がバックエンドごとにスコープ強制を実装する。**新設要素**: token ↔ pane/session の bind（[§4.4](#44-per-agent-token-のライフサイクル)）は識別の新レイヤであり、Surface 4 の改訂ではなく新 Surface（下記）に置く |
| Surface 5: Authentication / channel（5.1–5.2） | **改訂提案** | 5.1 dev-channel injection（flag 注入 + `send_keys(enter)` 承認）を**廃止し、`--mcp-config` による broker MCP 注入 + per-agent token 認証に置き換える**提案。5.2「transport は backend の自由（MAY）」は継承 — localhost HTTP はこの MAY の範囲内だが、認証要件（token 必須）は新設のため新 Surface に置く |
| Surface 6: Error code vocabulary（6.1–6.3） | **継承 + 新設** | `[<code>] <message>` 形式と最小語彙、ABI 安定性（6.2）、`backend_unreachable` 正規化（Q11、Issue #242）を継承。**新設コード**（6.2 の「MAY add」規定内）: `token_invalid` / `token_revoked` / `token_expired` / `nudge_failed`（静止確認リトライ枯渇）/ `adapter_unavailable`（broker は生きているが端末バックエンド側が不通 — `backend_unreachable`（broker 自体に到達不能）と区別する） |
| Surface 7: Backwards-compatibility | **継承** | broker MCP surface にも SemVer 義務をそのまま適用する |
| （新設）Surface 8 案: Broker auth & delivery | **新設** | per-agent token ライフサイクル（[§4.4](#44-per-agent-token-のライフサイクル)）、role-scoped ツール公開（[§4.2](#42-broker-mcp-surface役割別公開面)）、ナッジ配達契約（静止確認・冪等性・失敗時エスカレーション、[§4.3](#43-窓口への割り込み配達ナッジ最難関足切り対象)）、broker queue store の所有権（[§4.5](#45-broker-queue-storestatebroker-専用-subtree)。on-disk 面は **Set C の inventory 追加改訂と連動**させ、Set D 系統単独では閉じない）。Set D の追補 Surface とするか独立契約（Set G 等）とするかは契約改訂 PR の時点で判断する |

特に注意すべき非互換（移行時に harness prose の書き換えが必要な点）:

1. **受信モデルの変化**: 現行 prose は「`<channel source=...>` が in-band で届く」前提で書かれている（worker brief の「peer message を受けたら ack」等）。pull 統一後は「ナッジを見たら `check_messages`」に書き換える必要がある。Set D 2.1 の HYBRID 規定が「source 文字列をルーティングに使うな」と先に縛ってあるため、`from_*` / `sent_at` に依存した prose はそのまま生き残る。
2. **spawn 直後の儀式の変化**: dev-channel 承認（spawn-flow 3-3b）が消え、信頼確認プロンプト対応（有無・機械承認可否は Phase 1 AC-2 で実測）に変わる。`list_peers` 登録待ち（3-4）は **broker の bind 表ベース `list_peers` poll として同型のまま維持**する（`agent_ready` は latency 改善の補助であり正道ではない）。
3. **エラー分岐の追加**: `token_*` / `nudge_failed` / `adapter_unavailable` の分岐が dispatcher / secretary の error handling prose に追加される。未知コード non-fatal 規定（6.2）があるため、追加自体は破壊的でない。

## 6. non-goals との関係

- **[`docs/non-goals.md`](../non-goals.md) §12「MCP の HTTP 公開形式の外部統合は持たない」**: broker MCP は localhost HTTP（**host-local only**、127.0.0.1 bind + per-agent token 必須）であり、§12 が否定する「外部公開」（ブラウザ拡張・別マシン IDE からの接続、TLS / ネットワーク境界の問題）には該当しない。また §12 の代替手段節が認める「別途 MCP の HTTP サーバーを併設する設計も可能ですが、claude-org-ja 本体の責務外とします」と整合し、broker 実体を claude-org-runtime 側に置くことで「本体の責務外」を保つ。ただし §12 の理由節にある「`renga-peers`（ローカル標準入出力経由）に集約」「同一タブ内 P2P が通信モデルの正本」という記述は Phase 3 取り込み時に実態と乖離するため、**その時点で non-goals §12 の改訂（host-local 例外の明文化）を契約改訂 PR に含める**ことを提案する（本設計書からは提案のみ。規範文書は変更しない）。
- **§6「PTY や端末多重化器の層を持たない」**: broker / adapter は PTY 注入・ペイン制御を含む Layer 3 相当の責務であり、本リポジトリには持ち込まない。実体は claude-org-runtime または新規リポジトリに置く（[§1](#1-背景と確定制約本設計が覆さない前提)）。
- **§5「複数プロバイダー切替はしない」**: broker は端末バックエンドの差し替えであり、エージェント（Claude Code）の差し替えではない。Claude 専用の立ち位置は変えない。

## 7. Phase 計画と移行完了判定基準

各 Phase の「何を通せば移行完了か」を先置きで固定する。いずれもフォーク上で実証してから本体に取り込む。

### 7.1 Phase 1: スパイク（WezTerm / Windows、中止判断点）

最小実証: 対話ペイン spawn → broker MCP 接続（`--mcp-config` 注入）→ token 帰属付きナッジ → `check_messages` の一往復。

合格条件は AC-1（ナッジ 4 状態）と AC-2（接続チェーン）の **2 本立てで、両方の合格を Phase 2 以降へ進む条件**とする。

**AC-1 — ナッジ注入の 4 状態テスト（go/no-go、計画中止条項付き）**:

受信側（窓口役）ペインが以下の **4 状態それぞれにあるときにナッジを注入し、いずれの状態でも窓口の入力を壊さないこと**:

| # | 受信側の状態 | 合格基準 |
|---|---|---|
| 1 | **idle**（入力欄が空でプロンプト静止） | ナッジが 1 メッセージとして Claude セッションに到達し、画面・履歴に乱れがない |
| 2 | **IME 変換中**（日本語入力の変換窓が開いている） | 変換中の文字列・変換窓・確定操作が破壊されない。ナッジは defer され、変換確定（入力欄静止）後に配達される |
| 3 | **長文入力中**（未送信の複数行テキストが入力欄にある） | 入力中のテキストにナッジ文字列が混入しない。ユーザーの未送信テキストが勝手に送信されない |
| 4 | **Claude 出力ストリーミング中**（スピナー / 応答生成中） | 出力の描画が乱れず、ナッジが応答完了後に正しく処理される（入力キューに滞留したまま消えない、を含む） |

- 判定は**全 4 状態の合格が必須**。1 つでも「窓口入力を壊す」結果が再現可能に出た場合、緩和策（[§4.3](#43-窓口への割り込み配達ナッジ最難関足切り対象) の静止確認 defer 等）を尽くしても解消しなければ、**本計画ごと中止（棚上げ）する**。AC-1 のみが「計画中止」を導く足切り条項である。

**AC-2 — 起動・接続チェーンの置き換え成立（Phase 2 以降へ進む前提条件）**:

現行 spawn-flow 3-2〜3-5 の儀式（[§4.6](#46-起動フローの置き換え)）を broker 方式で置き換えた一往復が成立すること。具体的には以下すべて:

1. `--mcp-config` 注入で spawn した対話ペインの Claude が broker MCP に接続できること。**信頼確認プロンプトが出る場合は、orchestrator が `send_keys` で機械的に承認可能であること**（人間の手作業が必要なら不合格）。
2. per-agent token の env 受け渡しと認証が成立し、broker が `from` 帰属を token から正しく付与すること。
3. 登録検知が成立すること: broker の `list_peers`（bind 表ベース）poll で spawn 直後のエージェント出現を現行 3-4 同等のタイムアウト感（〜30 秒）で検出できること。
4. Windows（PowerShell / ConPTY）での send-text に文字化け・取りこぼしがないこと。

- AC-2 の不合格は AC-1 と異なり即「計画中止」ではない（実装手段の変更で解消しうる）が、**解消されるまで Phase 2 以降へ進まない**。3-3b / 3-4 相当の置き換えが成立しないまま「renga 併用で進める」中間状態は採らない。

### 7.2 Phase 2: 棚卸し・契約整合

- 完了判定: 全呼出箇所の 3 分類棚卸し（[§3](#3-mcp__renga-peers__-呼出箇所の棚卸し)）と Set D 差分表（[§5](#5-contract-set-d-との整合差分表)）が本設計書として固定され、レビューを通過していること。**本ドキュメントの作成がこの Phase の成果物に該当する**（ただし契約改訂そのものは含まない）。

### 7.3 Phase 3: メッセージング移行（messaging adapter）

フォークで `send_message` / `check_messages` / `list_peers` / `set_summary` の呼出を broker ツールへ配線替えし、以下を通したら本体へ取り込む:

- worker / curator / dispatcher / secretary 間の全メッセージ経路（完了報告 / ack / 判断仰ぎ / DELEGATE / CURATE_* / retro gate）が broker 経由で一巡すること（renga チャネル不使用で 1 委譲サイクル完走）。
- ナッジ配達の実運用成立: 静止確認 defer が IME / 長文入力と共存し、配達遅延が運用上許容できること（attention watcher の通知経路が壊れないことを含む）。
- 帰属の検証: 全メッセージの `from` が token 由来で正しく付き、なりすまし送信（他 agent の to_id を騙る試行）が構造的に不可能であること。
- 取り込み時の同時変更: 分類 (b) の許可スキーマ再宣言、分類 (a) のメッセージング系 prose 書き換え、Set D Surface 2 / 5 の契約改訂 PR、**Set C の state files inventory への `.state/broker/` subtree 追加改訂**（[§4.5](#45-broker-queue-storestatebroker-専用-subtree)）、non-goals §12 の改訂提案。

### 7.4 Phase 4: ペイン操作移行（full backend adapter）

フォークで spawn / close / list_panes / inspect_pane / send_keys / poll_events を broker + adapter へ配線替えし、以下を通したら本体へ取り込む:

- WezTerm backend のみ（renga 不使用）で、delegate → spawn → 監視（stall 検出 / 承認待ち観測を含む）→ 完了報告 → CLOSE_PANE → retro の 1 サイクルが完走すること。
- `poll_events` ポーリング合成の実効遅延が dispatcher 監視ループ（3 分 cadence）の正しさを損なわないこと（pane_exited 取りこぼしが list_panes reconcile で回復すること）。
- balanced split が WezTerm の geometry 情報で現行同等に機能すること。
- 取り込み時の同時変更: Surface 1 / 3 / 4 関連の prose 書き換えと契約改訂 PR、Surface 8 案（または Set G）の新設批准。

### 7.5 並走実験の分離

フォーク組織を本体と並走させる場合、ダッシュボードポート・workers_dir・`.state/`（state.db / broker queue store）の分離が必要。フォーク側の設定で衝突を避ける（実験手順の詳細はフォーク側 README に置く計画で、本体には持ち込まない）。

## 8. 残存リスク（既知・設計時点）

| リスク | 整理 |
|---|---|
| ナッジ注入の混線 | 受信側が長文入力中だと renga のチャネル注入より一段劣る。静止確認 defer（[§4.3](#43-窓口への割り込み配達ナッジ最難関足切り対象)）で緩和するが、最終判定は Phase 1 の 4 状態 AC-1（[§7.1](#71-phase-1-スパイクwezterm--windows中止判断点)）。**壊れたら計画ごと中止** |
| WezTerm 常駐の新前提化 | renga 依存を外す代わりに端末 backend（WezTerm / tmux）+ broker デーモンが前提に加わる。adapter 境界により二次移行は安価 — **Phase 2 で tmux 第二実装を成立させ、同一 AC が backend パラメータ切替で両系 green になることを実証**（[§4.7](#47-terminal-adapter-の境界と能力表)）。POSIX=tmux / Windows=WezTerm の使い分けが可能 |
| イベント合成の劣化 | WezTerm にはネイティブの pane lifecycle イベントがなく、ポーリング合成になる（[§4.7](#47-terminal-adapter-の境界と能力表)）。Set D Q9 の best-effort 許容内だが、監視の実効遅延は増える |
| broker の単一障害点化 | 現行 renga サーバーも同様の単一点だが、broker はデーモン管理（起動・再起動・queue store の復旧）という新しい運用責務を持ち込む。Phase 3 取り込み時に起動・死活の runbook を用意する |
| token 漏洩 | env 経由の子プロセス漏洩が理論上ありうる。revoke-on-exit + TTL + localhost bind + role scope で被害面を限定（[§4.4](#44-per-agent-token-のライフサイクル)） |

## 9. スコープ外（将来課題）

- **dispatcher の決定的処理の Python 化**: 監視ループ等を broker 側 code に寄せる構想はあるが、本設計のスコープ外（一次入力で合意済み）。
- **at-least-once 配達への強化**: Set D 2.3 の at-most-once drain を継承する（[§5](#5-contract-set-d-との整合差分表)）。broker queue store は永続化を持つため、将来 ack ベースの再配達に強化する余地はあるが、契約変更を伴うため本設計では扱わない。
- ~~**tmux adapter**: 能力表に参考として載せたのみ。実装計画はない。~~ → **Phase 2 で実装済み**（[`spike/tmux_adapter.py`](../../spike/tmux_adapter.py)、POSIX 正準 backend）。能力表（[§4.7](#47-terminal-adapter-の境界と能力表)）の tmux 列は実測値に更新済み。残るスコープ外は messaging / pane-control 以外の tmux 固有機能（copy-mode 連携等）であり、本設計の対象外。
- **focus_pane / new_tab の broker 公開**: 初期 surface から除外（[§4.2](#42-broker-mcp-surface役割別公開面)）。人間向け補助が必要になった時点で追加を検討する。

## 改訂履歴

- 2026-06-07: 初版（design only。renga-decoupling-design 委譲タスクの成果物）
- 2026-06-08: Phase 1 スパイク（WezTerm / Windows）の AC-1 / AC-2 結果を反映（Phase 1 ゲート通過）。
- 2026-06-09: Phase 2。tmux adapter（POSIX 正準 backend）を第二実装として追加し、ハーネスを backend パラメータ化。両 backend（tmux/WSL2・WezTerm/Windows）で AC-1 / AC-2 green。§4.7 の tmux 列を「参考」から実測値に格上げし、messaging（Phase 3）/ full backend（Phase 4）の能力境界を 2 表に分離（§4.7.2）。§8 リスク表・§9 スコープ外の tmux 記述を更新（Closes #2）。
