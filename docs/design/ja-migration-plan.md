# ja 移行方針 — broker/adapter の claude-org-runtime 抽出と renga 互換 surface 差し替え

> ステータス: **design only / 実装なし**。本ドキュメントは Epic #6（renga 依存解消 / Plan B）の**次段**の設計であり、コード変更・本番 ja への適用は一切含まない。
> **配送モデル方向反転（2026-06-13、Issue #18）**: メッセージ配送を **push 一次（`claude/channel`）+ pull フォールバック** に再設計した。これは本書 §3.2(1) / §5.2(ii) / renga-decoupling §4.6・§5 が想定した「全エージェント pull 化（broker は pull）」の**一次/フォールバックを反転**するもの（pull は廃止せずフォールバック層に降格）。spawn 儀式は **dev-channel injection + 3-3b 承認の再導入**（`--mcp-config`-only への移行は撤回）に戻る。挙動層・所有境界・契約改訂の一次設計は **[`broker-native-roles.md`](broker-native-roles.md) §9** が SoT。本書側の反映は §3.2(1) / §5.2(ii) 注記と §8 Issue 表（A=R3 channel sidecar / B=R4 daemon delivery lifecycle / D=D2 receive_mode=push / E=P8・P9・S3 / G=K1 spike + 3-3b 再導入）。**renga 不変性・切戻し安全は維持**（broker 枝・加算・flag-gated、renga は自前 dev-channel/push 保持）。
> **前提更新（2026-06-11、完全移行へ差し替え）**: 旧 IME 制約（窓口は renga 継続）の撤回（renga-decoupling.md §1.2）に伴い、本書の移行方針を「**輸送層だけ broker・窓口は renga 継続**」から「**窓口を含む全ペインが pure backend（tmux/WezTerm）で renga-free に起動する完全移行。renga は opt-in fallback として任意残置**」へ改訂した。最終形の既定 backend は broker（pure backend）に反転し、renga は任意選択（破壊最小・切戻し可、廃止しない）。本改訂で更新した節: §1 制約 2/3、§2 全体像、§3.4 併存理由、§4.5 RengaAdapter、§5.1 flag 既定、§5.5 併存、§8 Issue D/G、§9 前提変更注記。**既存 Issue の再スコープ推奨は新設 [§10](#10-既存-issue-の再スコープ推奨新前提-design-only)**（design-only = 推奨のみ、実 issue 編集は人間ゲート後）。
> 位置付け: [`docs/design/renga-decoupling.md`](./renga-decoupling.md)（Plan B 設計 SoT、完動ゲート = GO + 2026-06-11 前提変更まで反映済）の続編。前段は「フォークで broker + adapter が成立すること」を実証した（[`spike/RESULTS.md`](../../spike/RESULTS.md) Phase 1〜5）。本段は「その成果を本番 ja に**どう移すか**」の移行方針を確定する。
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

**前提制約**（renga-decoupling.md §1 から継承。2026-06-11 に #2 撤回・#3 改訂を反映）:

1. **課金制約（維持）**: 全エージェントは対話型 TUI セッション（ヘッドレス不可）。pure backend へ完全移行しても各エージェントは対話 TUI のまま broker MCP を consume する。
2. **~~IME 制約~~（撤回、2026-06-11）**: 旧制約「人間が日本語入力する端末（窓口ペイン）は renga を継続使用」は**経験的に覆った**ため撤回された（renga-decoupling.md §1.2。スピナー自己再描画 × IME の backend parity スパイク [`spike/ime-parity/`](../../spike/ime-parity/) + 実 Claude 実走でユーザーが日本語 IME 無傷を確認 2026-06-11（描画層）、broker ナッジ × IME 変換中の手動 AC GO 2026-06-08（輸送層）。**IME 非阻害は確認済**）。→ **窓口を含む全ペインが pure backend で renga-free に運用できる**。「renga と broker は併存し窓口だけ renga 継続」という旧・最重要前提は破棄した。renga はもはや**必須前提ではなく opt-in fallback**（後述）。
3. **採用方針 = 完全移行（renga は opt-in fallback、改訂）**: 輸送層だけでなく**人間入力を含む全ペインの端末 backend** を broker（tmux/WezTerm）へ移行し、**renga 無しで組織が起動・完結する**ことを既定とする。renga は「必須前提」から「**ユーザーが任意に選べる opt-in fallback**（pure backend が不調 / 未対応な環境の切戻し先・renga を使いたい人向け）」へ降格。**renga の廃止が目的ではない**（コード・prose は残す・破壊最小・切戻し可）。これは「破壊的にしない opt-in 追加」を**逆向き**に適用したもの: 旧設計は「broker を opt-in 追加」だったが、完全移行後の最終形は「pure backend が既定・renga が opt-in」になる（移行の安全装置として切戻しは常に有効に残す。[§5.1](#51-backend-選択-flag) / [§5.5](#55-併存切戻しopt-in--rollback)）。

---

## 2. 移行の全体像

```
  現状 (ja, 実装・運用中)              次段 (本書の設計対象 / 完全移行)
  ┌─────────────────────┐          ┌──────────────────────────────┐
  │ claude-org-ja        │          │ claude-org-ja                │
  │  prose/skills →      │          │  prose/skills → transport flag │
  │   mcp__renga-peers__*│  ──────▶ │   ├ broker 経路 (既定/全ペイン)  │
  │                      │          │   └ renga 経路 (opt-in fallback) │
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

移行は「**runtime を太らせて（抽出）→ ja は pin で consume（向け替え）→ flag で段階適用（切戻し可）**」の 3 段で、各段が前段に依存する。**最終形は broker（pure backend）が既定・全ペイン renga-free、renga は opt-in fallback**（制約 2 撤回・制約 3 完全移行）。renga 経路のコード・prose は削除せず**任意残置**する（切戻しの安全装置として常時有効に保つ）。ロールアウトの安全のため、移行途中は flag で renga 既定に留め、検証通過後に既定を broker へ反転させる段階を踏む（[§5.1](#51-backend-選択-flag)）。

---

## 3. (a) broker MCP surface と renga-peers の drop-in 互換性調査 + gap

ja 側（CLAUDE.md / skills / dispatcher references）は renga 固有ツールを **MCP の完全修飾名**（`mcp__renga-peers__<tool>`）で直接呼ぶ。配線替え量を最小化するには、broker surface をこれらと同名・同形に寄せるのが基本方針となる。本節は現 broker surface（[`spike/broker.py`](../../spike/broker.py)）と renga-peers の **required 14 ツール**を全数照合した対応表と gap である。

> **required surface の SoT**: ja が要求する renga-peers ツールは [`tools/check_renga_compat.py`](../../tools/check_renga_compat.py) の `REQUIRED_MCP_TOOLS`（renga 0.18.0、**ちょうど 14**）が正本。`spawn_codex_pane` は**この required surface には含まれない**（renga compat checker・各ロール allowlist のいずれにも無い）。
> **ただし broker の初期 surface には `spawn_codex_pane` を含める**（人間判断 2026-06-10）。required 外であることを承知の上で、**将来 codex を peer pane として spawn する運用に最初から備える**ため、初期スコープに載せる。
> **初期 surface の正確な内訳（数の整理）**: renga required 14 には `new_tab` / `focus_pane` も含まれるが、これらは人間補助で Set D 非必須のため broker は初期 surface から**除外**する（renga-decoupling.md §4.2 と整合）。したがって broker 初期 surface = **required 14 のうち移植する 12 面**（`new_tab`/`focus_pane` を除く）+ **`spawn_codex_pane`（required 外・人間判断）= 計 13 面**。移植する 12 面は renga と同名・同形に寄せる（drop-in 形差ゼロの対象はこの 12 面 + codex builder であり、除外 2 面は対象外）。

### 3.1 ツール対応表（全数）

| renga-peers ツール | broker 現状 | 引数形 | 戻り形 | drop-in 区分 |
|---|---|---|---|---|
| `send_message(to_id, message)` | `send_message` | **同一** | 同一（`{ok, delivered_to}`） | ✅ 完全 |
| `set_summary(summary)` | `set_summary` | **同一** | 同一（`{ok}`） | ✅ 完全 |
| `check_messages()` | `check_messages` | **同一** | 同一（`{messages:[…]}`） | ⚠️ 名/形同一・**意味論差**（push→pull） |
| `list_peers()` | `list_peers` | **同一** | `id/name/role/summary`（renga は + `cwd/kind/receive_mode`） | ⚠️ フィールド欠落（小） |
| `list_panes()` | `list_panes` | **同一** | `id/name/role/focused/x/y/w/h(/cursor)`（renga は + `cwd/kind/receive_mode`） | ⚠️ フィールド欠落（小） |
| `inspect_pane(target, lines, format, include_cursor)` | `inspect_pane` | `format` **欠落**、他同一 | `text/state(/cursor)`（renga は `format=grid` / `structuredContent`） | ⚠️ `format=grid` 欠落 + addressing |
| `send_keys(target, text, keys, enter)` | `send_keys` | 名/形同一・**vocab 宣言は一致※だが runtime adapter の実装が部分的**（Escape 不可、[broker-native-roles.md](broker-native-roles.md) §3.5） | 同一（`{ok}` / 未実装キーは `[key_unsupported]`） | ⚠️ addressing + **adapter key 語彙の実装欠落（Escape/Shift+Tab。R2）** |
| `poll_events(since, timeout_ms, types)` | `poll_events` | **同一** | 同一（`next_since` + `events[]`、event `id`=handle） | ✅ ほぼ完全 |
| `close_pane(target)` | `close_pane` | **同一** | `{ok, closed}` | ⚠️ addressing のみ |
| `set_pane_identity(target, name, role)` | `set_pane_identity` | three-state の **null クリア欠落** | 同一 | ⚠️ null クリア欠落 + addressing（+ role は表示専用に再定義※） |
| `spawn_claude_pane(direction, target, name, role, model, permission_mode, args, cwd)` | `spawn_agent(agent_id, name, role, argv, cwd, target, direction)` | **大幅相違** | `{ok, handle, direction}` | ❌ rename + 形相違 |
| `spawn_pane(command, …)`（generic） | **無し** | — | — | ❌ 欠落 |
| `spawn_codex_pane(args, …)` | **無し**（初期 surface に**新設**） | — | — | ❌ 欠落 → **初期スコープで追加**（required 外・人間判断） |
| `new_tab(…)` | **無し** | — | — | ❌ 欠落（設計上の意図的除外） |
| `focus_pane(target)` | **無し** | — | — | ❌ 欠落（設計上の意図的除外） |

上表の 14 行（`spawn_codex_pane` を除く）が renga の required surface（`tools/check_renga_compat.py` `REQUIRED_MCP_TOOLS` と一致。**`new_tab`/`focus_pane` もこの 14 に含まれる**点に注意）。broker の**初期 surface = この 14 のうち `new_tab`/`focus_pane` を除く 12 面 + `spawn_codex_pane` = 13 面**（人間判断 2026-06-10、[§9](#9-設計判断点人間確認結果) 確認点 3）。`spawn_codex_pane` の追加で将来の codex peer pane spawn に備える。`new_tab`/`focus_pane` は人間補助で Set D 非必須のため初期除外（必要時に追加）。

※ `send_keys` のキー語彙は **surface 宣言レベル**（`SEND_KEYS_VOCAB`/`normalize_key`）では照合済みで一致（renga: Enter/Return, Tab, Shift+Tab/BackTab, Esc/Escape, Backspace, Delete/Del, 矢印, Home/End, PageUp/PageDown, Space, Ctrl+A-Z ＝ broker `SEND_KEYS_VOCAB` と同一。未知キーは両者 `-32602 invalid-params`）。**ただし dogfood（2026-06-13、窓口観測）で runtime broker の tmux adapter は実効的に Enter/Ctrl+C/literal のみ翻訳し、`Escape`/`Shift+Tab` は `[key_unsupported]` を返す**ことが判明した（full 語彙翻訳は Phase 4 / full backend adapter スコープ）。= parity は **vocab 宣言レベルでは成立するが adapter 実装レベルでは未達**。この実装欠落が org-delegate の Escape ベース介入手順を broker で実行不能にする（[broker-native-roles.md](broker-native-roles.md) §3.5 介入層 defect）。閉鎖は **R2**（adapter key 語彙に Escape/Shift+Tab を実装、Issue A+C）。
※ `set_pane_identity` の `role` を broker は **表示専用ラベル**に再定義し、権限 tier は不変 `auth_role` のみで決める（[`spike/broker.py`](../../spike/broker.py) の codex Blocker 対応）。これは renga にない**意図的なセキュリティ強化**であり gap ではなく improvement。

### 3.2 gap の 4 分類

1. **意味論差（prose 書き換えで追随）**: `check_messages` の受信モデルが push（renga の in-band channel 注入）→ pull（broker は本文をキュー滞留させ `check_messages` で取得）に変わる。ツール名・形は同一だが、worker brief 等「`<channel>` が届いたら ack」の prose が **broker では「役割 cadence で能動 poll（`check_messages`）」** に変わる（renga-decoupling.md §5 非互換 1）。**注: ナッジは idle セッションを起こさないため「ナッジを見たら」という push 形の文言は採らない（pull-first cadence の役割別設計は [`broker-native-roles.md`](broker-native-roles.md) §2/§3 を一次参照。dogfood defect 1-3 の根因是正）**。
   - **方向反転（2026-06-13、Issue #18）**: 上記「broker は pull」は **#18 で再反転**した。broker も **push 一次**（per-session の `claude/channel` channel sidecar が daemon queue を ~1s で claim→push してセッションへ in-band 注入）とし、**pull（`check_messages` の役割 cadence）はフォールバック層**に降格する（push mode 失効時・channel 非対応エージェント向け）。よって worker brief は **「push 一次で受ける（idle でも起きる）/ push 失効時は役割 cadence で poll」** へ。`check_messages` のツール名・形は不変、意味論は「retroactive な claim-respecting drain（フォールバック）」へ精緻化。一次設計は [`broker-native-roles.md`](broker-native-roles.md) §9（特に §9.3 配送ライフサイクル・§9.6 読み替え）。
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
6. **`spawn_codex_pane` を初期 surface に新設**（人間判断 2026-06-10）。renga と同シグネチャ（`spawn_codex_pane(direction, target?, name?, role?, args?, cwd?)`）で公開し、broker 内部で **codex の対話 TUI argv をビルダーで組む**（spawn_claude_pane と同型のビルダー方式）。**課金中立 guard の codex 版が要る**: claude の `is_interactive_claude_argv`（allowlist / default-deny）が claude TUI に限定するのと同様に、codex 版も **allowlist（default-deny）** とする。codex には非対話サブコマンドが `exec` 以外にも複数ある（`review`＝非対話コードレビュー、`mcp-server` / `app-server` / `exec-server`、`apply` / `sandbox` / `completion` 等）ため、**`codex exec` 単体の blacklist では塞ぎきれない**（これは §7.6 の claude allowlist 化と全く同じ教訓 — blacklist 後追いは「flag 後サブコマンド」「`--` バイパス」を取り逃す）。設計: **`argv[0]` basename == `codex` かつ、以降は対話 TUI 用 allowlist の flag/value のみを許可。`exec` / `review` / `*-server` / `apply` / `sandbox` / `completion` / 未知サブコマンド / bare positional / `--` は一律拒否**（`codex exec` は代表例）。これは §7.6（renga-decoupling.md）の課金中立 argv 保守契約を codex へ拡張する設計判断であり、保守契約（新しい正規の対話 flag は allowlist 拡張要・headless 系は決して入れない）も継承する。broker token を持つ org agent としての codex pane（ナッジ + `check_messages` で受信）も対話 TUI に構造的に限定する。ops tier（dispatcher/secretary）のみが呼べる点は他 spawn 系と同じ。

### 3.4 重要結論 — 併存設計のため broker は別名（`org-broker`）を採り、FQ 名は書き換わる

renga と broker は**併存しうる**（制約 2 撤回後も、制約 3 により renga は opt-in fallback として任意残置 = pure backend が不調 / 未対応な環境の切戻し先・renga を使いたい人向け・移行期の切戻し先。完全移行後も renga 経路は削除しない）。MCP サーバーが同一マシン・同一セッションで両方見える状態になりうるため、**本設計では併存・切戻し安全性を優先して broker の MCP サーバー名を `renga-peers` と別の `org-broker` にする**（同名にすると同時登録時に衝突する）。理論上は per-session `--strict-mcp-config` で broker-only セッションに `renga-peers` 別名を切る余地もあるが、縮退運転（renga と broker の同居）と段階移行（messaging だけ broker 等）の安全性を取り、別名で固定する。したがって完全修飾ツール名は `mcp__renga-peers__send_message` → `mcp__org-broker__send_message` のように**プレフィックスが書き換わる**。

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
│   ├── tmux.py               #   TmuxAdapter   (POSIX 正準。初期抽出対象)
│   ├── wezterm.py            #   WezTermAdapter (Windows 正準。初期抽出対象)
│   └── renga.py              #   (将来・任意) RengaAdapter。opt-in fallback 用。初期実装スコープ外 (§4.5)
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
| （新規・**初期スコープ外**） | `terminal/renga.py` | renga を backend にする adapter。**opt-in fallback 用の任意オプションであり初期抽出（Issue A）には含めない**。必要時に別 Issue 化（§4.5） |
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
- **RengaAdapter（新規）の位置付け**: renga を「broker の一 backend」として駆動する adapter。これにより「broker 経路だが端末は renga」という構成（renga を opt-in fallback として broker 配下で使う = pure backend が不調 / 未対応な環境の切戻し / renga を使いたい人向けを adapter 差し替えで表現）が可能になる。**ただし制約 2 撤回により「窓口の IME のために renga が要る」という必然性は消えた**ため、これは必須経路ではなく**任意の互換オプション**である。実装優先度は低い（tmux/WezTerm で完動ゲート GO 済）。設計上の余地として置くだけで、**初期抽出（[§8 Issue A](#8-e-issue-分解案)）には含めず**、初期実装は tmux/WezTerm に限る。RengaAdapter が要るのは renga を broker 配下の backend として使いたい場合に限られ、必要になった時点で**別 Issue 化**する（初期スコープに renga 再導入を読ませない）。
- **settings.generator**: allowlist 生成は既存 generator に flag-aware の分岐を足す（[§5.3](#53-allowlist-分類-b-生成を-flag-aware-にする)）。

### 4.6 daemon / CLI entry

org-start が broker daemon を起動できるよう `broker/cli.py` に entry を足し、runtime の `cli.py` から `python -m claude_org_runtime.broker` 等で起動可能にする。死活・再起動の runbook は Phase の取り込み時に用意（renga-decoupling.md §8「broker の単一障害点化」リスク）。spike の `if __name__ == "__main__"` standalone 起動はこの entry の原型。

### 4.7 versioning と paired ja sync

- broker surface は SemVer 義務（Set D Surface 7 継承）。broker を追加する runtime リリースは**加算的**（既存 API 不変）であり、ja の pin `<0.2` 範囲内の minor bump で consume 可能（破壊しない）。
- **runtime リリースは ja 側 expectation 同期とペアで行う**（`runtime-release-with-paired-ja-sync` skill 該当）。DEFAULT_NOTIFY / classifier vocab / org_extension_schema / attention テンプレが変わる場合に CI cascade を予防する。本抽出で `settings/generator` を flag-aware 化する（§5.3）ため、この skill の発動条件に該当しうる。Issue 分解（§8）で runtime リリース課題に paired sync を明記する。

---

## 5. (c) ja 統合シーム最小化設計

破壊最小・切戻し可（Epic #6 制約）を保つため、ja 側の改変を「**1 つの flag + 1 つの生成系シーム**」に集約する（flag の既定値は移行期 renga → 完全移行後 broker に反転する。opt-in の向きの詳細は [§5.1](#51-backend-選択-flag)）。

### 5.1 backend 選択 flag

- **flag の所在**: 初期は**環境変数 `ORG_TRANSPORT`（`renga` | `broker`）に限る**。org-start / spawn-flow が起動時に 1 度読む。`.state/org-config.json` のような永続ファイル化は、**それ自体が Set C inventory への追加改訂対象になる**ため、env で済む初期段階では持ち込まず、永続設定が必要になった時点で別 Issue（Set C 改訂を伴う）に分離する。env のみなら Set C 改訂を増やさずに済む（非破壊・最小）。
- **既定の扱い（完全移行に伴い 2 段階、2026-06-11 改訂）**: flag は 2 値だが「どちらが既定か」は移行の進捗で反転する。
  - **移行期（ロールアウト中）の既定 = `renga`**（無設定時は現行どおり挙動不変 ＝ **非破壊**。`broker` を明示 opt-in で段階適用し dogfood）。
  - **完全移行後の既定 = `broker`**（pure backend / 全ペイン renga-free が組織の標準起動。**`renga` が opt-in fallback** に反転）。既定反転は dogfood ゲート（[§8 Issue G](#8-e-issue-分解案)）通過を条件とする人間判断。
  - 既定がどちらであっても **`renga` 経路は削除せず常時有効**（切戻しの安全装置。[§5.5](#55-併存切戻しopt-in--rollback)）。
- flag は **org 全体で 1 値**（worker ごとに混在させない。混在は帰属・配達の整合を壊す）。**制約 2 撤回により、窓口を含む全ペインが同一 flag に従う**（旧設計の「人間入力の窓口だけ flag に依らず renga 継続 / 端末=renga・輸送=broker」という二重構造は破棄。`broker` 時は窓口も tmux/WezTerm の pure backend 端末上で動作し、IME 非阻害が確認済のため renga 端末を必要としない）。

### 5.2 transport プレフィックスを生成系の単一シームに閉じる

ja の renga ツール参照は (i) **生成されるもの**（`tools/gen_delegate_payload.py` / worker_brief テンプレート / `org_extension_schema.json` / settings、＝ runtime の `settings/generator` 由来）と (ii) **静的 prose**（CLAUDE.md / skills / dispatcher references）に分かれる。

- **(i) 生成系**: transport プレフィックス（`renga-peers` / `org-broker`）と spawn 注入 flag（`--dangerously-load-development-channels server:renga-peers` / `--mcp-config <broker>`）を**テンプレート変数**にし、flag から render する。worker brief・delegate payload・allowlist が **flag 一つで両系に振り分く**。配線替えの主シーム。
  - **所有境界の注意**: 生成器は 1 つではない。`settings/generator`（allowlist。runtime 側）と `tools/gen_delegate_payload.py` / worker_brief テンプレート（delegate payload・brief。**ja 側資産**）が別々に同じ transport prefix / tool set を必要とする。これを各所にハードコードすると drift する。**runtime に小さな共有データ（transport surface descriptor: flag → {server 名, 注入 flag, ロール別 tool 名集合}）を 1 つ置き、runtime の settings generator と ja 側生成器の双方がこの descriptor を読む**設計にする（単一 SoT）。descriptor は加算的な runtime API で、ja は pin consume する。両生成器の出力が descriptor と一致する golden test を Issue D に置く。
- **(ii) 静的 prose**: 受信モデル・spawn 儀式（dev-channel 承認 → folder trust prompt の機械承認）・エラー分岐（`token_*` / `adapter_unavailable` 追加）は**意味が変わる**ため、両系を併記するか flag 条件付き記述にする。これは renga-decoupling.md 分類 (a) の prose 書き換えそのもの。**論理差を §3.3 で最小化しておくほど、この prose 改変が小さくなる**。
  - **受信モデルの cadence / 役割セマンティクスの一次設計は [`broker-native-roles.md`](broker-native-roles.md) を一次参照**（機械置換で埋まらない挙動層。Refs #16/#18）。本節 (ii) は「両系併記する」宣言、当該 doc は「両系併記する *中身*」の SoT。
  - **方向反転（2026-06-13、Issue #18）**: broker 枝の受信モデルは **push 一次（`claude/channel`）+ pull フォールバック**（[`broker-native-roles.md`](broker-native-roles.md) §9）。spawn 儀式は **`--mcp-config`（daemon）+ dev-channel（channel sidecar `server:org-broker-channel`）の併用 + 3-3b 機械承認の*再導入***へ（`--mcp-config`-only / dev-channel 廃止案は撤回、§9.5 / S3）。`nudge_failed` 系は channel sidecar 採用で不要化（nudge 撤回、§9.6）。**dev-channel flag 注入は broker 枝厳格・descriptor 駆動**とし、flag=renga 再生成は第二 dev-channel を一切 emit しない（launcher argv の bit 等価を Issue D golden に追加、§9.7）。

> 補足: Claude の prose は関数のような実行時間接化ができない（FQ ツール名を直書きする）。そのため「単一シーム化」は**生成系（render 時に確定）**で実現するのが唯一の現実解であり、静的 prose は両系併記/条件分岐で吸収する。§3.3 の形寄せは「静的 prose 側の差分を減らす」ために効く。

### 5.3 allowlist（分類 b）生成を flag-aware にする

- 対象: `.claude/settings.json`（tool allow）/ `tools/org_extension_schema.json`（ロール別 allow）/ `org-setup` references。いずれも runtime の `settings/generator` が生成 SoT。
- generator に「transport flag → 公開 tool 名集合」の分岐を足す。renga 時は `mcp__renga-peers__*` 14、broker 時は `mcp__org-broker__*`（role tier に応じて worker/curator=4・dispatcher/secretary=4+pane操作）。
- role tier は broker 側が**構造的に**遮断する（worker token は pane 操作が tools/list に出ず `[tool_forbidden]`）ため、allowlist は二重防御の片側。renga 時の「全ロール同一 surface を allowlist で絞る」モデルより安全側。

### 5.4 pin bump

- `pyproject.toml` / `requirements.txt` の `claude-org-runtime` pin を broker 同梱版へ bump（`>=0.1.9,<0.2` の範囲内 minor、または範囲を `<0.3` に広げる判断）。両ファイルは意図的に同期（pyproject コメント参照）。
- pin bump 自体は broker を**有効化しない**（flag 既定 renga）。コードを ja の依存ツリーに載せるだけ。有効化は §5.1 の flag。

### 5.5 併存・切戻し（opt-in / rollback）

- **併存**: renga 経路のコード・prose を削除しない（**完全移行後も renga は opt-in fallback として任意残置**）。移行期は broker が加算、完全移行後は既定が broker・renga が opt-in に反転するが、いずれも renga 経路は生かしておく（切戻しの安全装置）。
- **切戻し**: `transport=renga` への flag 戻しは「次に spawn される pane」を renga に向けるだけで、**実行中の broker-spawned ペインは即座には復帰しない**（`--mcp-config` / `--allowedTools` / pull 前提の prose を抱えたまま）。完全な切戻しの完了条件は次を含む: (1) flag 戻し、(2) **settings / 生成物の再生成**（renga allowlist へ）、(3) active な broker ペインの **suspend/resume または respawn**（renga 経路で再起動）、(4) **broker daemon の停止順序**（残ペインの revoke → daemon stop）、(5) **旧 token / queue store の破棄確認**（`.state/broker/` の未読・bind が残らないこと）。Phase ごとに切戻し可能な単位で取り込む（messaging → pane control）。**(6)（#18 追補）per-pane channel sidecar の reap**（SIGTERM/unregister）+ **当該 agent の `delivery_mode` reset** + **delivery-scoped credential の revoke**（(3) active ペイン respawn / (4) daemon 停止順序 へ畳む。push 一次の channel sidecar は新規 live process のため、flag=renga 切戻し時に orphan 化させない。[`broker-native-roles.md`](broker-native-roles.md) §9.7）。
- **段階導入**: renga-decoupling.md §7 の Phase 3（messaging）→ Phase 4（pane control）の順に、flag を**面単位で**段階適用できる設計が望ましい（例 messaging だけ broker・pane 操作は renga、の中間状態を許すか）。ただし帰属・配達の一貫性のため **messaging は all-or-nothing**（混在で from 帰属が割れる）。pane 操作は dispatcher/secretary に閉じるため、messaging 移行後に pane 操作を後追いする 2 段が安全。**この中間状態は移行途中だけ許す「面単位の段階適用」であり、撤回された旧前提の「窓口だけ renga 継続」とは別物**（全ペインが同一 flag に従う前提は不変。最終形は全面 broker）。

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

1. **移植性が load-bearing**: broker の価値は backend 非依存（既定 = POSIX:tmux / Windows:WezTerm の pure backend + opt-in fallback の renga を同一 reconcile で扱える）。WezTerm にネイティブ push が無い以上、差分 reconcile は**どの backend でも要る共通基盤**であり、これを正準から外せない。control mode を主軸にすると WezTerm 用に結局 reconcile を併存させ、二系統を抱える。
2. **Set D Q9 が best-effort + reconcile を許容済**: ポーリング合成は契約違反ではない。dispatcher 監視は 3 分 cadence で、reconcile の取りこぼし回復（Phase 4 AC-4-cadence GO）が正しさを担保する。**低遅延は現状の正しさ要件ではない**。
3. **control mode は blast radius が大きい**: `-CC` は端末多重化の対話モデル全体に関与し（renga 併存下では特に）新しい故障面・実装重量を持ち込む。完動ゲート GO 済の reconcile を置き換えるリスクに見合わない。
4. **accelerator は YAGNI まで defer**: tmux hooks ベースの低遅延補助は「**同じ event ring に流す任意経路**」として後から足せる（reconcile が正しさを担保するので accelerator 故障は degrade で済む）。3 分 cadence の監視遅延が実運用で不足と判明した時点で初めて着手する。Issue 分解では独立・低優先の spike 課題に置く（[§8](#8-e-issue-分解案) Issue F）。

> 結論を一言で: **正しさ = 差分 reconcile（全 backend 共通・維持）。低遅延 = tmux hooks accelerator（任意・defer）。control mode 主軸（選択肢 B）は採らない。**

---

## 7. 将来整合: anthropics/claude-code#26572（CustomPaneBackend）

prior-art 調査の結論（renga-decoupling.md 参考）どおり、本 backend 非依存 broker パターンの**既知の**近接事例は未実装 feature request #26572（CustomPaneBackend、7 オペ spawn_agent/write/capture/kill/list/get_self_id/push context_exited）で、our adapter プリミティブとほぼ 1:1（本件は前段 renga-decoupling.md の調査時点の参照であり、本レビューでの再確認は未実施 ＝ 設計本体には影響しない補足）。**将来 #26572 が ship したら our `TerminalAdapter` Protocol（[§4.2](#42-パッケージ境界)）の契約をそちらへ寄せられる**よう、adapter 面を 7 オペ相当に保ち、broker ↔ adapter の境界を薄く保つ（抽出時に adapter Protocol を肥大化させない）。本書では方針の明記に留め、追従実装はスコープ外。

---

## 8. (e) Issue 分解案

次段を、切戻し可能・レビュー可能な単位に分解する。依存は `A→B→{C,D}→E→G`、F・H は独立（H は #16 由来の broker 受信挙動層 runtime、E の S2 prose 起動と協調）。

| Issue | 主題 | スコープ | 依存 | 完了基準（要点） |
|---|---|---|---|---|
| **A. terminal 抽出** | `spike/*adapter*` → `claude_org_runtime/terminal/` | adapter Protocol / tmux / wezterm / classify / key 語彙を runtime へ移設。テスト移設。**+ tmux adapter の単一セッション複数ペイン化 + attach 導線**（[`broker-native-roles.md`](broker-native-roles.md) R1 / defect 4。独立 tmux セッション per ペイン → 単一 `claude-org` セッションへ再構成し `tmux attach -t claude-org` 一発で全体可視化）。**+ key 語彙に `Escape`（+`Shift+Tab`）を追加**（[`broker-native-roles.md`](broker-native-roles.md) R2 / 介入層 defect。tmux ネイティブ `send-keys Escape`。renga の Escape 介入手順を drop-in 不変化）。 | — | runtime のテストが green。ja 無改変。**tmux adapter が単一セッション構成で attach 一発全体可視（R1）+ `send_keys(["Escape"])` が `[key_unsupported]` を返さず介入が drop-in 成立（R2）**。 |
| **B. broker 抽出** | `spike/broker.py` → `claude_org_runtime/broker/` | server/store/tokens/surface に分割。queue 書込を `.state/broker/` 化。choose_split 再利用。daemon CLI entry。**runtime リリース（paired ja sync）**。**この段では ja から未使用（runtime 内部テストのみ）**。`.state/broker/` の **Set C amendment はこの段に前倒し**（書くコードを release する時点で台帳に載せる）。 | A | broker 起動・委譲サイクルが runtime パッケージ上で green。SemVer 加算。ja の依存ツリーに載るが flag 既定 renga で不活性。 |
| **C. renga 互換 surface** | broker surface を renga と同名・同形に寄せる | `spawn_claude_pane` 構造化ビルダー（[§3.3-1](#33-互換性を上げる設計推奨配線替え量を最小化する寄せ方)）/ target の name・`'focused'` 解決（§3.3-2）/ generic `spawn_pane`（§3.3-3）/ **`cwd` field parity（§3.3-4、必須）**/ `set_pane_identity` null クリア（§3.3-5）/ **`spawn_codex_pane` 新設 + codex 課金中立ビルダー（§3.3-6、default-deny allowlist）**。初期 surface = 移植 12 面 + codex = 13 面（new_tab/focus は除外確定、§3.1）。 | B | 移植 12 面 + codex builder が renga golden shape と drop-in 形差ゼロ。`cwd` 含む Set D 出力面の parity。codex spawn が対話 TUI に構造的限定（`exec`/`review`/`*-server` 等の非対話サブコマンドを default-deny で拒否）。 |
| **D. ja 統合シーム** | flag + 生成系シーム + pin bump | `ORG_TRANSPORT` env flag（§5.1。**窓口含む全ペインが flag に従う** — 制約 2 撤回）/ **runtime に transport surface descriptor を新設**（§5.2 (i)）/ `settings.generator` + ja 側生成器（`gen_delegate_payload.py`・worker_brief）を descriptor 駆動に（§5.2 (i)・§5.3）/ runtime pin bump（§5.4）。**両生成器出力 == descriptor の golden test**。**+ descriptor に `receive_mode`/役割別 `receive_cadence` フィールドを加算**（[`broker-native-roles.md`](broker-native-roles.md) D1。受信 cadence 文言を descriptor 駆動 render し prose drift を防ぐ。`receive_mode` は backend-interface-contract §8.8 出力フィールドの上流 SoT）。**この段の既定は renga・挙動不変**（既定の broker 反転は G ゲート後）。 | B, C | flag=renga で現行と bit 等価（切戻し忠実性）。flag=broker で全生成物が broker 面を指す（窓口含む）。golden test green。**descriptor の `receive_mode` と出力フィールドが一致（D1 golden）**。 |
| **E. ja prose + 契約改訂** | 分類 (a) prose + 契約 | 受信モデル/spawn 儀式/エラー分岐の prose（§5.2 (ii)）。**受信モデル prose の中身は [`broker-native-roles.md`](broker-native-roles.md) §6 の prose 変更一覧（P1 / P2 / P3a / P3b / P4 / P5 / P6 / P7、+ S2 の prose 起動部）に分解済**（pull-first cadence の役割別反映。`.dispatcher/CLAUDE.md` の /loop 実発火＝P3b、**介入手順の broker 枝＝P7**（org-delegate L326 + renga-error-codes broker 節: Escape は R2 後 drop-in / 暫定 gated Ctrl+C）を含む。S2 の runtime 部は Issue H、R1/R2 は Issue A、D1 は Issue D が所在）。契約改訂: Set D Surface 1/2/3/4/5 + Surface 8（broker auth&delivery）+ non-goals §12（host-local 例外）。**Set C の `.state/broker/` 改訂は B に前倒し済**（E では `cwd`/`receive_mode`/`kind` の Set D 出力面 amendment と、永続 transport config を採る場合のみ Set C 追加を扱う）。 | D | 契約改訂 PR 批准。両系併記 prose がレビュー通過。**`broker-native-roles.md` §6 の prose 変更一覧（P1/P2/P3a/P3b/P4/P5/P6/P7）を反映**（P7=介入手順の broker 枝）。 |
| **F. event accelerator（任意・低優先）** | tmux hooks 低遅延補助 | 差分 reconcile を正準に据えたまま、同 event ring に tmux hooks を流す spike（[§6.3](#63-推奨--cただし-accelerator-は-defer)）。**3 分 cadence の遅延が実運用で不足と判明した時のみ着手**。 | B（独立） | hooks 経路の遅延改善を実測。reconcile 故障時 degrade を確認。 |
| **G. ja dogfood（broker 有効化 → 既定反転）** | flag=broker で本番 ja を 1 サイクル + 既定反転判断 | messaging → pane control の段階適用（§5.5）。**本番 ja 反映ゲート**: 本番 ja で**全ペイン（窓口含む）が pure backend で renga-free に org-start し委譲サイクル完走**すること（renga-decoupling.md §7.6 の完動ゲート定義を**本番 ja に適用**した段階。フォーク完動ゲート＝既 GO と区別し、本ゲートは本番反映の合格条件）。課金中立 attestation（対話 TUI・実 argv）。**切戻しドリル（§5.5 の 5 完了条件: flag 戻し / 生成物再生成 / active ペイン respawn / daemon 停止順序 / token・queue 破棄確認）**。通過後に**既定を broker へ反転する人間判断**（§5.1）。 | E | flag=broker で窓口含む全ペイン renga-free に委譲サイクル完走 + 5 条件の切戻し確認 + 既定反転の Go 判断。WezTerm 実機 AC は既存 Issue #9。 |
| **H. broker 受信 sidecar（#16 由来・runtime / #18 で nudge 撤回）** | secretary 人間不在 gap の能動通知（~~+ 任意の低遅延 nudge~~ #18 撤回） | **S2（attention watcher input 拡張）**: `attention/readers.py` に secretary broker-queue poll source を新設 + read-scope token ハンドリング（現 readers.py は state.db/pending_decisions のみ。queue read は net-new。[`broker-native-roles.md`](broker-native-roles.md) §3.2 B2 / §6.2 S2）。~~**N1（nudge accelerator、任意・低優先）**: poll 正準のまま打鍵ナッジを同 path に流す spike~~ **→ #18 で撤回**（push の正準手段が `claude/channel` channel sidecar になり nudge は wake 機構として不要。[`broker-native-roles.md`](broker-native-roles.md) §9.6/§9.9）。**本 Issue H の active item は S2 のみ**。 | B（独立。E の S2 prose 起動と協調） | S2: 人間不在中に worker 完了/DELEGATED/escalation が OS 通知される（state.db 未書込の未処理着信を被覆）。renga 時は無効化（加算）。~~N1~~: 撤回（§9.6）。 |

> **#18 追補（push 一次配送、2026-06-13）— 上表 Issue 行を *#18 後の SoT で上書き***（一次設計 = [`broker-native-roles.md`](broker-native-roles.md) §9。すべて broker 枝・加算・renga 不変）。**読み方**: 上表本体行は #16（pull-first）時点の分解であり、push 一次に必要な descriptor 更新（D2）・spawn 儀式（P8）・受信 prose（P9）・契約改訂（S3）・spike ゲート（K1）は **下記が各 Issue の完了基準を上書きする**（D は D1→D2 supersede、E は P1-P7 を「フォールバック層の cadence」へ読み替え + P8/P9/S3 を追加、H は N1 撤回）。本体行を単独で完了基準として読まないこと:
> - **Issue A（terminal 抽出）**: **+R3** = per-session **channel sidecar**（`claude_org_runtime/broker/channel_sidecar.py`、stdio MCP・`experimental{claude/channel}`・claim→push ループ）を spawn 経路に追加。完了基準に「spawn 時に channel sidecar が起動し dev-channel が機械承認される」を追加。
> - **Issue B（broker 抽出）**: **+R4** = daemon **delivery lifecycle 改修**（三状態 `UNDELIVERED→CLAIMED(lease,owner,epoch)→DELIVERED`・`/poll-claims`+`/confirm-delivered`・per-agent `delivery_mode`・**delivery-scoped token scope**・mode-epoch fencing。§9.3/§9.4）。`.state/broker/` schema 改訂を本段に含める。完了基準に「sidecar 死亡時に lease-reap で再配達され message が喪失しない」を追加。
> - **Issue D（ja 統合シーム）**: **+D2** = descriptor `receive_mode` broker 値を `poll`→**`push`**（fallback=`poll`）へ。**launcher argv（dev-channel injection の有無）を golden の bit 等価に追加**（flag=renga は第二 dev-channel を emit しない、§9.7）。
> - **Issue E（prose + 契約）**: **+P8**（spawn-flow の dev-channel sidecar load + 3-3b 承認の*再導入* prose）/ **+P9**（受信を push 一次/pull フォールバックへ・§2/§3.1 を fallback 層と明記）/ **+S3**（Set D Surface 1.2/2.1/2.3/5.1/8 の改訂*提案*: dev-channel 廃止提案の撤回・Surface 2.3 への三状態加算・delivery-scoped token。批准は人間ゲート）。
> - **K1（HARD pre-ratification spike、依存順で E より前）**: Claude Code harness が tool-less な `claude/channel` サーバーを load し idle wake させ renga と coexist するかの実測（不成立なら sidecar 同梱形へ fallback、§9.5）。**これは「批准前必須ゲート」なので、依存順では契約批准の Issue E より*前*に置く独立ゲート**（F/H と同様に独立、ただし **PASS が E（S3 契約改訂批准）と P8/P9 prose land の前提条件**）。K1 未 PASS のまま E/G を批准させない。実走自体は G の dogfood 環境を流用してよいが、**ゲート判定の所在は E より上流**。
> - **Issue G（dogfood + 既定反転）**: spawn-flow AC に **3-3b 承認の再導入**を反映。push 経路は Claude Code ≥ v2.1.80 + claude.ai login 前提（pull フォールバックは auth 非依存）。K1 ゲート（上記）通過済を G 着手の前提に含める。
> - **Issue H（受信 sidecar + nudge）**: **N1（nudge accelerator）は撤回**（push の正準手段が `claude/channel` になり nudge は wake 機構として不要、§9.6）。**S2（attention watcher 拡張）は不変で有効**（人間不在 gap の*人間*ページングは push と別軸、§3.2 B2）。

**段階適用の指針**（§5.5）: messaging は all-or-nothing（D の messaging 面 → G messaging）。pane 操作は dispatcher/secretary に閉じるため後追い（D の pane 面 → G pane control）。各段で `transport=renga` 切戻しが効くことを完了基準に含める。**#18 追補**: 切戻し完了条件（§5.5 の 5 条件）に **第 6 ステップ = per-pane channel sidecar の reap（SIGTERM/unregister）+ delivery_mode reset + delivery-scoped credential revoke** を条件 (3)/(4) へ畳む（[`broker-native-roles.md`](broker-native-roles.md) §9.7）。

---

## 9. 設計判断点（人間確認結果）

> **前提変更の影響（2026-06-11）**: 下記 5 点は旧前提（IME 制約で窓口は renga 継続・既定 renga）の下で確定されたが、**制約 2（IME 制約）の撤回**により方針が完全移行へ差し替わった（[§1](#1-目的とスコープ) 制約 2/3、renga-decoupling.md §1.2）。これに伴う変更は以下に閉じる: (i) **flag の最終既定が `broker` へ反転**（移行期は renga 既定のまま、ゲート通過後に反転。[§5.1](#51-backend-選択-flag)）、(ii) **窓口を含む全ペインが flag に従う**（旧「窓口だけ renga 継続」の二重構造を破棄。[§5.1](#51-backend-選択-flag) / [§5.5](#55-併存切戻しopt-in--rollback)）。確認点 1〜5 の判断内容自体（成果物形態 / broker 名 / codex surface / reconcile 正準 / flag 粒度 2 段）は**いずれも有効で不変**。下記の「既定 renga」表現は移行期の既定を指す。

下記 5 点は**人間判断で確定済み（2026-06-10、窓口経由）**。4 点は本書の推奨どおり、確認点 3 のみ変更:

1. **成果物形態** → **確定（推奨どおり）**: 本書（新規 `ja-migration-plan.md`）+ `renga-decoupling.md` に最小の次段ポインタ追記（§10）。
2. **MCP サーバー名** → **確定（推奨どおり）**: broker は `org-broker`（renga と別名）。FQ ツール名は `mcp__org-broker__*` に書き換わる前提（[§3.4](#34-重要結論--併存設計のため-broker-は別名org-brokerを採りfq-名は書き換わる)。drop-in は形レベル・FQ 名は機械置換）。
3. **spawn_codex_pane / new_tab / focus_pane のスコープ** → **変更して確定**: `spawn_codex_pane` は required 外だが**初期 broker surface に含める**（将来の codex peer pane spawn に最初から備える）。codex 課金中立ビルダーを伴う（default-deny allowlist。`exec`/`review`/`*-server` 等の非対話サブコマンドを拒否、[§3.3-6](#33-互換性を上げる設計推奨配線替え量を最小化する寄せ方)）。`new_tab`/`focus_pane` は初期除外で確定。
4. **(d) の推奨** → **確定（推奨どおり）**: 差分 reconcile を backend 横断正準で維持 + tmux hooks accelerator は defer（control mode 主軸は不採用、[§6.3](#63-推奨--cただし-accelerator-は-defer)）。
5. **flag の粒度** → **確定（推奨どおり）**: messaging all-or-nothing 先行 + pane 操作後追いの 2 段（§5.5）。面単位のさらに細かい中間状態は許さない。

---

## 10. 既存 Issue の再スコープ推奨（新前提 / design only）

> **本節の位置付け（重要）**: 以下は **2026-06-11 の前提変更（IME 制約撤回 → 完全移行）を受けて、既存 Issue を「どう改訂 / supersede すべきか」の推奨**である。**design only — 本節は推奨を書くだけで、実際の Issue 編集・クローズ・本文書換は一切行わない**（GitHub への書込・production claude-org-ja・runtime 挙動には触れない、本タスクの不可触制約）。実 Issue への反映は**人間ゲート後**に窓口・ユーザー判断で行う。
>
> **断定範囲の明示**: 「IME 非阻害（pure backend でスピナー描画・ナッジ注入が日本語 IME を壊さない）」は spike/ime-parity（2026-06-11）+ AC-1 状態 2（2026-06-08）で**確認済として断定**する。一方、**本節の Issue 再スコープは「提案」に留める**。各 Issue の正確な本文・現在の状態は本フォークから直接照合していない（GitHub 不可触）。下表は CLAUDE.md / renga-decoupling.md / 本書に記録された各 Issue の**前提の記述**に基づく推奨であり、実本文との細部突合は人間ゲートで行う。

### 10.1 推奨サマリ表

| Issue | 記録上の旧前提 | 新前提下の評価 | 推奨 |
|---|---|---|---|
| **ja #513** | broker を **opt-in 追加**（renga 既定のまま broker を任意で足す）前提の ja 統合シーム系 | opt-in の方向が**反転**: 最終形は broker 既定・renga が opt-in。ただし「flag + 生成系シーム + pin bump」の骨子（本書 [§5](#5-c-ja-統合シーム最小化設計) / [§8 Issue D](#8-e-issue-分解案)）は有効 | **改訂**（supersede ではない）。flag の最終既定を broker に、対象を「窓口含む全ペイン」に修正。骨子は流用 |
| **ja #514** | **bit 等価**（flag=renga で現行と bit 一致 = 非破壊の証明）前提 | bit 等価テスト自体は有効だが**目的が変わる**: 「opt-in が非破壊である証明」→「**opt-in fallback（renga）への切戻し忠実性**」。完全移行後も renga 経路の bit 等価は切戻し品質として要る | **改訂**。テストは維持し、意味付けを「切戻し忠実性」に再定義（[§5.1](#51-backend-選択-flag) / [§8 Issue D](#8-e-issue-分解案) の完了基準と整合） |
| **ja #515** | **renga 既定**（renga が組織の標準 backend）前提 | 新前提と**直接矛盾**: 最終既定は broker（pure backend）、renga は opt-in fallback | **改訂**（記述が renga 既定に深く依存していれば **supersede** して新前提の dogfood/既定反転 Issue（[§8 Issue G](#8-e-issue-分解案)）に置換）。「窓口だけ renga 継続」記述があれば撤回 |
| **transport-lab #9** | **WezTerm 実機 AC**（フォークの WezTerm 実機検証、輸送層中心） | 新前提で WezTerm は **窓口（人間 IME 入力）を含む第一級の pure backend**に格上げ。AC の射程が「輸送層の WezTerm 実機」から「**窓口の renga-free 実機運用（IME 非阻害の実機再確認含む）**」へ拡張 | **改訂（拡張）**。AC を「WezTerm 実機で窓口含む全ペイン renga-free + 実 IME 入力で確定文字列が壊れない」まで広げる。spike/ime-parity の手動 AC テンプレ（[`spike/ime-parity/manual-ac-ime-parity.md`](../../spike/ime-parity/manual-ac-ime-parity.md)）を流用可 |
| **runtime #45 / #47** | broker / terminal の **`claude_org_runtime` 抽出**関連（runtime 側リリース） | 抽出の骨子（[§4](#4-b-runtime-抽出設計claude_org_runtime) / [§8 Issue A/B](#8-e-issue-分解案)）は**新前提でも不変**。差分は: (i) `settings.generator` の最終既定が broker（flag-aware、[§5.3](#53-allowlist分類-b生成を-flag-aware-にする)）、(ii) transport surface descriptor 新設（[§5.2](#52-transport-プレフィックスを生成系の単一シームに閉じる)）、(iii) **RengaAdapter は必須経路から任意の互換オプションへ降格**（[§4.5](#45-既存資産との同居方針)） | **改訂（小）**。抽出スコープは維持。flag 既定方針・descriptor・RengaAdapter 任意化の 3 点を反映。paired ja sync（[§4.7](#47-versioning-と-paired-ja-sync)）は継続 |

### 10.2 改訂 / supersede の判断指針

- **改訂で足りるケース**: Issue の骨子（実装スコープ・成果物）が新前提でも生きており、**前提・既定値・対象範囲の文言修正**で整合できるもの（#513 / #514 / #9 / #45 / #47）。本書 [§8](#8-e-issue-分解案) の新 Issue 分解（A〜G）に各既存 Issue を対応付け直すのが基本。
- **supersede を検討するケース**: Issue の主旨が「renga 既定」「窓口は renga 継続」という**撤回された前提そのもの**に立脚していて、文言修正では筋が通らないもの（#515 がこれに該当しうる）。その場合は当該 Issue をクローズ（supersede）し、[§8 Issue G](#8-e-issue-分解案)（dogfood + 既定反転）等の新 Issue に置換する。
- **新規追加が要るか**: 本書 [§8](#8-e-issue-分解案) の A〜G は新前提で再導出済みのため、既存 Issue 群を A〜G にマップし直せば概ね充足する。**「全ペイン renga-free org-start」を完動ゲートに含める** 点（新定義、renga-decoupling.md §7.6）が既存 Issue 群に無ければ、Issue G の完了基準として明記する追補を推奨。
- **不可触の徹底**: 上記はすべて**推奨**。実 Issue の編集・状態変更・ラベル付けは人間ゲート後に窓口/ユーザーが行う。本タスク（ワーカー）は doc への推奨記載で停止する。

---

## 改訂履歴

- 2026-06-13: **挙動層設計 doc `broker-native-roles.md` を新設し相互参照を追記（design only / Refs #16）**。dogfood（ja#515、2026-06-13）で観測された defect 1〜4（nudge が idle セッションを起こさない / secretary 受信契機なし / dispatcher /loop 自己開始せず / tmux 独立セッション）の共通根因 = 機械置換が API 形状は満たすが push→pull の挙動層を埋めないこと、を受け、受信モデルを pull-first cadence に再導出する独立 doc を追加。本書 §5.2(ii)（受信モデルの cadence/役割設計の一次参照ポインタ）と §8 Issue E（完了基準に prose 変更一覧 P1-6 の反映を追加）に相互参照を最小追記。§5 は静的シーム（flag+生成器+pin）の SoT のまま、挙動層 SoT は新 doc に分離。
- 2026-06-10: 初版（design only。ja-migration-extraction-design 委譲タスクの成果物。(a) renga 互換性調査+gap / (b) runtime 抽出 / (c) ja 統合シーム / (d) control mode vs 差分 reconcile 判断 / (e) Issue 分解を収録）。codex design review 1 周（gpt-5.5、Blocker 0 / Major 6 / Minor 2 / Nit 1。総評「重大な設計破綻なし、§6 推奨は妥当」）を反映: tool 数 15→**14**（`spawn_codex_pane` を required 外に。SoT=`tools/check_renga_compat.py`）/ `cwd` を field parity 必須に格上げ / transport flag を初期 env 限定（Set C 改訂回避）/ **transport surface descriptor** 新設で複数生成器の単一 SoT 化 / 切戻し完了条件を 5 項目に具体化 / Set C `.state/broker/` 改訂を Issue B に前倒し / §3.4・§7 の断定を緩和。
- 2026-06-10: 5 設計判断点の人間回答を反映（窓口経由）。4 点は推奨どおり確定（成果物形態 / broker 名=`org-broker` 別名 / 差分 reconcile 正準・control mode 不採用 / flag 粒度 2 段）。**確認点 3 のみ変更**: `spawn_codex_pane` を required 外と承知の上で**初期 broker surface に含める**（将来の codex peer pane spawn に備える）。§3 intro・§3.1 表・§3.3-6（codex 課金中立ビルダー）・§8 Issue C・§9 を更新。
- 2026-06-10: 確認点 3 反映分に codex design review 1 周（gpt-5.5、Blocker 0 / Major 2 / Minor 0 / Nit 1）を追加適用。(M1) 初期 surface の数を厳密化 — required 14 には `new_tab`/`focus_pane` も含まれるため、broker 初期 surface = **移植 12 面 + `spawn_codex_pane` = 13 面**と全節整合（§3 intro・§3.1・§8）。(M2) codex 課金中立 guard を `codex exec` 単体 blacklist ではなく **default-deny allowlist**（`exec`/`review`/`*-server`/`apply`/`sandbox`/`completion`/未知サブコマンド/`--`/bare positional を拒否）に修正（§3.3-6・§8）。(Nit) §9 見出しを「設計判断点（人間確認結果）」へ。総評「design only/ja 不可触の逸脱なし」。
- 2026-06-11: **前提変更（IME 制約撤回 → 完全移行）を反映（design only / Refs #6 #9）**。renga-decoupling.md §1.2 の確定制約 #2 撤回（spike/ime-parity 2026-06-11 + AC-1 状態 2 2026-06-08、IME 非阻害は確認済）に追従し、移行方針を「輸送層だけ broker・窓口は renga 継続」から「**窓口含む全ペインが pure backend で renga-free に起動する完全移行。renga は opt-in fallback として任意残置**」へ改訂。更新節: status header（前提更新注記）/ §1 制約 2 撤回・3 完全移行 / §2 全体像（既定を broker・renga を opt-in fallback に反転）/ §3.4 併存理由（制約 2 撤回後も opt-in fallback として併存しうる）/ §4.5 RengaAdapter（必須経路 → 任意の互換オプションへ降格）/ §5.1 flag 既定（移行期 renga 既定 → ゲート後 broker 既定へ反転・窓口含む全ペインが flag に従う）/ §5.5 併存（完全移行後も renga 任意残置）/ §8 Issue D（窓口含む全ペイン・bit 等価=切戻し忠実性）・Issue G（全ペイン renga-free org-start + 既定反転判断）/ §9（前提変更注記。確認点 1〜5 の判断内容は不変）。**新設 §10「既存 Issue の再スコープ推奨」**: ja #513/#514/#515・transport-lab #9・runtime #45/#47 を新前提で改訂 / supersede すべきかの推奨表を追加（**推奨のみ・実 Issue 編集は人間ゲート後・GitHub 不可触**）。IME 非阻害は断定、Issue 再スコープは提案、と書き分け。design-only: 実装・production ja・runtime 挙動・GitHub への書込なし。
- 2026-06-13: **配送モデル方向反転（push 一次）を反映（design only / Refs #18）**。メッセージ配送を「全エージェント pull 化」から **「push 一次（`claude/channel`）+ pull フォールバック」**へ再設計（一次設計は [`broker-native-roles.md`](broker-native-roles.md) §9）。更新節: status header（方向反転注記）/ §3.2(1)（broker も push 一次・pull はフォールバック）/ §5.2(ii)（spawn 儀式に dev-channel sidecar + 3-3b 承認の再導入・`nudge_failed` 不要化・launcher argv の bit 等価）/ §5.5（切戻し第 6 条件 = channel sidecar reap + delivery_mode reset + delivery-scoped credential revoke）/ §8 Issue 表（A=R3 channel sidecar / B=R4 daemon delivery lifecycle + delivery-scoped token / D=D2 receive_mode=push + launcher argv golden / E=P8・P9・S3 / G=K1 HARD spike + 3-3b 再導入 / H=N1 撤回・S2 不変）。**dev-channel 廃止は未批准提案だったため #18 が正当に撤回**（Set D は dev-channel injection を依然 MUST）。renga 不変性・切戻し安全は維持（broker 枝・加算・flag-gated）。design-only: 実装・production ja・runtime 挙動・GitHub への書込なし。
