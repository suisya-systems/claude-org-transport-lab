# Herdr backend の workspace レイアウトポリシー設計 — control 面 1 スペース + プロジェクト単位ワーカースペース

> ステータス: **design only / 実装なし**。本リポジトリにこの設計の実装は一切存在しない（設計書のみ）。実体コード（HerdrAdapter / broker / launcher）は claude-org-runtime 側にあり、本リポジトリ（transport-lab フォーク）には持ち込まない（[`docs/non-goals.md`](../non-goals.md) §6 と整合）。
>
> **目的**: claude-org-runtime Issue #110（dogfood フィードバック）で提起された、Herdr backend の **workspace レイアウトポリシー**を設計として固定する。現行 HerdrAdapter は「専用 workspace 1 つに全ペインを詰める」設計だが、実走では画面領域が破綻する（ワーカー数枚 + 制御系ペインが単一スペースの分割に積み上がり実用に耐えない）。本書は **control 面 1 スペース + ワーカーはプロジェクト単位スペース**というレイアウトへ拡張し、そのために必要な (1) isolation 境界の「単一 workspace_id → org 所有 workspace 集合」への拡張、(2) spawn 時の workspace 選択入力を渡す層の確定、(3) workspace の lazy 作成と全ペイン close 時の掃除、(4) 世代識別（daemon 再起動で workspace が再利用され孤児が混ざる問題）との整合、を設計判断として固定する。
>
> **最重要の前提（結論先出し）**: 本レイアウトは **Herdr `agent.start` の決定的な per-workspace 配置**に依存する。しかし実測（runtime Issue #114）で **Herdr 0.7.1 の `agent.start` は `workspace` / `tab` パラメータを尊重せず、focused workspace にペインを相乗り配置する**ことが判明した。したがって本書は「専用 workspace で隔離」という [`docs/design/herdr-adapter.md`](./herdr-adapter.md) §3.4 / §4.2 の前提を **そのままでは成立しない前提**として明示し（[§3](#3-前提条件と硬い依存)）、決定的配置の充足経路を設計論点として扱う（[§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処)）。本レイアウトの multi-space 便益は、少なくとも 1 つの決定的配置戦略が capability probe で成立することにゲートされる。
>
> 依存ドキュメント（参照は本書 → 既存文書の一方向）:
> - [`docs/design/herdr-adapter.md`](./herdr-adapter.md)（現行 HerdrAdapter 設計 SoT。本書はその「単一 workspace 隔離」前提をレイアウト面で拡張・supersede する。cross-reference は herdr-adapter.md 側 §3.4 / §4.2 にも追記済み）
> - [`docs/reports/herdr-socket-spike.md`](../reports/herdr-socket-spike.md)（Herdr 0.7.1 / protocol 14 の実測。workspace / tab / pane 階層・events 挙動・error 語彙の一次実測）
> - [`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md)（Contract Set D。特に Surface 1 spawn / Surface 4.2 single-tab MUST。本書の Set D 影響は [§10](#10-set-d-契約への影響single-tab-must-の再解釈)）
> - [`docs/design/renga-decoupling.md`](./renga-decoupling.md)（org-broker + terminal adapter 境界。§4.7 能力表 / §7 Phase 体系）
> - [`docs/design/broker-native-roles.md`](./broker-native-roles.md)（受信挙動層 / push 一次配送）
>
> **runtime 側の一次情報**（GitHub Issue / PR、本書執筆時点で参照）: runtime Issue #110（本レイアウト提起）/ #114（`agent.start` の focused-workspace 相乗りと mass false-reap の root cause）/ #109（誤 reap + close 不発 + 世代共有の観測）/ PR #112（決定的 liveness モデル + 常時 close 検証。「workspace の世代識別 / stale 一括掃除は #110 に依存」と明記）。`agent.start` の focused-workspace 相乗りは #114 調査で観測済みの root-cause finding として扱う（[§3](#3-前提条件と硬い依存) P1）が、それを **workspace 尊重へ修正/確認できるか（戦略 A の成否）・`pane.move` の cross-workspace 可否・headless focus 意味論**といった配置決定性の充足経路は **capability probe 6（[§11](#11-capability-probe-placement-probe-6)）で確定する**ものとし、本書は probe 前に断定しない。

---

## 1. 背景と問題

### 1.1 現行の「単一専用 workspace」設計と破綻

現行 HerdrAdapter（[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §3.4 / §4.2、runtime 実装 `terminal/herdr.py`）は **専用 workspace を 1 つだけ確保**し（初回 spawn で lazy に `workspace.create`）、その workspace の pane のみを list / close する（`isolated_session=True`）。この単一 workspace 前提は isolation フィルタ（無関係 pane を混入させない）の単純化には正しい。

しかし dogfood 実走（runtime Issue #110）で、この設計は画面領域を破綻させることが判明した:

> 窓口とディスパッチャーは同じスペースで良いが、ワーカーはワーカーごと、またはプロジェクト単位でスペースを分けないと画面領域が厳しい（ユーザー原文意訳）

単一 tab / 単一 workspace に「制御系（secretary / dispatcher / watcher）＋ 並走ワーカー数枚」を全て分割で積むと、1 ペインあたりの領域が実用下限を割る。Herdr は headless server が全 PTY を pump する（[`docs/reports/herdr-socket-spike.md`](../reports/herdr-socket-spike.md) §0）ため機能は動くが、人間が attach して監視・介入する際の視認性が成立しない。

### 1.2 提案レイアウト

- **control スペース（1 org あたり 1 つ）**: dispatcher / watcher（pr-watch・attention）、および managed spawn される secretary Claude セッションといった **adapter-managed の制御系 pane** を 1 スペースに集約する。org のライフタイムと同寿命（org down まで掃除しない）。**人間 窓口（logical pane）はこの owned workspace の外**に置く（`workspace.close` で巻き添えにしないため。[§4.1](#41-現行と拡張後--2-つの集合の分離)）。Issue #110 の「secretary (logical)」は secretary の論理識別（broker registry 上の bind）を指し、人間の閲覧 pane そのものではない。
- **ワーカースペース（プロジェクト単位、0..N、lazy）**: 同一プロジェクトの並走ワーカーが同居する（ラベル例 `project:<slug>`）。**1 プロジェクト 1 ワーカーの通常ケースは実質ワーカー単位に等しく、per-worker / per-project 両案を包含する**（per-project を採る理由: 1-project-1-worker では退化して per-worker と一致し、複数ワーカーが同一プロジェクトを並走する場合のみ同居する = 上位互換）。プロジェクト完了で空になったら掃除する（ephemeral）。

これにより、人間は control スペースで組織全体を俯瞰し、注目するプロジェクトのワーカースペースへ切り替えて深掘りできる。Herdr の headless server は非フォーカスの workspace の PTY も pump するため、dispatcher の監視ループは自分が表示していないプロジェクトスペースの pane も観測できる（本レイアウトが成立する構造的な前提。[§9](#9-監視とイベントの-multi-workspace-対応)）。

### 1.3 本書のスコープと非スコープ

- **スコープ**: レイアウトポリシーの定義、isolation 境界の集合化、spawn 時 workspace 選択入力の層設計、lazy 作成 / 空スペース掃除、世代識別ラベル、control スペースの分割方向、`agent.start` の focused-workspace 問題（#114）への対処戦略、Set D single-tab MUST の再解釈、#109/#110/#112/#114 の依存整理、capability probe（placement）の定義。
- **非スコープ**: 実装コード（claude-org-runtime 側）、Set D 契約本文の改訂（Surface 4.2 amendment は本体取り込み時の別 PR。本書は影響を [§10](#10-set-d-契約への影響single-tab-must-の再解釈) で flag するのみ）、renga / WezTerm / tmux backend の挙動変更（本ポリシーは Herdr 固有。他 backend は flat session のまま）、Issue #114 の liveness 修正本体（別ワーカー担当。本書は依存関係のみ整理、[§12](#12-依存関係の整理-109-110-112-114)）、Herdr 本体の挙動変更提案の実施。

---

## 2. 用語と Herdr 分割方向のマッピング（混同注意）

本書と関連レイヤで **「vertical / horizontal」の指す向きが逆転している**ため、最初に固定する。

| 概念 | claude-org / renga 用語 | 見た目 | Herdr `direction` 値（実測 [`docs/reports/herdr-socket-spike.md`](../reports/herdr-socket-spike.md) §項目1 / gotcha 8） |
|---|---|---|---|
| 上下に積む（stacked） | **horizontal** | ペインが上下に並ぶ | `down` |
| 左右に並べる（side-by-side） | **vertical** | ペインが左右に並ぶ | `right` |

Herdr の `pane.split` / `agent.start` の `direction` / `split` は **`right` | `down` の 2 値のみ**（`left` / `up` 不可）。Issue #110 追補フィードバックの「control スペースの分割は vertical（左右）でなく **horizontal（上下）** が良い」は、Herdr 語彙では **`down` を既定にする**ことを意味する（[§8](#8-control-スペースの分割方向-上下-herdr-down)）。

- **space / スペース**: 本書では Herdr の 1 **workspace** を「スペース」と呼ぶ。Herdr の workspace は原理的に複数 tab を持ちうる（`tab.create` / `tab.list` で `w1:t1` / `w1:t2` …、[`docs/reports/herdr-socket-spike.md`](../reports/herdr-socket-spike.md) §項目1）が、**本 adapter は 1 owned workspace につき `workspace.create` が返す単一 tab（`active_tab_id`）のみを使い、orchestration 目的で `tab.create` を発行しない**（[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §3.4 の「単一 tab スコープ強制 / `tab.create` を orchestrator 用に使わない」方針を workspace 単位で継続する **per-workspace single-tab 不変条件**）。よってスペース = **(workspace_id, その adapter-managed tab_id) の組**で、adapter はこの tab_id を `_spaces` に記録し、list / close / addressing を workspace_id **かつ** tab_id で絞る（owned workspace 内に外部が作った余分な tab があってもその pane を混入・close しない、[§4.1](#41-現行と拡張後--2-つの集合の分離)）。識別子は階層コロン表記（`workspace_id="w1"` / `tab_id="w1:t1"` / `pane_id="w1:p2"`）。
- **space key**: レイアウト上の論理スペース種別を表す adapter 内キー。`control` または `project:<slug>`。broker が role / project-slug から算出し、adapter が workspace へ解決する（[§6](#6-設計論点-2-spawn-時-workspace-選択入力の層設計)）。

---

## 3. 前提条件と硬い依存

本レイアウトは以下を前提とする。特に **P1 は現行実測で成立していない前提**であり、本書の設計論点の起点である。

- **P1（要修正 / 要確認）**: Herdr `agent.start` が `workspace` / `tab` パラメータどおりに pane を配置すること。**現状は不成立（観測済み）** — runtime Issue #114 の root cause 調査で、Herdr 0.7.1 の `agent.start` は `workspace` / `tab` を **無視し、focused workspace（ユーザーが TUI で見ている workspace）に相乗り配置する**ことが観測された（`herdr pane get` で実所属 `workspace_id` が adapter の意図した workspace と食い違う）。この **現状挙動（ignore）は #114 の root-cause finding として扱う**が、**それが Herdr の仕様か bug か / 尊重するよう修正・確認できるか（= 配置戦略 A の成否）は #114 の未完了ステップであり、probe 6a で確定する**（[§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処) / [§11](#11-capability-probe-placement-probe-6)。本書は「現状 ignore」は観測事実として述べるが、「尊重へ直せるか」は断定しない）。これが崩れたまま（尊重も workaround も不成立）だと「専用 workspace で隔離」も「control / project のスペース分離」も成立しない。対処は [§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処)。
- **P2（済み依存）**: 決定的 liveness モデルが有効であること。runtime PR #112 が導入済み（pane 毎に spawned_at / last_seen_at / missing_since / missing_count を追跡、age 超過 + 連続欠落回数 + 実時間継続の 3 条件成立時のみ reap、常時物理 close 発行 + `closed_via` 確証、`HerdrAdapter.kill_pane_detailed` / `close_workspace(bool)` / defer 意味論）。**PR #112 は「workspace の世代識別 / stale 一括掃除は #110 に依存」と明記**しており、本書 [§5](#5-設計論点-4-世代識別と起動時-stale-掃除) がそれを供給する。
- **P3（Blocker 先行）**: Issue #114 の mass false-reap 修正が先行すること。#114 は「`agent.start` の focused-workspace 相乗り → 意図した throwaway workspace が root pane cleanup で auto-close → `pane.list {workspace}` が恒常 `workspace_not_found`→空 → 全 managed pane が欠落扱い → 生存ペイン mass false-reap」という連鎖であり、レイアウト以前の liveness Blocker。本書のレイアウトは #114 が確立する「実配置への rebind による liveness 回復」の上に構築する（[§12](#12-依存関係の整理-109-110-112-114)）。
- **P4（構造前提）**: Herdr backend は **detached / headless** で運用される。broker path の spawn は runtime Issue #99 で `choose_split`（rect balanced-split）を **バイパス**し、detached session に split 座標を要求しない設計になっている（runtime `broker/placement.py` は deprecated、`build_plan` が明示 `max_concurrent_workers` policy + 安定固定 spawn target を使う）。**帰結**: pane の rect 精密配置はオーケストレーション正しさに load-bearing ではなく、**人間が attach して見るときの画面体験**の問題である。よって本レイアウトは「どの workspace に置くか（スペース分離）」と「スペース内の既定分割方向」を扱い、rect balanced-split には依存しない。
- **P5（識別子前提）**: Herdr の `pane_id` は `wN:pM` 形式で **workspace prefix を含みグローバル一意**（[`docs/reports/herdr-socket-spike.md`](../reports/herdr-socket-spike.md) §項目1）。イベント / list 応答は各 pane に `workspace_id` を付す。これにより、複数 workspace 横断でも pane-addressed op（`pane.read` / `pane.send_keys` / `pane.close`）は曖昧さなく解決でき、pane の所属 workspace を応答から判定できる（[§4](#4-設計論点-1-isolation-境界の集合化) / [§10](#10-set-d-契約への影響single-tab-must-の再解釈) の基盤）。

---

## 4. 設計論点 1: isolation 境界の集合化

Issue #110 論点「adapter の isolation 境界を『単一 workspace_id』から『org 所有 workspace 集合』へ拡張（list / close / org down の範囲判定）」。

### 4.1 現行と拡張後 — 2 つの集合の分離

現行 `HerdrAdapter` は単一 `_workspace_id` を保持し、`list_panes()` は `pane.list {workspace_id}` を単一 workspace で絞り、さらに adapter 側で `p["workspace_id"] == self._workspace_id` を厳格再確認する（runtime `herdr.py`）。本レイアウトはこれを集合へ拡張するが、**「close の権限を持つ集合」と「liveness を追跡する集合」を明確に分離する**（[§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処) の verify+rebind が実配置 workspace を無条件に前者へ混ぜると isolation を破るため — これが本書の最重要不変条件）:

- **close-authority owned set（close 権限集合）**: adapter が **自ら `workspace.create` で作成し、かつラベルが `{prefix}/{org_instance_id}/g{current}/` に前方一致する** workspace のみからなる集合。`space key → (workspace_id, tab_id)` の写像（`_spaces`。tab_id は `workspace.create` が返す単一 `active_tab_id`、[§2](#2-用語と-herdr-分割方向のマッピング混同注意) の per-workspace single-tab 不変条件）の workspace_id 値集合がこれ。**org down / 空スペース掃除で `workspace.close` を発行してよいのはこの集合のメンバに限る**。Herdr ラベルは一意制約が無い（[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §4.2）ため、集合の権威は「自作成した」という adapter の記録であり、ラベルは起動時 discovery（[§5](#5-設計論点-4-世代識別と起動時-stale-掃除)）と人間可読性の補助。**不変条件（isolation の核）: close-authority owned set のメンバシップは「自作成 + 自ラベル一致」を要件とし、runtime の rebind / list 観測では成長させない**（唯一の例外は起動時 [§5.3](#53-起動時-stale-掃除startup-sweep) step 4 の自ラベル discovery による `_spaces` 再構成 — `{prefix}/{org_instance_id}/g{current}/` ラベル一致 = 自作成の証左を持つ workspace のみを adopt するため不変条件を破らない）。
- **liveness-tracking set（liveness 追跡集合）**: pane 毎に「その pane が実際に居る workspace_id」を指すポインタ集合。verify+rebind（[§7.3](#73-戦略と-degrade-ladder)）はここへ実配置 workspace を記録する。**`list_panes()` が pane 状態を問い合わせる先を決めるためだけに使い、`workspace.close` の対象にはしない**。実配置が意図した自作成 workspace と一致する通常ケースでは両集合は一致するが、#114 で pane が別（focused = 人間 / 他 org かもしれない）workspace に相乗りした場合、その foreign workspace は **liveness-tracking にのみ入り、close-authority owned set には決して入らない**（[§7.3](#73-戦略と-degrade-ladder) の self-ownership ゲート）。
- **list の集合化（一次ゲート = registry pane_id、owned は tab_id 不変条件を追加）**: `list_panes()` は liveness-tracking set の各 workspace について `pane.list {workspace_id}` を取り union するが、**一次フィルタは常に `p["pane_id"] ∈ 自 registry`**（adapter が spawn し追跡する pane のみ）とする。これを主ゲートにしたうえで、workspace の種別で追加条件を分ける:
  - **close-authority owned workspace**（自作成）: 追加で **`p["tab_id"] == その workspace の adapter-managed tab_id`（`_spaces` の記録）を要求**し、owned workspace 内に外部が作った余分な tab の pane を弾く（[§2](#2-用語と-herdr-分割方向のマッピング混同注意) の per-workspace single-tab 不変条件 = Surface 4.2 の tab 分離を workspace 単位で維持）。
  - **foreign（liveness-tracking-only）workspace**（#114 で misplaced pane が相乗りした先。adapter 非作成なので adapter-managed tab_id は無い）: adapter-managed tab フィルタは適用**しない**（存在しないため）。代わりに **verify+rebind（[§7.3](#73-戦略と-degrade-ladder)）が `pane.get` で得た当該 misplaced pane の実 `(workspace_id, tab_id, pane_id)` を registry に記録**し、その pane_id を registry 一次ゲートで通す（tab_id は表示・突き合わせ用に保持するがフィルタ条件にしない）。これにより **misplaced pane を list から落とさず liveness を保つ**（§4.2/§7.3 の #114 対処と整合。owned-tab フィルタを foreign に強要して false-missing を再発させない）。
  返す dict は現行同様 broker の `list_panes_view` が読む key（pane_id / x / y / width / height / active / cwd / label / agent_status）に加え、`workspace_id` / `tab_id`（= space 判定）を保持する。
- **close / org down の集合化**: org down は **close-authority owned set の全 workspace を `workspace.close`** する（現行 `close_workspace()` は単一 workspace のみ → close-authority 集合をループ、各々 bool 成否確認 + 失敗は defer）。**foreign workspace は close しない**（そこに居る自 pane は個別 `kill_pane` で pane-addressed に閉じる。P5）。個別 `kill_pane` は pane-addressed で workspace 非依存に効くため、foreign workspace 相乗りの自 pane も巻き添えなく閉じられる。
- **logical pane（人間 窓口）の除外と配置**: PR #112 は human-driven の logical pane を reap 対象外にする（adapter snapshot に永遠に出ないため）。集合版でもこれを維持するが、**`workspace.close` は非 managed pane を除外できない**ため「logical pane を含む owned workspace を close する」と logical pane まで巻き添えに閉じる。これを避けるため、**本レイアウトでは人間 窓口（logical pane）を adapter の owned workspace の外に置く**（tmux の `isolated_session` が人間 pane を別サーバに置き `+1` last-pane 保険で数える前提と同型。Herdr では人間が閲覧・操作する pane は adapter が `workspace.create` で作った workspace には属さない）。したがって owned workspace への `workspace.close`（org down / 空スペース掃除）は logical pane に届かず、list_panes にも現れない（close-authority の外）。**control workspace が保持するのは adapter-managed の制御系 pane のみ**（dispatcher / watcher、および managed spawn される secretary Claude セッション。Issue #110 の「secretary (logical)」は secretary の論理識別 = broker registry を指し、人間の閲覧 pane とは別物）で、これらは org down で正当に閉じられる。**将来 logical pane を owned workspace 内に同居させる構成を採る場合は、その space の teardown を `workspace.close` でなく per-pane `kill_pane`（managed pane のみを個別 close）に切り替える**（[§1.2](#12-提案レイアウト) / [§4.3](#43-空スペース掃除ephemeral-cleanupと-control-スペース除外) の control 掃除除外と整合）。
- **strict 非所属フィルタ（isolation の核）**: 両集合いずれの経路でも、**close-authority owned set 外の workspace を `workspace.close` しない**、かつ **自 registry に無い pane を触らない**。これは現行 `herdr.py` の list_panes strict filter（「isolated_session の adapter は org down が list_panes の全 pane を broker 所有として close しうるため workspace_id 不一致 pane を通さない」= runtime 実装の Codex P2 対応）を、2 集合分離で強化したものである。

### 4.2 workspace 単位の状態と degraded / gone の区別（#114 修正 3 の集合版）

Issue #114 修正方向 3「`workspace_not_found` → benign 空 の写像が『全ペイン一斉欠落』に化けて mass false-reap を誘発する。reaper は『list ソース喪失（degraded）』を『個別 pane 不在』と区別せよ」を、集合へ拡張する。ただし **degraded を無限に defer すると逆に『恒久的に消えた workspace の pane を永久に reap しない』false-liveness の穴**（#114 の裏返し）が開くため、有界な脱出を必ず設ける。

- **workspace 単位の状態（どの集合のメンバにも付く）**: adapter が追跡する各 workspace（close-authority owned set のメンバ、および degrade で foreign 着地を追跡する liveness-tracking-only のメンバの**両方**）が `LIVE` / `SWEPT`（adapter が意図的に掃除した = 正当に消えた）/ `DEGRADED`（`workspace_not_found` や socket blip で list ソースが一時喪失）/ `GONE`（恒久的に消えたと確定）の状態を持つ。**ただし `SWEPT`（掃除）と `workspace.close` は close-authority owned set のメンバにのみ適用する** — foreign（liveness-tracking-only）workspace は後述の `DEGRADED → GONE` 有界脱出は持つが `SWEPT` にも `workspace.close` にもしない（そこに居る自 pane は個別 `kill_pane` で閉じる、[§4.1](#41-現行と拡張後--2-つの集合の分離) self-ownership）。`list_panes()` は workspace 単位で結果を合成し、**ある workspace が `DEGRADED` でも、他 workspace の pane 集合を空にしない**（単一 workspace の喪失で org 全体の liveness を落とさない）。
- **現行 clear 挙動の supersede（重要）**: 現行 `herdr.py` の list_panes は `workspace_not_found` を benign `[]` に写像し、**かつ `_workspace_id`/`_tab_id` を clear** して次 spawn で新規 workspace を作る。この clear は #109/#114 の孤児増殖アーム（消えた workspace を捨てて別 workspace を量産）である。本レイアウトは **これを supersede する — `workspace_not_found` を受けた owned-set メンバは集合から clear/削除せず `DEGRADED` として保持する**（自動 recreate しない）。集合から外れるのは `SWEPT` / `GONE` 確定時のみ。
- **DEGRADED → GONE の有界な脱出（両集合の workspace に適用）**: `DEGRADED` は一時喪失の仮定であり、放置しない。**追跡中の各 workspace（close-authority / foreign liveness-tracking-only の双方）**を `workspace.list`（起動時だけでなく runtime でも）と突き合わせて再確認する — **`workspace.list` に現れない workspace は恒久的に消えたと確定し `GONE`**（その pane を reap 対象へ解放）、**現れるが `pane.list` が一時失敗しているだけなら `DEGRADED` を継続 defer**。加えて `DEGRADED` に連続回数 / 実時間の上限を設け（PR #112 の per-pane ゲートと同型のしきい値）、上限超過で `workspace.list` 再確認を強制する。**foreign workspace（degrade 経路で自 pane が相乗りした先）が消えた場合もこの有界脱出で `GONE` へ収束させ、その自 pane を解放する** — さもないと [§7.3](#73-戦略と-degrade-ladder) の degrade で foreign 上に留まった pane の false-liveness leak が残る。これにより「人間が TUI でプロジェクトスペースを閉じた」「Herdr が最後の pane 退出で workspace を auto-close した」等の恒久喪失が、close-authority / foreign いずれの workspace でも世代内で解決され、dead worker が世代末まで possibly-alive のまま残らない。
- **reap 抑止と解放**: pane の所属 workspace が `DEGRADED` の間は reap を抑止（PR #112 defer に workspace-level を追加）。`SWEPT`（[§4.3](#43-空スペース掃除ephemeral-cleanupと-control-スペース除外) の意図的掃除）/ `GONE`（上記確定）の場合のみ、その workspace の pane を正当に消えたと扱い reap 対象にする。**恒久喪失を『個別 pane 不在』と即断せず、また『一時 degraded』と永久に扱わない**両側を有界化する。
- これにより #114 の「list 恒常空 → 全 managed pane false-reap」は集合化 + workspace 単位状態で構造的に断ち、その裏返し（gone workspace の pane を永久 defer する false-liveness leak）も `workspace.list` 突き合わせと DEGRADED 上限で断つ。

### 4.3 空スペース掃除（ephemeral cleanup）と control スペース除外

Issue #110 論点「workspace の lazy 作成 / 全ペイン close 時の workspace 掃除」の掃除側を、[§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版) の状態機械の上に固定する（lazy 作成側は [§7.4](#74-lazy-作成と-root-pane-の-114-対応)）。

- **空検知**: プロジェクトスペースが空になる契機は 2 つ — (a) org 主導 close（ワーカー CLOSE_PANE でそのスペースの最後の managed pane を閉じる。`kill_pane_detailed` の sole-pane `workspace.close` fallback が発火）、(b) poll でそのスペースの owned live pane 数が 0 になる（ワーカーが自己終了）。
- **LIVE → SWEPT 遷移**: 次のいずれかを adapter が記録できた時のみ LIVE → SWEPT に遷移し、close-authority owned set から外す — (i) **そのスペースの最後の自 managed pane が閉じた / 退出した**（通常の ephemeral 完了）、または (ii) **lazy 作成した workspace が意図した agent pane を受け取らないまま意図的に掃除される**（[§7.4](#74-lazy-作成と-root-pane-の-114-対応) の misplacement — agent が foreign に流れ born-empty の自 workspace を捨てるケース）。この「自 managed pane が居ない / 居なくなった」という記録が SWEPT と DEGRADED を判別する — 記録がある空 = `SWEPT`（正当に空、掃除してよい）、記録なしの `workspace_not_found` = `DEGRADED`（[§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版) の gone 確認へ）。
- **掃除の in-flight / grace ガード**: 空スペースの `workspace.close`（掃除）には **workspace-level の min-age / grace period** を課し、かつ **そのスペースへの spawn が in-flight の間は掃除を抑止する**（spawn-in-flight カウンタを adapter lock 下で持つ）。さもないと、ワーカーの `agent.start` 直後〜pane が `pane.list` に現れるまでの boot latency 窓で「一瞬空」を掃除と誤認し、boot 中の pane ごと workspace を閉じる（[§7.4](#74-lazy-作成と-root-pane-の-114-対応) の #114 auto-close 連鎖の再来）。
- **per-space-key 作成 lock**: 同一プロジェクトの 2 ワーカーが同時に spawn すると、両者が `_spaces` にキー不在を見て二重に `workspace.create` しうる。**space_key ごとの作成 lock**（現行 `_spawn_lock` の粒度を space_key 別に）で lazy 作成を dedup する。
- **掃除の retry（SWEPT の close 失敗）**: 掃除の `workspace.close` は失敗しうる（`workspace_close_failed` / `refused` → PR #112 の defer）。SWEPT にして即 owned set から落とすと、CURRENT 世代ラベルのまま残り、起動時 sweep（[§5.3](#53-起動時-stale-掃除startup-sweep) は generation < current のみ）が次 boot まで回収できず **世代内孤児**になる。よって **掃除失敗の workspace は pending-sweep 集合に保持**し（live-list liveness 源からは除外するが #112 defer 下で `workspace.close` を再試行）、同一世代内で回収する。
- **control スペースは掃除除外**: `space_key == "control"` は org ライフタイムと同寿命であり、**一時的に空でも掃除しない**（[§1.2](#12-提案レイアウト)）。掃除は project スペースの ephemeral 対象のみ。

---

## 5. 設計論点 4: 世代識別と起動時 stale 掃除

Issue #110 論点「世代識別（runtime#109 付随観測: daemon 再起動で workspace 再利用され孤児が混ざる）との整合 — workspace ラベルに org/daemon 識別を含めると #109 の掃除も楽になる」。PR #112 が #110 へ委譲した「workspace の世代識別 / stale 一括掃除」を本節が供給する。

### 5.1 なぜ世代識別が要るか

Issue #109 付随観測: 「全 daemon 世代が同一 workspace w1 を共有していた（HerdrAdapter の専用 workspace 厳格分離前提が daemon 再起動をまたぐと成立しない — 再起動時に既存 workspace を再利用?）」「孤児は pane.list で世代識別 → keep set 以外を pane.close で回収した」。現行 adapter のラベルは `f"{prefix}-{os.getpid()}"`（PID ベース）だが、PID は **再起動で変化し、単調でなく、OS により再利用されうる**ため世代識別に不十分（旧世代 workspace が新しいか古いか判定できず、PID 再利用で誤同定しうる）。

### 5.2 ラベルスキーマ

workspace ラベルを次の構造にする（現行 `f"{prefix}-{pid}"` を supersede）:

```
{prefix}/{org_instance_id}/g{generation}/{space_key}
```

例: `claude-org/8f3a2c/g7/control` / `claude-org/8f3a2c/g7/project:transport-lab`

- **`org_instance_id`**: 本 org インスタンスの **安定かつ衝突耐性のある識別子**。broker の state dir（`.state/broker/`）に初回 org up 時に生成・永続化し、以後読み出す。**衝突耐性が isolation の要**（[§5.3](#53-起動時-stale-掃除startup-sweep) の sweep は前方一致で自 org workspace を選ぶため、2 org が同じ id を引くと高 generation 側が他 org の workspace を掃除する cross-org 汚染になる）。よって **≥128-bit のエントロピー（UUID 等）で衝突耐性を明示的に担保**する（短トークンは不可）。末尾スラッシュ前方一致は部分文字列衝突は防ぐが同一トークン衝突は防げないため、id 自体の一意性で担保する。**daemon 再起動をまたいで不変**であり、(a) 共存する別 org を区別し、(b) 再起動した daemon が「自分の過去世代 workspace」を認識できるようにする。state dir 喪失時の挙動は残存リスク（[§14](#14-残存リスク--スコープ外)）。
- **`generation`**: **daemon boot ごとの単調増加カウンタ**。同じ state dir 内に永続化し boot ごとに increment する。**現世代（live）と旧世代（孤児）を区別する**。**write-ahead 順序が必須**: increment 後の generation を **`workspace.create` を 1 つでも発行する前に state dir へ永続化（fsync）する**。さもないと「g8 に increment → g8 で workspace 作成 → 永続化前に crash」で次 boot が再び g8 を読み、[§5.3](#53-起動時-stale-掃除startup-sweep) の sweep（generation < current のみ対象）が死んだ g8 workspace を回収できず、かつ step 4 の adopt が死 daemon の g8 workspace を live として取り込む世代内孤児を生む。単調性 + write-ahead により、再起動した daemon は厳密に新しい generation を得て旧 generation を確実に孤児同定できる。
- **`space_key`**: `control` / `project:<slug>`（[§6](#6-設計論点-2-spawn-時-workspace-選択入力の層設計)）。ラベルからスペース種別が復元できる。

Herdr ラベルは一意制約が無い（[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §4.2）ため、ラベルは **起動時 discovery / 掃除**と**人間可読性**のための補助であり、owned set の権威は adapter の `_spaces` 写像（[§4.1](#41-現行と拡張後--2-つの集合の分離)）である。

### 5.3 起動時 stale 掃除（startup sweep）

**single-live-daemon lock 前提**: 掃除は「旧世代 daemon は既に死んでいる」を前提とするが、rolling / overlapping restart（新 daemon が旧 daemon の完全終了前に boot）や誤った二重起動では **旧世代 workspace が作業中で LIVE のまま `generation < current`** になり、素朴な sweep が生 pane を大量 close して in-flight 作業を破壊する。よって boot は **state dir の pid/lease ファイルで single-live-daemon lock を取得**し、旧 daemon の終了 / lease 失効を確認してから sweep する。lock が取れない（旧 daemon 生存）場合は sweep を保留し窓口へエスカレーション（自己判断で旧世代を掃除しない）。

lock 取得後、daemon boot 時（新 generation 確定後）に:

1. `workspace.list` で全 workspace のラベルを取得。
2. `{prefix}/{org_instance_id}/` に **末尾スラッシュ付き前方一致**するものを **自 org の workspace** として抽出。前方一致しない（別 `org_instance_id` / prefix 無し = 別 org・人間・無関係）は **絶対に触れない**（isolation。`org_instance_id` の衝突耐性が前提、[§5.2](#52-ラベルスキーマ)）。
3. 自 org のうち **`generation < 現 generation`** のものを **旧世代孤児**として `workspace.close` で一括掃除する。lock で旧 daemon の死を確認済みだが、なお防御的に「掃除対象に live pane が居ないか / 予期せぬ現世代 pane が混ざっていないか」を確認してから close する。掃除は PR #112 の defer 意味論に従う（`workspace.close` 成否を bool で確認、失敗は state 保持して次ラウンド再試行、成功を偽装しない）。
4. `generation == 現 generation` のラベルは（clean boot では write-ahead 永続化により通常存在しない）、存在すれば **無条件 adopt せず suspect 扱い**にする — live pane の有無を確認し、live pane を持つものだけ `_spaces` へ adopt し、空 / 死のものは掃除する（crash mid-spawn 等で残った現世代ラベルを live として誤取り込みしない）。**同一 generation + 同一 space_key のラベルが複数ある場合**（一意制約なしのため起きうる）は、live pane を持つ 1 つを adopt し残りを SWEPT 掃除する tie-break で `_spaces` を決定的に再構成する。

これは Issue #109 提案「daemon 起動時の自 workspace の stale ペイン一括掃除」を、世代識別ラベル + single-live-daemon lock により **他 org を巻き込まず・生きた旧世代を殺さず・旧世代孤児のみを狙って**実現する。#114 の孤児（focused workspace 相乗りで生まれた迷子）は、自作成 workspace でなければ close-authority owned set に入れず（[§4.1](#41-現行と拡張後--2-つの集合の分離) self-ownership ゲート）、旧世代の自作成 workspace であれば本 sweep の対象になる。

---

## 6. 設計論点 2: spawn 時 workspace 選択入力の層設計

Issue #110 論点「spawn 時の workspace 選択入力（role / project-slug 相当のメタデータをどの層から渡すか — broker spawn API は name/role を持つ。project は delegate 側の知識）」。

### 6.1 現状の層と欠落

- **adapter 面**: `TerminalAdapter.spawn(argv, cwd, new_window) -> PaneRef`（runtime `terminal/base.py`）。**role / name / project を受けない**。現行 HerdrAdapter は `os.getpid()` + counter からラベルを自前生成する。
- **broker spawn 面**: `BrokerServer.spawn_claude(caller, direction, target, name, role, model, permission_mode, extra, cwd)`（runtime `broker/server.py`）は **role / name / cwd / direction / target を持つが project-slug を持たない**。呼出は `self.adapter.spawn(argv, cwd=cwd, new_window=True)` で、**argv / cwd / new_window しか adapter に届かない**。
- **delegate 面**: project-slug は dispatcher の delegate-plan が持つ知識（worker brief / send_plan の `project_slug`。本タスクの派遣でも `project_slug` が payload に載っている）。broker spawn API へは現状渡っていない。

### 6.2 層設計（3 層のリレー）

space の選択入力を **算出できる層から下流へリレー**する:

1. **Layer A（delegate / dispatcher）— project アイデンティティの出所**: delegate-plan が持つ `project_slug` を spawn 入力として供給する。control 系（secretary / dispatcher / watcher）は launcher（`org up`）が spawn するため project は不要（role から `control` へ写像）。
2. **Layer B（broker spawn API）— role の出所 + space 算出**: broker が `role` と（追加入力の）`project_slug` から **SpaceDescriptor** を算出する:
   - control role（secretary / dispatcher / watcher）→ `space_key = "control"`。
   - worker role + project_slug → `space_key = "project:<slug>"`。
   - project_slug 欠落の worker → **既定プロジェクトスペース** `project:_unassigned`（control スペースを汚さないための catch-all。degrade だが安全側）。
   - 実装上は broker の spawn MCP surface に `project`（optional）フィールドを追加し、dispatcher の delegate 呼出が供給する。broker は SpaceDescriptor を `adapter.spawn` へ渡す。
3. **Layer C（adapter.spawn）— workspace への解決**: `TerminalAdapter.spawn` に **optional `space: SpaceDescriptor | None = None`** を追加する。HerdrAdapter は space を workspace へ解決（`_spaces` 写像を引き、無ければ lazy 作成）。**tmux / wezterm は space を無視**（flat session のまま。既存 AC 不変）。backend が本ポリシーを持つかは能力フラグ **`supports_space_layout: ClassVar[bool]`**（Herdr=True / 他=False）で宣言し、broker は `getattr` で読んで「space を算出・付与し、空スペース掃除等のレイアウト挙動を有効化するか」を分岐する（既存の `isolated_session` / `supported_named_keys` / reap 閾値 ClassVar と同じパターン）。**なおこの `space` パラメータ追加は Set D Surface 1（spawn）の契約変更である**（現行シグネチャは `spawn(argv, cwd, new_window)`）。default `None` で後方互換だが、**契約としての ratify は本体取り込みスコープ（別 PR）であり、本書は flag のみ**（[§10](#10-set-d-契約への影響single-tab-must-の再解釈)）— 4.2 amendment と同じ flag-not-ratify 規律で扱う。

**この層設計が Issue #110 の問いへの答え**: role は broker が既に持つ → Layer B で使う。project は delegate の知識 → Layer A から明示リレー（cwd からの推定 fallback は pattern 依存で脆いため、明示リレーを一次とし、欠落時のみ `project:_unassigned` へ degrade）。adapter Protocol は optional 引数追加で後方互換（default None = 現行の flat 挙動）。

---

## 7. 設計論点 placement: `agent.start` の workspace 無視への対処

**本書の中心的論点**（[§3](#3-前提条件と硬い依存) P1 / Issue #114）。SpaceDescriptor で「どの workspace に置くか」を決めても、Herdr `agent.start` が `workspace` を無視して focused workspace に相乗りするなら、control / project のスペース分離は成立しない。

### 7.1 なぜ rebind-only では #110 が解けないか

Issue #114 の最小修正は「`agent.start` 応答 pane を `pane.get` で引き、実際の `workspace_id` に adapter の bind を rebind する」ことで **liveness を回復**する（list が実在 workspace を問い合わせるので恒常空にならない）。しかしこの rebind-only は **全ペインを focused workspace 1 つに集める**（配置を制御しないため）。それは Issue #110 が解こうとしている「全部同じスペースに積む」問題そのものである。

**帰結（設計上の核心）**: #114 の rebind-only 修正は **liveness（#114 の Blocker）を回復するが、multi-space レイアウト（#110）は供給しない**。#110 は **決定的な per-workspace 配置**を要求し、それは配置を focused workspace 任せにしない機構を必要とする。

### 7.2 配置戦略（3 択）

| 戦略 | 機構 | 依存 | 長所 | 短所 / 要 probe |
|---|---|---|---|---|
| **A. Herdr が workspace を尊重** | `agent.start {workspace}` がそのまま効く | **Herdr 本体の修正 / 確認**（#114 修正方向 2） | 最も clean。adapter は SpaceDescriptor → `agent.start {workspace}` を直結するだけ | 上流（Herdr）挙動に依存し我々の制御外。probe 6a で「尊重するか」を確定 |
| **B. focus-then-spawn** | spawn lock 下で `workspace.focus(target)` → `agent.start`（now-focused な target に着地）→ `pane.get` で実配置検証 | 上流変更不要（既存メソッドのみ） | Herdr 修正を待たずに実現可 | (i) global focus を変える（人間閲覧を乱すが detached 運用が常態）(ii) spawn 中の人間の手動 focus 切替と競合（lock は adapter op のみ直列化）(iii) headless で `workspace.focus` が `agent.start` の着地先を決めるか要 probe 6b |
| **C. spawn-then-move** | `agent.start`（focused に着地）→ `pane.get` で実 workspace 判定 → target と異なれば `pane.move` / `pane.swap` で target へ移設 | `pane.move` の cross-workspace 可否 | 配置が focus 非依存で最も堅牢（成立すれば） | `pane.move` が workspace をまたげるか要 probe 6c。不能なら B へ fallback |

### 7.3 戦略と degrade ladder

**共通の必須手順（verify + rebind、#114 修正方向 1 を常時適用）**: いずれの戦略でも、spawn 後に必ず `pane.get` で **実配置 workspace を確定**する（`pane_id` の `wN:` prefix は agent.start 直後で cross-workspace move が起きていない時のみ workspace の proxy として使える。戦略 C の move 後は prefix が実 workspace と乖離しうるため [`pane.get` を正本]とし、move 後は verify を再実行する）。実 **`(workspace_id, tab_id, pane_id)`** を **liveness-tracking set に記録**して list の問い合わせ先を正す（foreign 着地では adapter-managed tab が無いので、この記録した実 tab_id / pane_id で追跡する — [§4.1](#41-現行と拡張後--2-つの集合の分離) の foreign 経路は registry pane_id を一次ゲートにし owned-tab フィルタを課さない）。これにより配置が意図とずれても **liveness は常に保たれる**。

**self-ownership ゲート（本書の最重要不変条件、[§4.1](#41-現行と拡張後--2-つの集合の分離)）**: verify で得た実配置 workspace が **自作成 workspace（`{prefix}/{org_instance_id}/g{current}/` ラベル一致）でない**場合、それを **close-authority owned set に決して加えない**。#114 で pane が focused workspace（人間 / 他 org のものかもしれない）に相乗りしたケースがこれで、無条件に owned set へ入れると org down が `workspace.close` でその foreign workspace ごと破壊する（人間 / 他 org の全 pane 巻き添え = isolation 破壊）。**foreign 着地は「配置失敗（misplacement）」として扱い**、戦略 C で自 workspace へ移設 / 戦略 B で close して re-focus+respawn / いずれも不能なら abort → 窓口エスカレーション、とする。foreign workspace に留まった自 pane は liveness-tracking で追跡し個別 `kill_pane`（pane-addressed）で閉じるが、その **workspace は決して close しない**。verify+rebind は「配置を追跡できる」ことを保証するが、「close 権限を持つ集合へ入れてよい」ことは自作成の場合に限る。

**decision ladder**（probe 6 の結果で確定）:

1. probe 6a で **Herdr が workspace を尊重**するなら → **戦略 A**（最 clean。misplacement は原理的に起きない）。
2. さもなくば probe 6c で **`pane.move` が cross-workspace 可**なら → **戦略 C**（focus 非依存で堅牢）。move 後は verify を再実行し、pane_id が変わる場合は broker bind 表を re-key する（probe 6c で id 保存性も測る、[§11](#11-capability-probe-placement-probe-6)）。
3. さもなくば probe 6b で **focus-then-spawn が headless で決定的**なら → **戦略 B**。spawn lock を focus→start→verify 全体で保持し、**verify が foreign 着地を検出したら pane を close して re-focus+respawn を有界回数リトライ、超過で abort**（foreign 着地を owned set に rebind しない）。lock は adapter op のみ直列化し人間の手動 focus 切替とは競合しうるため、6b の in-isolation 決定性だけでは B の安全性は担保されない（この remediation と lock が安全性を担う。[§11](#11-capability-probe-placement-probe-6) probe 6b 注記）。
4. いずれも不成立なら → **レイアウトは degrade**（multi-space non-conformant）。配置制御が無いので各 spawn は **その時点の focused workspace に着地し、focus が spawn 間でドリフトすれば複数の focused workspace に散らばりうる**（「single」focused workspace とは限らない）。この散らばりは self-ownership ゲートにより close-authority owned set を汚さない（liveness-tracking にのみ入る）。この場合でも liveness は保たれ（#114 は解ける）、**multi-space の便益（#110）は Herdr の配置決定性が得られるまで保留**する。degrade は正直に明示し、窓口経由で「Herdr 側修正待ち」か「戦略 B の UX 犠牲受容」かの判断を仰ぐ。

### 7.4 lazy 作成と root pane の #114 対応

`workspace.create` は root shell pane を同時生成する（[`docs/reports/herdr-socket-spike.md`](../reports/herdr-socket-spike.md) §項目1）。現行 adapter は初回 agent 起動後にこの root pane を close するが、#114 の連鎖はここで悪化した — **agent が実際には別（focused）workspace に着地したのに root pane を close すると、意図した workspace は root pane が最後の 1 枚だったため丸ごと auto-close され、以後 list が `workspace_not_found` になる**。

対処: **root pane cleanup を「実配置検証」にゲートする**。lazy 作成した workspace `W` の root pane を close するのは、**agent pane が `pane.get` で `W` に居ることを確認できた後のみ**とする。着地先が `W` でない（戦略 A 不成立で focused に流れた）場合は:
- 戦略 C/B で pane を `W` へ移設 / 再配置してから root を close する、または
- `W` を意図的に `SWEPT`（[§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版) の状態、[§4.3](#43-空スペース掃除ephemeral-cleanupと-control-スペース除外) の掃除 + close 失敗時の pending-sweep retry）として掃除する（迷子 workspace を残さない）。着地先 `W` は自作成 workspace なので close-authority owned set 内であり `workspace.close` してよい（[§4.1](#41-現行と拡張後--2-つの集合の分離) self-ownership ゲートに抵触しない — foreign な着地先は close せず個別 kill_pane で閉じる、[§7.3](#73-戦略と-degrade-ladder)）。

これにより #114 の「throwaway workspace auto-close → 恒常空 → false-reap」連鎖を、レイアウトの lazy 作成経路でも再発させない。

---

## 8. control スペースの分割方向 上下 Herdr down

Issue #110 追補フィードバック: control スペース内の窓口 / ディスパッチャーのペイン分割は **horizontal（上下）** が良い（runtime #104 の broker path spawn direction が固定値 vertical（左右）になっている点への指摘）。

**まず現状の層を正確に切り分ける**（[§6.1](#61-現状の層と欠落) / P4）: broker の `direction` パラメータ（#104 が vertical に固定した層）は **Herdr adapter へ渡らない** — broker は `self.adapter.spawn(argv, cwd, new_window)` を呼び、`direction` を forward しない（broker path は Issue #99 で `choose_split` をバイパス、detached に split 座標不要）。よって **#104 の broker-level vertical は Herdr path では inert であり、是正すべき「vertical レンダリング」は存在しない**。実際に分割方向を決めるのは HerdrAdapter が `agent.start` に渡す `split` 値で、現行 `herdr.py` は追加 pane を `"split": "down"`（= 上下）でハードコードしている。つまり **Herdr backend では上下フィードバックは adapter 層で既に満たされている**。

**設計判断（真の変更点）**:
- ハードコードの `split="down"` を **per-space policy 由来**にする（単一定数をやめる）。`SpacePolicy.split_direction`: control スペース既定 = **上下（Herdr `down`）を authoritative かつ設定可能に**、project スペース既定 = 上下（`down`。同一プロジェクトの並走ワーカーを縦積み）。[§2](#2-用語と-herdr-分割方向のマッピング混同注意) の語彙対応を厳守（claude-org「horizontal(上下)」= Herdr `down`、「vertical(左右)」= Herdr `right`）。これは「vertical → horizontal の挙動是正」ではなく「既存の down 既定を per-space に昇格し明示・設定可能化する」変更である。
- **要 probe 6d**: `agent.start` は workspace を無視した（#114）。**`split` パラメータも同様に無視するか**（= ハードコードの `down` すら効いているか）を probe で確認する。無視するなら分割方向の制御は `agent.start` の `split` に頼れず、`pane.split`（direction 指定）での配置か着地後の `pane.resize` / `layout.apply` 整形が要る（[§11](#11-capability-probe-placement-probe-6)）。分割方向は correctness に load-bearing ではない（P4: detached）ため、満たせなければ「既定方向を強制できない degrade」を明示し、レイアウトの他部分は先行させる。

---

## 9. 監視とイベントの multi-workspace 対応

複数 workspace 横断で dispatcher が pane を監視する必要がある。Herdr の制約:
- `events.subscribe` は **pane_id 必須・ワイルドカード不可**、かつ **overflow でサイレントロス**（[`docs/reports/herdr-socket-spike.md`](../reports/herdr-socket-spike.md) §項目6 / 窓口補足4）。
- 現行 HerdrAdapter は events を一切使わず、`list_panes` の polling で liveness / 状態を得る（[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §4.5、runtime `herdr.py` はイベント非使用）。

**設計判断**:
- 監視は **poll ベースを維持**する。`list_panes()` が **liveness-tracking set** を union する（[§4.1](#41-現行と拡張後--2-つの集合の分離)。close-authority より広く、degrade で foreign 着地した自 pane も含む）ため、poll reconcile は自然に全プロジェクトスペース + foreign 相乗り先を覆う。Herdr headless server が非フォーカス workspace の PTY も pump する（P4 / §1.2）ので、**dispatcher は自分が表示していないプロジェクトスペースの pane も list / read / 状態判定できる**（本レイアウトが成立する構造的前提）。これは renga の「フォーカス中タブしか見えない」制約（`spawn-flow.md`）とは異なる Herdr の優位点。
- 将来 events を採る場合は、**owned set の全 workspace の全 pane に対し per-pane subscribe** を張り、silent loss は list_panes reconcile で補う必要がある（[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §4.5 の cursor/buffer 正規化を multi-workspace の pane 集合へ拡張）。本書のレイアウト成立には events は不要（poll で足りる）。

---

## 10. Set D 契約への影響（single-tab MUST の再解釈）

[`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md) Surface 4.2 は **single-tab MUST（Q10）**「全 pane-addressed op が現タブのみで解決する」を規定し、[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §3.4 は Herdr adapter が単一 tab スコープを強制するとした。本レイアウトの multi-workspace はこれと表面的に衝突する。

**再解釈**: single-tab MUST には 2 つの根拠がある — (i) pane-addressed op が **意図しない pane（別タブ・人間のペイン）へ解決しない**という曖昧さ排除、(ii) 監視到達性（フォーカス中タブしか見えない backend で全 pane を確実に観測する、renga#71）。本レイアウトはこの解決スコープを **「単一 tab」から「org 所有 workspace 集合（現世代）× 各 workspace の単一 adapter-managed tab」へ拡張**する（tab 分離自体は捨てず、workspace ごとに単一 tab を維持したまま workspace を複数持つ、[§2](#2-用語と-herdr-分割方向のマッピング混同注意) の per-workspace single-tab 不変条件）:
- (i) 曖昧さ排除は保たれる: Herdr `pane_id` はグローバル一意（P5）で pane-addressed op（`pane.read` / `pane.send_keys` / `pane.close`）は所属 workspace に依らず曖昧さなく解決し、adapter は自 registry 外の pane・close-authority owned set 外の workspace・**owned workspace 内でも自 tab 以外の tab の pane** を絶対に触らない（[§4.1](#41-現行と拡張後--2-つの集合の分離) の 2 集合分離 + self-ownership ゲート + tab_id フィルタ = single-tab より厳密な positive membership test）。**Surface 4.2 の tab 分離は各 workspace 内で保たれ、緩和されるのは「解決先 tab が唯一の workspace に属する」制約のみ**（tab の一意性でなく workspace の一意性を緩める）。
- (ii) 監視到達性は **Herdr headless server が非フォーカス workspace の PTY も pump する**性質で別途担保される（[§9](#9-監視とイベントの-multi-workspace-対応)。renga と異なりフォーカスに依存しない）。

**ただしこれは Set D の文言（single-tab）に対しては literal な緩和である** — 現行 §4.2 は cross-tab addressing に `pane_not_found` を要求するが、本設計は owned set 内の cross-workspace addressing を**解決させる**。よって **Set D Surface 4.2 の Herdr backend 向け amendment（「Herdr backend は org 所有 workspace 集合をスコープとする」）が必要**であり、本体取り込み時の別 PR とする（[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §7.3 の契約改訂スコープに合流。本書は flag のみ、ratify しない）。加えて [§6.2](#62-層設計3-層のリレー) の **Surface 1 spawn の amendment**（optional `space` パラメータ + `supports_space_layout` 能力フラグ追加）も同様に flag のみ・別 PR 扱いとする。cross-reference は herdr-adapter.md §3.4 に追記済み。

---

## 11. capability probe placement probe 6

[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §6 の probe 表（1-5）に、本レイアウト固有の **probe 6（placement 決定性）** を追加する。実測は herdr-socket-spike 系のワーカーが担い、本書は probe 前に断定しない。

| # | probe 項目 | 何を実測するか | 確定する本書の設計判断 | 満たせない場合の degrade |
|---|---|---|---|---|
| 6a | **`agent.start` の workspace 尊重** | `agent.start {workspace, tab}` が指定 workspace に配置するか（`pane.get` の実 `workspace_id` で検証。#114 の再実測） | [§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処) 戦略 A の成否 | 不尊重なら戦略 B/C へ |
| 6b | **focus-then-spawn の決定性** | `workspace.focus(W)` → `agent.start` が **headless でも** `W` に着地するか。**加えて focus→start 窓に competing focus 変更を注入した際の着地**（strategy B の実リスクは in-isolation 決定性でなく concurrent focus 競合） | [§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処) 戦略 B の成否と安全性 | 非決定的 / 競合で誤着地なら [§7.3](#73-戦略と-degrade-ladder) の lock+remediation に依存（6b 単独では B の安全性を担保しない）→ 戦略 C か degrade |
| 6c | **`pane.move` の cross-workspace 可否 + id 保存性** | `pane.move` / `pane.swap` が pane を別 workspace へ移設できるか。**加えて move 後に `pane_id` が保存されるか（変わるなら broker bind の re-key が要る）、`pane.get` の `workspace_id` が移設先に更新されるか**（P5 の prefix=workspace proxy が move 後も成立するか） | [§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処) 戦略 C の成否と bind 整合 | 不能なら戦略 B へ fallback。id が変わるなら C は move 後に verify 再実行 + bind re-key を必須化 |
| 6d | **`agent.start` の split/direction 尊重** | `agent.start {split}` / `pane.split {direction}` が指定方向で分割するか（workspace 同様に無視されないか） | [§8](#8-control-スペースの分割方向-上下-herdr-down) の per-space 分割方向 | 無視なら `pane.resize` / `layout.apply` 整形か「方向強制不可」明示 |
| 6e | **throwaway workspace の auto-close 再現** | lazy 作成した workspace の最後の pane（root）close で workspace ごと auto-close するか（#114 連鎖の裏取り） | [§7.4](#74-lazy-作成と-root-pane-の-114-対応) の root pane cleanup ゲート | 再現するなら root cleanup を実配置検証にゲート（本書どおり） |
| 6f | **非フォーカス workspace の監視到達性** | headless server が非フォーカスの project workspace の pane も pump し、その pane に対する `pane.read` / `pane.list` が live・前進する内容を返すか（[§9](#9-監視とイベントの-multi-workspace-対応) の poll 監視前提の裏取り） | [§9](#9-監視とイベントの-multi-workspace-対応) の cross-workspace 監視前提 | 非フォーカスで observe 不能なら、監視は focus 巡回 or events per-pane subscribe が要る（レイアウトの監視前提を見直し） |

いずれも satisfy されない（6a も 6b も 6c も不成立）場合、[§7.3](#73-戦略と-degrade-ladder) の ladder に従い、配置は現フォーカス追従（複数 workspace に散らばりうる）へ degrade し、multi-space は Herdr の配置決定性が得られるまで保留する。

---

## 12. 依存関係の整理 109 110 112 114

窓口要請により、本レイアウト（#110）と関連 Issue / PR の依存を 1 節に整理する。

```
  #109 (closed)         観測: 誤 reap + 物理 close 不発 + 全世代が同一 workspace 共有 + 孤児堆積
     │  motivates
     ▼
  #112 (merged, v0.1.32) 決定的 liveness (per-pane 追跡 / 常時 close 検証 / defer /
     │                    kill_pane_detailed / close_workspace(bool))
     │  「workspace 世代識別 / stale 一括掃除は #110 に依存」と明記して委譲
     ├───────────────────────────────────────────────┐
     ▼                                                ▼
  #114 (open, Blocker)   agent.start が focused workspace 相乗り →         #110 (本書)
     │  throwaway ws auto-close → list 恒常空 → mass false-reap        world: レイアウトポリシー
     │  最小修正 = 実配置 rebind で liveness 回復                       - 世代識別ラベル + startup sweep
     │  (ただし全ペインが focused 1 スペースに集まる)                    (#112 が委譲した掃除を供給)
     │                                                                 - owned set 境界 (list/close/org down)
     │  provides deterministic-placement resolution                    - spawn 時 space 選択の層設計
     └────────────────────────────────────────────►  - placement 戦略 (#114 に依存)
                        depends on                     - control 分割方向 (上下)
```

- **#109 → #112 / #110**: #109 の観測（世代共有・孤児堆積）が #112 の liveness 修正と #110 の世代識別を動機づけた。#110 [§5](#5-設計論点-4-世代識別と起動時-stale-掃除) が #109 提案「起動時の自 workspace stale 一括掃除」を世代ラベルで実現する。
- **#112 → #110**: #112 は「workspace の世代識別 / stale 一括掃除は #110 に依存」と明記して本書へ委譲。#110 [§5](#5-設計論点-4-世代識別と起動時-stale-掃除) がラベルスキーマ + startup sweep でこれを供給。#112 の `kill_pane_detailed` / `close_workspace(bool)` / defer 意味論は、#110 の owned set close（[§4.1](#41-現行と拡張後--2-つの集合の分離)）と空スペース掃除（[§4.3](#43-空スペース掃除ephemeral-cleanupと-control-スペース除外)、および [§7.4](#74-lazy-作成と-root-pane-の-114-対応) の misplaced-W 掃除）がそのまま再利用する。
- **#114 ↔ #110（本書の中心依存）**: #114 の root cause（`agent.start` focused-workspace 相乗り）は #110 の **前提 P1 を崩す**。#114 の最小修正（実配置 rebind）は **liveness を回復するが multi-space を供給しない**（[§7.1](#71-なぜ-rebind-only-では-110-が解けないか)）。**#110 の multi-space は決定的 per-workspace 配置を要求し、それは #114 が確立すべき「Herdr の workspace 尊重（戦略 A）」または「spawn-then-move（戦略 C）」に依存する**。#114 修正実装（herdr-misreap-fix ワーカーが並行担当）は **placement 戦略 A または C のいずれかを実測（probe 6a/6c）に基づき選択し、加えて degraded-list と pane 不在の区別（本書 [§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版) 相当、"Fix-D"）を必須**とする方針である。**#110 の設計は placement 方式が A / C のどちらに決まっても成立する** — [§6](#6-設計論点-2-spawn-時-workspace-選択入力の層設計) の SpaceDescriptor → workspace 解決、[§4.1](#41-現行と拡張後--2-つの集合の分離) の 2 集合分離 + self-ownership ゲート、[§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版)/[§4.3](#43-空スペース掃除ephemeral-cleanupと-control-スペース除外) の状態機械は **placement 戦略に非依存**であり、#114 fix が供給する「決定的に狙った workspace へ置ける手段（A なら `agent.start {workspace}`、C なら着地後 `pane.move`）」を消費するだけである（C の場合は move 後の verify 再実行 + bind re-key を [§7.3](#73-戦略と-degrade-ladder) / probe 6c に従って行う）。ordering: (1) #114 の liveness 修正（rebind + Fix-D）が Blocker 先行、(2) #110 [§5](#5-設計論点-4-世代識別と起動時-stale-掃除)（世代識別 / sweep）は placement 決定性に非依存なので先行可能、(3) #110 の multi-space 配置（[§6](#6-設計論点-2-spawn-時-workspace-選択入力の層設計) / [§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処)）は #114 fix が確定する A / C の決定的配置手段の上に載る。
- **#114 修正 3 → #110 [§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版)**: 「degraded list と pane 不在の区別」を owned set の workspace 単位状態へ拡張し、単一 workspace の `workspace_not_found` が org 全体の liveness を落とさないようにする。

---

## 13. 段階的導入計画

[`docs/design/herdr-adapter.md`](./herdr-adapter.md) §7 の Phase 体系（H0 probe / H1 messaging / H2 full backend）に、レイアウト固有の Phase L を重ねる。いずれも **claude-org-runtime 側のフォーク**で実証してから runtime 本体へ取り込む（本 transport-lab フォークは design SoT であり実装は持たない、[§1.3](#13-本書のスコープと非スコープ) / preamble）。

- **Phase L0: 配置 probe + #114 liveness 先行**: probe 6（[§11](#11-capability-probe-placement-probe-6)）で配置決定性を確定し、#114 の liveness 修正（rebind + degraded/pane 区別）が landed していることを前提化する。完了判定: probe 6a-6e の実測が出て、[§7.3](#73-戦略と-degrade-ladder) の配置戦略が確定すること。
- **Phase L1: 世代識別 + owned set + startup sweep**: [§5](#5-設計論点-4-世代識別と起動時-stale-掃除) のラベルスキーマ・startup sweep、[§4](#4-設計論点-1-isolation-境界の集合化) の owned set 境界 / degraded 区別を実装。**配置決定性に非依存**（single-focused-workspace 下でも孤児掃除の価値がある）ため L2 に先行できる。完了判定: daemon 再起動で旧世代 workspace が他 org を巻き込まず掃除され、単一 workspace の `workspace_not_found` が mass false-reap を誘発しないこと。
- **Phase L2: multi-space 配置**: [§6](#6-設計論点-2-spawn-時-workspace-選択入力の層設計) の spawn 時 space 選択の層（delegate → broker → adapter）、[§7](#7-設計論点-placement-agentstart-の-workspace-無視への対処) の配置戦略、control / project スペース分離を実装。**probe 6 の配置決定性成立にゲート**される。完了判定: control スペースに制御系、プロジェクトスペースにワーカーが決定的に配置され、org down で owned set 全 workspace が閉じること。
- **Phase L3: ephemeral 掃除 + 分割方向**: [§7.4](#74-lazy-作成と-root-pane-の-114-対応) の空プロジェクトスペース掃除、[§8](#8-control-スペースの分割方向-上下-herdr-down) の per-space 分割方向（control=上下）。完了判定: ワーカー全 close でプロジェクトスペースが掃除され、control スペースが上下分割になること。
- **本体取り込み（別スコープ）**: Set D **Surface 4.2 amendment**（owned workspace 集合スコープ）+ **Surface 1 amendment**（spawn の optional `space` パラメータ + `supports_space_layout` 能力フラグ、[§6.2](#62-層設計3-層のリレー) / [§10](#10-set-d-契約への影響single-tab-must-の再解釈)）、runtime 実装、herdr-adapter.md 能力表への workspace layout 列追加は claude-org-runtime 側の取り込みスコープ。本フォーク（ja 不可触制約）では実施しない。

---

## 14. 残存リスク / スコープ外

- **配置決定性の未確定（最大の未知）**: probe 6 待ち。戦略 A（Herdr 尊重）は上流依存、B（focus-then-spawn）は headless focus 意味論と人間 focus 競合が未確定、C（pane.move）は cross-workspace 可否が未確定。いずれも不成立なら multi-space は degrade（[§7.3](#73-戦略と-degrade-ladder)）。
- **focus-then-spawn の UX 副作用**: 戦略 B は global focus を変える。detached / headless 運用が常態のため許容範囲だが、人間が attach 中に spawn が走ると視界が飛ぶ。多発する control spawn は org up 時に集中するため影響は起動時に限定される見込みだが要観測。
- **#114 修正本体への依存**: 本レイアウトは #114 の liveness 修正（別ワーカー担当）を前提とする。#114 が rebind-only で止まると multi-space はブロックされる（[§12](#12-依存関係の整理-109-110-112-114)）。
- **project-slug の供給欠落**: delegate が project を渡さない spawn は `project:_unassigned` へ degrade（[§6.2](#62-層設計3-層のリレー)）。cwd からの推定は pattern 依存で脆いため採らない。
- **Set D amendment 未 ratify**: single-tab MUST の再解釈（Surface 4.2）と spawn の `space` パラメータ（Surface 1）は本体取り込み時の別 PR（[§6.2](#62-層設計3-層のリレー) / [§10](#10-set-d-契約への影響single-tab-must-の再解釈)）。
- **state dir 喪失による org_instance_id リセット**: `.state/broker/` が失われる（disk wipe / tmpfs / container 再作成 / state ローテーション）と新 `org_instance_id` が発行され、旧 id ラベルの自 workspace が「別 org」と分類されて sweep 対象から外れ **恒久孤児化**する（#109 の再来）。緩和は「直近 instance-id の記録」or「自 prefix だが未知 instance の workspace を operator 確認下で sweep」する回復経路だが、本書ではリスクとして明示するにとどめ、回復経路の実装は本体スコープ。
- **DEGRADED 有界化のトレードオフ**: [§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版) は degraded を `workspace.list` 突き合わせ + 上限で GONE へ収束させ false-liveness leak を防ぐが、`workspace.list` 自体が lag する極端ケースでは gone 判定が遅れうる（安全側 = false-reap より reap 遅延。PR #112 の設計思想と同じ）。上限値は probe / 実測で調整。
- **rolling / 二重起動時の sweep 安全性**: [§5.3](#53-起動時-stale-掃除startup-sweep) の single-live-daemon lock が前提。lock 機構が無い / 破られると旧世代の生 pane を誤掃除しうるため、lock の実装堅牢性が本レイアウトの安全性に load-bearing。
- **スコープ外**: 実装コード（runtime 側）、#114 liveness 修正本体、events ベース cross-workspace 監視（poll で足りる、[§9](#9-監視とイベントの-multi-workspace-対応)）、Herdr 本体への挙動変更提案の実施、renga / WezTerm / tmux の挙動変更（本ポリシーは Herdr 固有）、worktree / plugin 機能の活用。

---

## 改訂履歴

- 2026-07-03: 初版（design only）。Herdr backend の workspace レイアウトポリシー（control 面 1 スペース + プロジェクト単位ワーカースペース）を固定。Issue #110 の 4 設計論点（isolation 境界の集合化 / spawn 時 workspace 選択入力の層設計 / lazy 作成と空スペース掃除 / 世代識別）を全カバーし、追補フィードバック（control スペースの上下分割）を反映。runtime Issue #114 の観測（`agent.start` の focused-workspace 相乗り）を前提条件 P1 として明示し、決定的配置の充足経路（戦略 A: Herdr 尊重 / B: focus-then-spawn / C: spawn-then-move）と degrade ladder、placement probe（probe 6）を設計論点として固定。#109 / #110 / #112 / #114 の依存関係、Set D single-tab MUST の再解釈（owned workspace 集合スコープへの拡張、amendment は本体取り込み時）を整理。配置決定性は probe 6 の実測で後決めとし断定しない（Refs #27、runtime #110）。
- 2026-07-03: 初版に対する敵対的セルフレビュー（5 観点）で以下を強化 — (1) **close-authority owned set と liveness-tracking set の 2 集合分離 + self-ownership ゲート**（[§4.1](#41-現行と拡張後--2-つの集合の分離) / [§7.3](#73-戦略と-degrade-ladder)）で、verify+rebind が foreign（人間 / 他 org）workspace を close 権限集合へ取り込む isolation 破壊を封鎖、(2) **DEGRADED の有界脱出（GONE 収束）と現行 clear 挙動の supersede**（[§4.2](#42-workspace-単位の状態と-degraded--gone-の区別114-修正-3-の集合版)）、(3) **空スペース掃除機構の明示**（空検知 / LIVE→SWEPT / grace・in-flight ガード / per-space-key lock / control 除外、[§4.3](#43-空スペース掃除ephemeral-cleanupと-control-スペース除外)）、(4) **世代識別の堅牢化**（collision-resistant `org_instance_id` / generation の write-ahead 永続化 / rolling-restart の single-live-daemon lock、[§5](#5-設計論点-4-世代識別と起動時-stale-掃除)）、(5) **戦略 B/C の misplacement remediation と probe 6b/6c/6f 追加**、(6) logical-pane reap 除外の集合版継承、(7) §8 の層切り分け是正（broker direction は adapter に届かず #104 vertical は inert）、(8) Surface 1 amendment の flag 化、citation / overclaim 修正。
