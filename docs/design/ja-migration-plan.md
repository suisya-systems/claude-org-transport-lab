# ja 移行方針 — broker/adapter の claude-org-runtime 抽出と renga 互換 surface 差し替え

> ステータス: **design only / 実装なし**。本ドキュメントは Epic #6（renga 依存解消 / Plan B）の**次段**の設計であり、コード変更・本番 ja への適用は一切含まない。
> 位置付け: [`docs/design/renga-decoupling.md`](./renga-decoupling.md)（Plan B 設計 SoT、完動ゲート = GO まで反映済）の続編。前段は「フォークで broker + adapter が成立すること」を実証した（[`spike/RESULTS.md`](../../spike/RESULTS.md) Phase 1〜5）。本段は「その成果を本番 ja に**どう移すか**」の移行方針を確定する。
> 一次入力: [`docs/design/renga-decoupling.md`](./renga-decoupling.md) §3〜§7（呼出棚卸し・broker 設計・Phase 計画）、[`spike/broker.py`](../../spike/broker.py)（Phase 4/5 で確定した MCP surface + allowlist guard）、[`spike/terminal_adapter.py`](../../spike/terminal_adapter.py)（adapter Protocol + key 語彙）、renga-peers MCP ツール群の実シグネチャ（本タスクで全数照合）、`claude_org_runtime` 0.1.14 の実パッケージ構成（本タスクで実測）。
> 依存ドキュメント（参照は本書 → 既存文書の一方向のみ）:
> - [`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md)（Set D。surface セマンティクスの正本）
> - [`docs/contracts/state-schema-contract.md`](../contracts/state-schema-contract.md)（Set C。`.state/` ファイル台帳）
> - [`docs/contracts/state-semantics-contract.md`](../contracts/state-semantics-contract.md)（Set F。state.db SoT）
> - [`docs/non-goals.md`](../non-goals.md)（§6 PTY 層 / §12 MCP HTTP 外部公開）

---

## 1. 目的とスコープ

完動ゲート（Epic #6）が GO となり、フォーク（claude-org-transport-lab）上で broker + terminal adapter が renga 不使用で委譲サイクルを複数回完走することが実証された。本段の目的は、この実証済み資産を **本番 ja に移行する方針を design only で確定**することである。

確定すべき 5 項目（CLAUDE.md impl-guidance に対応）:

- **(a)** broker MCP surface の renga-peers drop-in 互換性調査 + gap 洗い出し（[§3](#3-a-broker-mcp-surface--renga-peers-の-drop-in-互換性調査--gap)）
- **(b)** broker + terminal adapter を `claude_org_runtime` に抽出する設計（[§4](#4-b-runtime-抽出設計claude_org_runtime)）
- **(c)** ja 統合シーム最小化設計（[§5](#5-c-ja-統合シーム最小化設計)）
- **(d)** イベント取得を tmux control mode にするか差分 reconcile を維持するかの設計判断（[§6](#6-d-設計判断イベント取得--tmux-control-mode-vs-差分-reconcile)）
- **(e)** 次段の Issue 分解案（[§8](#8-e-issue-分解案)）

**スコープ外（本書で扱わない）**: 実装、本番 ja への適用、契約改訂本文の批准（提案のみ）、dispatcher 決定的処理の Python 化（renga-decoupling.md §9 のとおり将来課題）。

**覆さない確定制約**（renga-decoupling.md §1 から継承）:

1. 課金制約: 全エージェントは対話型 TUI セッション（ヘッドレス不可）。
2. IME 制約: 人間が日本語入力する端末（窓口ペイン）は renga を継続使用。→ **renga と broker は併存する**（後述、移行設計の最重要前提）。
3. 採用方針 = 案 B: 輸送層だけを broker 非依存化し、renga は「必須前提」から「ユーザーの端末選択」に降格。破壊的にしない opt-in 追加方式。

---

## 2. 移行の全体像

```
  現状 (ja, 実装・運用中)              次段 (本書の設計対象)
  ┌─────────────────────┐          ┌──────────────────────────────┐
  │ claude-org-ja        │          │ claude-org-ja                │
  │  prose/skills →      │          │  prose/skills → transport flag │
  │   mcp__renga-peers__*│  ──────▶ │   ├ renga 経路 (既定/切戻し先)  │
  │                      │          │   └ broker 経路 (opt-in)        │
  │ deps:                │          │  deps: runtime pin bump         │
  │  claude-org-runtime  │          │   (broker + terminal を内包)    │
  │   0.1.x (choose_split│          └──────────────┬───────────────┘
  │   / schema / settings)│                        │ pin consume
  └─────────────────────┘          ┌──────────────▼───────────────┐
                                    │ claude-org-runtime (抽出先)    │
                                    │  既存: dispatcher.runner       │
                                    │   (choose_split) / schema /    │
                                    │   settings / attention / migrate│
                                    │  新規: broker/ + terminal/      │
                                    │   (spike/ から移設)             │
                                    └────────────────────────────────┘
```

移行は「**runtime を太らせて（抽出）→ ja は pin で consume（向け替え）→ flag で opt-in（切戻し可）**」の 3 段で、各段が前段に依存する。renga 経路は削除せず併存させる（制約 2・3）。

---

## 3. (a) broker MCP surface と renga-peers の drop-in 互換性調査 + gap

ja 側（CLAUDE.md / skills / dispatcher references）は renga 固有ツールを **MCP の完全修飾名**（`mcp__renga-peers__<tool>`）で直接呼ぶ。配線替え量を最小化するには、broker surface をこれらと同名・同形に寄せるのが基本方針となる。本節は現 broker surface（[`spike/broker.py`](../../spike/broker.py)）と renga-peers の **required 14 ツール**を全数照合した対応表と gap である。

> **required surface の SoT**: ja が要求する renga-peers ツールは [`tools/check_renga_compat.py`](../../tools/check_renga_compat.py) の `REQUIRED_MCP_TOOLS`（renga 0.18.0、**ちょうど 14**）が正本。`spawn_codex_pane` は**この required surface に含まれない**（後述、別枠扱い）。`org_extension_schema.json` の各ロール allowlist もこの 14 と整合する。

### 3.1 ツール対応表（全数）

| renga-peers ツール | broker 現状 | 引数形 | 戻り形 | drop-in 区分 |
|---|---|---|---|---|
| `send_message(to_id, message)` | `send_message` | **同一** | 同一（`{ok, delivered_to}`） | ✅ 完全 |
| `set_summary(summary)` | `set_summary` | **同一** | 同一（`{ok}`） | ✅ 完全 |
| `check_messages()` | `check_messages` | **同一** | 同一（`{messages:[…]}`） | ⚠️ 名/形同一・**意味論差**（push→pull） |
| `list_peers()` | `list_peers` | **同一** | `id/name/role/summary`（renga は + `cwd/kind/receive_mode`） | ⚠️ フィールド欠落（小） |
| `list_panes()` | `list_panes` | **同一** | `id/name/role/focused/x/y/w/h(/cursor)`（renga は + `cwd/kind/receive_mode`） | ⚠️ フィールド欠落（小） |
| `inspect_pane(target, lines, format, include_cursor)` | `inspect_pane` | `format` **欠落**、他同一 | `text/state(/cursor)`（renga は `format=grid` / `structuredContent`） | ⚠️ `format=grid` 欠落 + addressing |
| `send_keys(target, text, keys, enter)` | `send_keys` | **同一**（キー語彙も一致※） | 同一（`{ok}`） | ⚠️ addressing のみ |
| `poll_events(since, timeout_ms, types)` | `poll_events` | **同一** | 同一（`next_since` + `events[]`、event `id`=handle） | ✅ ほぼ完全 |
| `close_pane(target)` | `close_pane` | **同一** | `{ok, closed}` | ⚠️ addressing のみ |
| `set_pane_identity(target, name, role)` | `set_pane_identity` | three-state の **null クリア欠落** | 同一 | ⚠️ null クリア欠落 + addressing（+ role は表示専用に再定義※） |
| `spawn_claude_pane(direction, target, name, role, model, permission_mode, args, cwd)` | `spawn_agent(agent_id, name, role, argv, cwd, target, direction)` | **大幅相違** | `{ok, handle, direction}` | ❌ rename + 形相違 |
| `spawn_pane(command, …)`（generic） | **無し** | — | — | ❌ 欠落 |
| `new_tab(…)` | **無し** | — | — | ❌ 欠落（設計上の意図的除外） |
| `focus_pane(target)` | **無し** | — | — | ❌ 欠落（設計上の意図的除外） |

上表が required 14 ツール（`tools/check_renga_compat.py` `REQUIRED_MCP_TOOLS` と一致）。**`spawn_codex_pane` は required surface 外**（compat checker・各ロール allowlist のいずれにも無い）。codex を peer pane として spawn する運用は ja の必須面ではないため、broker は初期 surface から**除外**してよい（codex design review は `codex exec` CLI 経路で peer pane を要さない）。これは [§9](#9-確認したい設計判断点窓口人間へ) の確認点 3 を SoT 照合で裏付けた結論である。

※ `send_keys` のキー語彙は照合済みで一致（renga: Enter/Return, Tab, Shift+Tab/BackTab, Esc/Escape, Backspace, Delete/Del, 矢印, Home/End, PageUp/PageDown, Space, Ctrl+A-Z ＝ broker `SEND_KEYS_VOCAB`/`normalize_key` と同一。未知キーは両者 `-32602 invalid-params`）。
※ `set_pane_identity` の `role` を broker は **表示専用ラベル**に再定義し、権限 tier は不変 `auth_role` のみで決める（[`spike/broker.py`](../../spike/broker.py) の codex Blocker 対応）。これは renga にない**意図的なセキュリティ強化**であり gap ではなく improvement。

### 3.2 gap の 4 分類

1. **意味論差（prose 書き換えで追随）**: `check_messages` の受信モデルが push（renga の in-band channel 注入）→ pull（ナッジ + `check_messages`）に変わる。ツール名・形は同一だが、worker brief 等「`<channel>` が届いたら ack」の prose が「ナッジを見たら `check_messages`」に変わる（renga-decoupling.md §5 非互換 1）。
2. **フィールド欠落（小・任意追加）**: `list_peers`/`list_panes` の `cwd`/`kind`/`receive_mode`、`inspect_pane` の `format=grid`。ja の実運用呼出が消費しているフィールドのみ補えばよい（後述 §3.3-4）。
3. **横断的 gap（addressing）**: **broker の pane 操作は target を broker handle（整数 / 全桁数字）でしか解決しない**（`_resolve_target`）。renga は `id` / **stable name** / リテラル `'focused'` を受ける。ja の dispatcher/secretary prose は `to_id="secretary"` 同様に **name や `'focused'` で pane を指す**箇所がある。これを補わないと、pane を name/`'focused'` で指す全呼出箇所が「handle を一旦引く」二段化を強いられる。**互換性上もっとも効く単一の改善点**。
4. **構造的相違・欠落（設計判断が要る）**: `spawn_claude_pane`（rename + 形相違）、`spawn_pane`/`spawn_codex_pane`/`new_tab`/`focus_pane`（欠落）。

### 3.3 互換性を上げる設計推奨（配線替え量を最小化する寄せ方）

drop-in 度を上げる＝ja 側の論理・retraining を最小化する、ための broker surface 改修案。**いずれも renga と同名・同形に寄せる方向**で、Phase（[§8](#8-e-issue-分解案)）の compat surface 課題に集約する。

1. **`spawn_claude_pane` 名・形を broker 第一級 surface にする（最重要）**。現 `spawn_agent`（raw `argv`）は残しつつ、renga と同シグネチャの `spawn_claude_pane(direction, target?, name?, role?, model?, permission_mode?, args?, cwd?)` を broker が公開し、**broker 内部で対話 TUI argv を組み立てる**。これには副次的な大きな利点がある: broker が argv を構築する経路では、`is_interactive_claude_argv` allowlist が「caller argv の検査（false-reject リスクあり）」ではなく「**broker 自身のビルダー出力（構造的に対話 TUI 確定）**」になり、課金中立 guard の保守契約（[renga-decoupling.md §7.6](./renga-decoupling.md#76-完動ゲートphase-5--ac-5--epic-6-最終ゲート)）の false-reject 面が消える。`agent_id` は `name` から導出（無ければ生成）する。
2. **pane 操作 target に name / `'focused'` 解決を追加（横断 gap の解消）**。broker の bind 表は既に name↔pane↔handle を保持しているため、`_resolve_target` を「整数 handle / 全桁数字 → handle」に加えて「**非数字 str → bind 表の name 一致 → handle**」「`'focused'` → `list_panes` の focused 一致」へ拡張する。Set D §4.1（全桁数字=id 解釈）を保ったまま renga の addressing 三系統に揃う。
3. **generic `spawn_pane` の最小公開（secretary のみ）**。`/org-attention-start` の attention watcher は renga の generic `spawn_pane(command=…)` で起動される（renga-decoupling.md §3.1）。broker は既に `inject_mcp_config=False` の非 org spawn 経路（blacklist のみ・token 非注入）を内部に持つため、これを secretary tier に最小公開すれば watcher 経路が壊れない。renga-decoupling.md §4.2 が secretary 専用として予定済み。
4. **フィールド parity**。`cwd` は **Set D の `list_panes`/`list_peers` 出力に含まれる契約面**であり、「ja が消費していれば追加」より強く扱う ＝ **compat surface（Issue C）の必須項目に格上げ**する（broker は spawn 時に cwd を知るため bind 表に持てる）。省略する場合は Set D amendment として明示が要る。`inspect_pane` の `format=grid` は ja の実呼出が依存している場合のみ追加（YAGNI。Phase C で grep 確定）。`receive_mode`/`kind` は broker では概念が異なる（全 pull 統一）ため、定数化 or 省略を Set D amendment で明記し prose 非破壊にする。
5. **`set_pane_identity` の null クリア three-state を追加**（renga と同形）。role の表示専用化は維持。

### 3.4 重要結論 — 併存設計のため broker は別名（`org-broker`）を採り、FQ 名は書き換わる

renga と broker は**併存する**（制約 2: 人間入力の窓口ペインは renga 継続。制約 3: renga 故障時の縮退先として renga 経路を残す）。MCP サーバーが同一マシン・同一セッションで両方見える状態になりうるため、**本設計では併存・切戻し安全性を優先して broker の MCP サーバー名を `renga-peers` と別の `org-broker` にする**（同名にすると同時登録時に衝突する）。理論上は per-session `--strict-mcp-config` で broker-only セッションに `renga-peers` 別名を切る余地もあるが、縮退運転（renga と broker の同居）と段階移行（messaging だけ broker 等）の安全性を取り、別名で固定する。したがって完全修飾ツール名は `mcp__renga-peers__send_message` → `mcp__org-broker__send_message` のように**プレフィックスが書き換わる**。

帰結:
- **「drop-in 互換」が成立するのは引数形・セマンティクスのレベル**（ja の論理・retraining を最小化）であって、**prose 中のリテラル FQ 名と allowlist 文字列は機械的に書き換わる**（renga-decoupling.md の分類 (a)/(b)）。
- ゆえに ja 側の「向け替え」は、(i) §3.3 で形を renga に寄せて**論理差をゼロに近づけ**、(ii) FQ プレフィックスの差し替えを**生成系の単一シーム**に閉じる（[§5.2](#52-transport-プレフィックスを生成系の単一シームに閉じる)）、の二段で最小化するのが正攻法である。

---

## 4. (b) runtime 抽出設計（claude_org_runtime）

### 4.1 抽出先は既存の `claude_org_runtime`（新規リポジトリは作らない）

`claude-org-ja` は既に `claude-org-runtime>=0.1.9,<0.2` を pin 依存している（`pyproject.toml`、現行 0.1.14）。runtime には既に `dispatcher.runner.choose_split`（split SoT、Phase 4 で broker が再利用済）/ `schema/`（org_state・journal_event・enums）/ `settings/generator` / `attention/` / `migrate/` / `cli.py` がある。**broker + terminal adapter はこの既存 runtime に subpackage として抽出する**のが自然（新規リポジトリ不要・ja の pin consume 機構をそのまま使える・choose_split 同居）。renga-decoupling.md §1 の「実体は claude-org-runtime（既存の別パッケージ）または新規リポジトリ」のうち**前者を採る**。

### 4.2 パッケージ境界

```
claude_org_runtime/
├── dispatcher/runner.py      # 既存。choose_split (broker が再利用)
├── schema/                   # 既存。state schema (broker は触らない)
├── settings/generator.py     # 既存。allowlist 生成 (§5.3 で flag-aware 化)
├── attention/                # 既存
├── terminal/                 # ★新規 (spike/ から移設)
│   ├── base.py               #   TerminalAdapter Protocol / PaneRef / PaneId
│   │                         #   / classify_pane_state / SEND_KEYS_VOCAB / normalize_key / make_adapter
│   ├── tmux.py               #   TmuxAdapter   (POSIX 正準)
│   ├── wezterm.py            #   WezTermAdapter (Windows 正準)
│   └── renga.py              #   ★RengaAdapter (新規。renga を backend として駆動。§4.5)
└── broker/                   # ★新規 (spike/ から移設)
    ├── server.py             #   Broker + _McpHandler (localhost HTTP MCP)
    ├── store.py              #   queue store (.state/broker/ subtree)
    ├── tokens.py             #   AgentBind / token lifecycle / argv allowlist guard
    ├── surface.py            #   tool 定義 + role tier (renga 互換名で再編、§3.3)
    └── cli.py                #   daemon entry (org-start から起動)
```

分割は責務単位。スパイクの単一 `broker.py`（1577 行）は `server`/`store`/`tokens`/`surface` に割る（テスト境界も明確化）。

### 4.3 何を移すか（spike → runtime 対応）

| spike 資産 | 抽出先 | 備考 |
|---|---|---|
| `broker.py` の Broker/HTTP | `broker/server.py` | role tier・poll_events 合成・nudge を含む |
| `broker.py` の AgentBind/token/argv guard | `broker/tokens.py` | allowlist 保守契約（§7.6）を継承 |
| `broker.py` の queue/journal | `broker/store.py` | 書き込み先を `spike/broker-state/` → `.state/broker/`（Set C 改訂） |
| `broker.py` の TOOLS/tier 表 | `broker/surface.py` | §3.3 で renga 互換名に再編 |
| `terminal_adapter.py` | `terminal/base.py` | Protocol + classify + key 語彙 |
| `tmux_adapter.py` / `wezterm_adapter.py` | `terminal/tmux.py` / `wezterm.py` | そのまま |
| （新規） | `terminal/renga.py` | renga を backend にする adapter（§4.5） |
| `tests/test_broker_*.py` | runtime のテストスイート | CI は runtime 側で常設化 |
| `run_ac*.py` / `*-design-note.md` | フォークに残置（移植しない） | 検証アーティファクト。本体には持ち込まない |

### 4.4 依存方向（一方向・循環なし）

```
ja (prose/skills/settings)  ──pin──▶  claude_org_runtime
                                         broker/  ──▶ terminal/ (adapter Protocol)
                                         broker/  ──▶ dispatcher.runner.choose_split (既存・再利用)
                                         terminal/ ──▶ (stdlib のみ。runtime 内部に依存しない)
```

- **broker は ja を import しない**（ja は runtime を pin consume するのみ。逆依存を作らない）。
- **broker → terminal**（adapter Protocol）と **broker → choose_split**（split SoT 再利用）の 2 方向のみ。
- terminal は runtime 内部に依存しない葉パッケージ（stdlib + backend CLI 呼出のみ）。
- 既存 `schema`/`state.db` writer と broker は**所有領域を分離**: broker は `.state/broker/` のみ書き、state.db（runs/org_sessions/events/worker_dirs）には一切書かない（Set F 非干渉。renga-decoupling.md §4.5）。

### 4.5 既存資産との同居方針

- **choose_split 再利用**: balanced split は再実装せず `dispatcher.runner.choose_split` を呼ぶ（Phase 4 で実証済の現行同等保証）。prose doc は runtime と drift 済のため移植しない。
- **`.state/broker/` subtree**: 唯一の書き手は broker。state.db / 既存 journal とは subtree 単位で所有権を分離（Set C の inventory 追加改訂が必要 ＝ path/format/owner=broker/readers/migration を Set C に足す。renga-decoupling.md §4.5）。
- **RengaAdapter（新規）の位置付け**: renga を「broker の一 backend」として駆動する adapter。これにより「broker 経路だが端末は renga」という構成（renga-decoupling.md §4.1 の縮退運転先・人間入力端末との共存）が adapter 差し替えで表現できる。ただし実装優先度は低い（tmux/WezTerm で完動ゲート GO 済）。設計上の余地として置き、初期実装は tmux/WezTerm に限る。
- **settings.generator**: allowlist 生成は既存 generator に flag-aware の分岐を足す（[§5.3](#53-allowlist-分類-b-生成を-flag-aware-にする)）。

### 4.6 daemon / CLI entry

org-start が broker daemon を起動できるよう `broker/cli.py` に entry を足し、runtime の `cli.py` から `python -m claude_org_runtime.broker` 等で起動可能にする。死活・再起動の runbook は Phase の取り込み時に用意（renga-decoupling.md §8「broker の単一障害点化」リスク）。spike の `if __name__ == "__main__"` standalone 起動はこの entry の原型。

### 4.7 versioning と paired ja sync

- broker surface は SemVer 義務（Set D Surface 7 継承）。broker を追加する runtime リリースは**加算的**（既存 API 不変）であり、ja の pin `<0.2` 範囲内の minor bump で consume 可能（破壊しない）。
- **runtime リリースは ja 側 expectation 同期とペアで行う**（`runtime-release-with-paired-ja-sync` skill 該当）。DEFAULT_NOTIFY / classifier vocab / org_extension_schema / attention テンプレが変わる場合に CI cascade を予防する。本抽出で `settings/generator` を flag-aware 化する（§5.3）ため、この skill の発動条件に該当しうる。Issue 分解（§8）で runtime リリース課題に paired sync を明記する。

---

## 5. (c) ja 統合シーム最小化設計

破壊的にしない opt-in 追加（Epic #6 制約）として、ja 側の改変を「**1 つの flag + 1 つの生成系シーム**」に集約する。

### 5.1 backend 選択 flag

- **flag の所在**: 初期は**環境変数 `ORG_TRANSPORT`（`renga` | `broker`）に限る**。org-start / spawn-flow が起動時に 1 度読む。`.state/org-config.json` のような永続ファイル化は、**それ自体が Set C inventory への追加改訂対象になる**ため、env で済む初期段階では持ち込まず、永続設定が必要になった時点で別 Issue（Set C 改訂を伴う）に分離する。env のみなら Set C 改訂を増やさずに済む（非破壊・最小）。
- **既定 = `renga`**（無設定時は現行どおり。挙動不変 ＝ 非破壊）。`broker` は明示 opt-in。
- flag は **org 全体で 1 値**（worker ごとに混在させない。混在は帰属・配達の整合を壊す）。ただし制約 2 の通り**人間入力の窓口ペインは flag に依らず renga 継続**であり、broker 経路でも窓口は renga 端末上で broker MCP を consume する構成になる（端末 = renga、輸送 = broker）。

### 5.2 transport プレフィックスを生成系の単一シームに閉じる

ja の renga ツール参照は (i) **生成されるもの**（`tools/gen_delegate_payload.py` / worker_brief テンプレート / `org_extension_schema.json` / settings、＝ runtime の `settings/generator` 由来）と (ii) **静的 prose**（CLAUDE.md / skills / dispatcher references）に分かれる。

- **(i) 生成系**: transport プレフィックス（`renga-peers` / `org-broker`）と spawn 注入 flag（`--dangerously-load-development-channels server:renga-peers` / `--mcp-config <broker>`）を**テンプレート変数**にし、flag から render する。worker brief・delegate payload・allowlist が **flag 一つで両系に振り分く**。配線替えの主シーム。
  - **所有境界の注意**: 生成器は 1 つではない。`settings/generator`（allowlist。runtime 側）と `tools/gen_delegate_payload.py` / worker_brief テンプレート（delegate payload・brief。**ja 側資産**）が別々に同じ transport prefix / tool set を必要とする。これを各所にハードコードすると drift する。**runtime に小さな共有データ（transport surface descriptor: flag → {server 名, 注入 flag, ロール別 tool 名集合}）を 1 つ置き、runtime の settings generator と ja 側生成器の双方がこの descriptor を読む**設計にする（単一 SoT）。descriptor は加算的な runtime API で、ja は pin consume する。両生成器の出力が descriptor と一致する golden test を Issue D に置く。
- **(ii) 静的 prose**: 受信モデル（push→pull）・spawn 儀式（dev-channel 承認 → folder trust prompt の機械承認）・エラー分岐（`token_*` / `nudge_failed` / `adapter_unavailable` 追加）は**意味が変わる**ため、両系を併記するか flag 条件付き記述にする。これは renga-decoupling.md 分類 (a) の prose 書き換えそのもの。**論理差を §3.3 で最小化しておくほど、この prose 改変が小さくなる**。

> 補足: Claude の prose は関数のような実行時間接化ができない（FQ ツール名を直書きする）。そのため「単一シーム化」は**生成系（render 時に確定）**で実現するのが唯一の現実解であり、静的 prose は両系併記/条件分岐で吸収する。§3.3 の形寄せは「静的 prose 側の差分を減らす」ために効く。

### 5.3 allowlist（分類 b）生成を flag-aware にする

- 対象: `.claude/settings.json`（tool allow）/ `tools/org_extension_schema.json`（ロール別 allow）/ `org-setup` references。いずれも runtime の `settings/generator` が生成 SoT。
- generator に「transport flag → 公開 tool 名集合」の分岐を足す。renga 時は `mcp__renga-peers__*` 14、broker 時は `mcp__org-broker__*`（role tier に応じて worker/curator=4・dispatcher/secretary=4+pane操作）。
- role tier は broker 側が**構造的に**遮断する（worker token は pane 操作が tools/list に出ず `[tool_forbidden]`）ため、allowlist は二重防御の片側。renga 時の「全ロール同一 surface を allowlist で絞る」モデルより安全側。

### 5.4 pin bump

- `pyproject.toml` / `requirements.txt` の `claude-org-runtime` pin を broker 同梱版へ bump（`>=0.1.9,<0.2` の範囲内 minor、または範囲を `<0.3` に広げる判断）。両ファイルは意図的に同期（pyproject コメント参照）。
- pin bump 自体は broker を**有効化しない**（flag 既定 renga）。コードを ja の依存ツリーに載せるだけ。有効化は §5.1 の flag。

### 5.5 併存・切戻し（opt-in / rollback）

- **併存**: renga 経路のコード・prose を削除しない。broker は加算。
- **切戻し**: `transport=renga` への flag 戻しは「次に spawn される pane」を renga に向けるだけで、**実行中の broker-spawned ペインは即座には復帰しない**（`--mcp-config` / `--allowedTools` / pull 前提の prose を抱えたまま）。完全な切戻しの完了条件は次を含む: (1) flag 戻し、(2) **settings / 生成物の再生成**（renga allowlist へ）、(3) active な broker ペインの **suspend/resume または respawn**（renga 経路で再起動）、(4) **broker daemon の停止順序**（残ペインの revoke → daemon stop）、(5) **旧 token / queue store の破棄確認**（`.state/broker/` の未読・bind が残らないこと）。Phase ごとに切戻し可能な単位で取り込む（messaging → pane control）。
- **段階導入**: renga-decoupling.md §7 の Phase 3（messaging）→ Phase 4（pane control）の順に、flag を**面単位で**段階適用できる設計が望ましい（例 messaging だけ broker・pane 操作は renga、の中間状態を許すか）。ただし帰属・配達の一貫性のため **messaging は all-or-nothing**（混在で from 帰属が割れる）。pane 操作は dispatcher/secretary に閉じるため、messaging 移行後に pane 操作を後追いする 2 段が安全。

### 5.6 向け替え規模（呼出主体別）

renga-decoupling.md §3.1 の棚卸しから、配線替え規模は次のとおり集中している:

- **worker / curator**: 必要面は messaging 4 ツール（send_message + list_peers + check_messages 相当 + set_summary）のみ。pane 操作を一切呼ばない。→ **大多数の呼出箇所は 4 ツールのプレフィックス差し替え + 受信モデル prose のみ**で済む。
- **dispatcher / secretary**: pane 操作（spawn/close/inspect/send_keys/poll_events/list_panes/set_pane_identity）はこの 2 ロールに集中。→ pane control 移行の影響範囲はこの 2 ロールの prose に閉じる。

---

## 6. (d) 設計判断: イベント取得 — tmux control mode vs 差分 reconcile

### 6.1 論点

現行 broker は `poll_events` を **`list_panes` 差分 reconcile で合成**する（backend 横断・実証済）。一方 **tmux control mode（`tmux -CC`）は `%window-add` / `%window-close` / `%layout-change` 等を push する**ため、tmux 単体なら差分合成は不要になりうる。「イベント取得を control mode ベースに切り替えるか、差分 reconcile を維持するか」が論点。

### 6.2 選択肢

| 選択肢 | 内容 | 長所 | 短所 |
|---|---|---|---|
| **A. 差分 reconcile 維持（現状）** | `list_panes` 差分から `pane_started`/`pane_exited`/`events_dropped` を合成 | backend 横断（tmux/WezTerm/将来 Zellij/screen で同一コード）・実証済（Phase 4/5 GO）・exactly-once emit 担保済・Set D Q9 の best-effort + reconcile に整合 | ポーリング遅延（dispatcher 3 分 cadence では実害小だが理論上 push より遅い） |
| **B. tmux control mode 主軸** | `tmux -CC` の push 通知を解析して event 化 | 低遅延・tmux native のライフサイクル通知 | **tmux 専用**（WezTerm/Zellij/screen に同等 push が無い → backend ごとに別実装が要る）・control mode は対話モデル全体を変える重い protocol（control client ライフサイクル・通知ストリーム解析・新しい故障面）・併存（人間端末は renga）で制御クライアント管理が複雑化 |
| **C. 差分 reconcile を正準 + tmux hooks/control mode を任意 accelerator** | reconcile を正しさの SoT に据え、tmux では hooks（`pane-died` 等）や control mode 通知を**同じ event ring に流す低遅延の補助**として任意追加 | A の移植性 + tmux での低遅延・正しさは reconcile が担保（accelerator 故障時も degrade で正しい）・`agent_ready`（renga-decoupling.md §4.6）が list_peers poll の補助である構図と同型 | accelerator 実装分の複雑性（ただし任意・後追い可） |

### 6.3 推奨 — **C（ただし accelerator は defer）**

**差分 reconcile を backend 横断の正準インフラとして維持する**。理由:

1. **移植性が load-bearing**: broker の価値は backend 非依存（renga 故障時 WezTerm/tmux に縮退、POSIX=tmux / Windows=WezTerm）。WezTerm にネイティブ push が無い以上、差分 reconcile は**どの backend でも要る共通基盤**であり、これを正準から外せない。control mode を主軸にすると WezTerm 用に結局 reconcile を併存させ、二系統を抱える。
2. **Set D Q9 が best-effort + reconcile を許容済**: ポーリング合成は契約違反ではない。dispatcher 監視は 3 分 cadence で、reconcile の取りこぼし回復（Phase 4 AC-4-cadence GO）が正しさを担保する。**低遅延は現状の正しさ要件ではない**。
3. **control mode は blast radius が大きい**: `-CC` は端末多重化の対話モデル全体に関与し（renga 併存下では特に）新しい故障面・実装重量を持ち込む。完動ゲート GO 済の reconcile を置き換えるリスクに見合わない。
4. **accelerator は YAGNI まで defer**: tmux hooks ベースの低遅延補助は「**同じ event ring に流す任意経路**」として後から足せる（reconcile が正しさを担保するので accelerator 故障は degrade で済む）。3 分 cadence の監視遅延が実運用で不足と判明した時点で初めて着手する。Issue 分解では独立・低優先の spike 課題に置く（[§8](#8-e-issue-分解案) Issue F）。

> 結論を一言で: **正しさ = 差分 reconcile（全 backend 共通・維持）。低遅延 = tmux hooks accelerator（任意・defer）。control mode 主軸（選択肢 B）は採らない。**

---

## 7. 将来整合: anthropics/claude-code#26572（CustomPaneBackend）

prior-art 調査の結論（renga-decoupling.md 参考）どおり、本 backend 非依存 broker パターンの**既知の**近接事例は未実装 feature request #26572（CustomPaneBackend、7 オペ spawn_agent/write/capture/kill/list/get_self_id/push context_exited）で、our adapter プリミティブとほぼ 1:1（本件は前段 renga-decoupling.md の調査時点の参照であり、本レビューでの再確認は未実施 ＝ 設計本体には影響しない補足）。**将来 #26572 が ship したら our `TerminalAdapter` Protocol（[§4.2](#42-パッケージ境界)）の契約をそちらへ寄せられる**よう、adapter 面を 7 オペ相当に保ち、broker ↔ adapter の境界を薄く保つ（抽出時に adapter Protocol を肥大化させない）。本書では方針の明記に留め、追従実装はスコープ外。

---

## 8. (e) Issue 分解案

次段を、切戻し可能・レビュー可能な単位に分解する。依存は `A→B→{C,D}→E→G`、F は独立。

| Issue | 主題 | スコープ | 依存 | 完了基準（要点） |
|---|---|---|---|---|
| **A. terminal 抽出** | `spike/*adapter*` → `claude_org_runtime/terminal/` | adapter Protocol / tmux / wezterm / classify / key 語彙を runtime へ移設。テスト移設。 | — | runtime のテストが green。ja 無改変。 |
| **B. broker 抽出** | `spike/broker.py` → `claude_org_runtime/broker/` | server/store/tokens/surface に分割。queue 書込を `.state/broker/` 化。choose_split 再利用。daemon CLI entry。**runtime リリース（paired ja sync）**。**この段では ja から未使用（runtime 内部テストのみ）**。`.state/broker/` の **Set C amendment はこの段に前倒し**（書くコードを release する時点で台帳に載せる）。 | A | broker 起動・委譲サイクルが runtime パッケージ上で green。SemVer 加算。ja の依存ツリーに載るが flag 既定 renga で不活性。 |
| **C. renga 互換 surface** | broker surface を renga と同名・同形に寄せる | `spawn_claude_pane` 構造化ビルダー（[§3.3-1](#33-互換性を上げる設計推奨配線替え量を最小化する寄せ方)）/ target の name・`'focused'` 解決（§3.3-2）/ generic `spawn_pane`（§3.3-3）/ **`cwd` field parity（§3.3-4、必須）**/ `set_pane_identity` null クリア（§3.3-5）。spawn_codex/new_tab/focus は initial surface 除外で確定（§3.1）。 | B | renga golden shape との対応テスト green。drop-in 形差ゼロ。`cwd` 含む Set D 出力面の parity。 |
| **D. ja 統合シーム** | flag + 生成系シーム + pin bump | `ORG_TRANSPORT` env flag（§5.1）/ **runtime に transport surface descriptor を新設**（§5.2 (i)）/ `settings.generator` + ja 側生成器（`gen_delegate_payload.py`・worker_brief）を descriptor 駆動に（§5.2 (i)・§5.3）/ runtime pin bump（§5.4）。**両生成器出力 == descriptor の golden test**。**既定 renga・挙動不変**。 | B, C | flag=renga で現行と bit 等価。flag=broker で全生成物が broker 面を指す。golden test green。 |
| **E. ja prose + 契約改訂** | 分類 (a) prose + 契約 | 受信モデル/spawn 儀式/エラー分岐の prose（§5.2 (ii)）。契約改訂: Set D Surface 1/2/3/4/5 + Surface 8（broker auth&delivery）+ non-goals §12（host-local 例外）。**Set C の `.state/broker/` 改訂は B に前倒し済**（E では `cwd`/`receive_mode`/`kind` の Set D 出力面 amendment と、永続 transport config を採る場合のみ Set C 追加を扱う）。 | D | 契約改訂 PR 批准。両系併記 prose がレビュー通過。 |
| **F. event accelerator（任意・低優先）** | tmux hooks 低遅延補助 | 差分 reconcile を正準に据えたまま、同 event ring に tmux hooks を流す spike（[§6.3](#63-推奨--cただし-accelerator-は-defer)）。**3 分 cadence の遅延が実運用で不足と判明した時のみ着手**。 | B（独立） | hooks 経路の遅延改善を実測。reconcile 故障時 degrade を確認。 |
| **G. ja dogfood（broker 有効化）** | flag=broker で本番 ja を 1 サイクル | messaging → pane control の段階適用（§5.5）。課金中立 attestation（対話 TUI・実 argv）。**切戻しドリル（§5.5 の 5 完了条件: flag 戻し / 生成物再生成 / active ペイン respawn / daemon 停止順序 / token・queue 破棄確認）**。 | E | flag=broker で委譲サイクル完走 + 5 条件の切戻し確認。WezTerm 実機 AC は既存 Issue #9。 |

**段階適用の指針**（§5.5）: messaging は all-or-nothing（D の messaging 面 → G messaging）。pane 操作は dispatcher/secretary に閉じるため後追い（D の pane 面 → G pane control）。各段で `transport=renga` 切戻しが効くことを完了基準に含める。

---

## 9. 確認したい設計判断点（窓口/人間へ）

本書の方針は固めたが、以下は自己判断せず確認したい（CLAUDE.md「設計判断に迷う点は窓口へ」）:

1. **成果物形態**: 本書（新規 `ja-migration-plan.md`）+ `renga-decoupling.md` に短い次段ポインタ追記、で良いか（renga-decoupling.md は完動ゲートで最終化済のため、追記は最小ポインタに留める想定）。
2. **MCP サーバー名**: 併存制約により broker は `org-broker`（renga と別名）で確定 ＝ FQ ツール名は必ず変わる（[§3.4](#34-重要結論--併存制約により-fq-ツール名は必ず変わる)）。この帰結（drop-in は形レベル・FQ 名は機械置換）で合意して良いか。
3. **spawn_codex_pane / new_tab / focus_pane のスコープ**: 初期 broker surface から除外で良いか（codex design review で **SoT 照合により裏付け済**: `tools/check_renga_compat.py` の required 14 ・各ロール allowlist のいずれにも `spawn_codex_pane` は無く、new_tab/focus は人間補助で Set D 非必須。[§3.1](#31-ツール対応表全数)）。残る確認は「ja に codex を peer pane として spawn する運用が将来要るか」のみ。
4. **(d) の推奨**: 差分 reconcile を正準維持 + tmux hooks accelerator は defer（[§6.3](#63-推奨--cただし-accelerator-は-defer)）で良いか。
5. **flag の粒度**: messaging all-or-nothing + pane 操作後追いの 2 段（§5.5）で良いか。面単位のさらに細かい中間状態は許さない方針で良いか。

---

## 改訂履歴

- 2026-06-10: 初版（design only。ja-migration-extraction-design 委譲タスクの成果物。(a) renga 互換性調査+gap / (b) runtime 抽出 / (c) ja 統合シーム / (d) control mode vs 差分 reconcile 判断 / (e) Issue 分解を収録）。codex design review 1 周（gpt-5.5、Blocker 0 / Major 6 / Minor 2 / Nit 1。総評「重大な設計破綻なし、§6 推奨は妥当」）を反映: tool 数 15→**14**（`spawn_codex_pane` を required 外に。SoT=`tools/check_renga_compat.py`）/ `cwd` を field parity 必須に格上げ / transport flag を初期 env 限定（Set C 改訂回避）/ **transport surface descriptor** 新設で複数生成器の単一 SoT 化 / 切戻し完了条件を 5 項目に具体化 / Set C `.state/broker/` 改訂を Issue B に前倒し / §3.4・§7 の断定を緩和。
