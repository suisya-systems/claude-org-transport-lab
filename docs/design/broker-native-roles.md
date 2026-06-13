# broker-native な役割要素の設計再導出 — 受信挙動層（#16 pull-first → #18 push 一次 + pull フォールバック）

<!-- 旧題: 「push→pull の挙動層」。#18（§9）で配送モデルを push 一次へ再反転したため改題。 -->


> **status / 位置付け**: design only。Epic #6（renga 依存解消 / Plan B）の挙動層設計。`docs/design/ja-migration-plan.md` の **§5.2(ii)（静的 prose の両系併記）** と **§8 Issue E（ja prose + 契約改訂）** が「受信モデル（push→pull）の prose を両系併記する」と宣言した、その **prose の中身（受信 cadence と役割セマンティクス）を再導出する** 文書。§5 は ja 改変を「1 flag + 1 生成系シーム」に閉じる *静的シーム* の SoT であり、本書はそのシームを通過する *挙動* の SoT。両者は **概ね直交** する（例外: §6.3 D1 の descriptor フィールド追加だけは §5.2(i) 静的シームに接する。§7 で整合と反対仮説の反証を明記）。
>
> **入力**: (1) transport-lab Issue #16 本文、(2) suisya-systems/claude-org-ja#515 の dogfood 実走観測コメント（2026-06-13、flag=broker で委譲サイクル実走中に観測された defect 1〜4 + transport 非依存の #5→ja#554）、(3) 窓口追加観測（2026-06-13）: broker tmux adapter の key 語彙制限（Escape 不可）による **介入層 defect**（§3.5）。
>
> **不可触制約**: 本タスクは設計のみ。production claude-org-ja / runtime 挙動 / GitHub への書込は行わない。本書は transport-lab worktree 内の設計 doc 追加と、`ja-migration-plan.md` への相互参照ポインタ追記に閉じる。ja への実反映は §6 の変更一覧として分解し、人間ゲート後に窓口/ユーザー判断で行う。
>
> ---
>
> ⚠️ **配送モデルの方向反転（2026-06-13 追補、Issue #18）— 本書を読む前に**: 本書 §1〜§8 は #16 の枠組み（**pull-first cadence を正準・nudge を任意 accelerator**）で書かれている。その後ユーザー判断（ja#515 dogfood レビュー）と決定的 prior art（happy-ryo/claude-peers-mcp の `claude/channel` パターン）を受け、**配送モデルを「push 一次（claude/channel）+ pull フォールバック」へ反転**した。**§9（push 一次配送への再設計）が現行の SoT** であり、**§2 / §3.1 の pull-first cadence は『正準』から『フォールバック層』へ降格**する（push mode 失効時・channel 非対応エージェント向けの保険）。§1〜§8 を読むときは §9.6 の読み替え規定を併せて参照すること。§9 は §1〜§8 を**撤回しない**（pull cadence の役割別設計はフォールバック層としてそのまま生きる）が、**どちらが一次か**を反転する。

---

## 1. 根因 — なぜ機械置換で挙動層が埋まらないか

ja の broker 対応は `mcp__renga-peers__*` → `mcp__org-broker__*` の **完全修飾名の機械置換** で実装された。API 形状（引数形・セマンティクス）は満たすが、配送 *モデル* が異なる:

| | renga（push） | broker（pull） |
|---|---|---|
| 配送契機 | メッセージは `<channel source="renga-peers" …>` として **in-band push**。**idle セッションを起こし**、本文をエージェントのターンへ注入する | broker は **pane-local なナッジ（out-of-band 信号）を出すだけ**。本文はキューに滞留し、`check_messages` で **pull 取得** する。**idle セッションは起きない**・ナッジは agent ターンへ注入されない（broker の受信は構造的に pull。renga の `receive_mode` 出力フィールドは broker では定数 `"poll"` 化が提案中 — backend-interface-contract.md §8.8 amendment / §6.3 D1。spike コードに既存の `receive_mode` 定数は無く未実装） |
| 役割 prose の前提イディオム | 「メッセージを受けたら即応答」「受信したら最初に ack」「ナッジを見たら check_messages」 — いずれも **push が起こす『受けたら / 見たら』契機** に依存 | その契機が **構造的に存在しない**。idle と idle の間に誰もセッションを再起動しない |

**核心**: renga の役割 prose は「**push が受信契機を生む**」ことを暗黙の前提にしている。broker ではこの前提が崩れるため、`受けたら → 即応答` という形のイディオムは **すべて不活性化** する。これが dogfood defect 1〜3 の共通根因である。

> **重要な発見 — 第 1 次 prose pass は push 形を broker 枝へ持ち込んでいた**: §5.2(ii) を受けて既に land 済みの broker 枝 prose（`.claude/skills/org-delegate/SKILL.md` L42、`.claude/skills/org-start/SKILL.md` L56）は **「ナッジを見たら `check_messages`」** と書いている。これは「ナッジが観測可能な in-pane イベントを生む」という **push 形の残留仮定** であり、dogfood で「ナッジは idle セッションを起こさない（=『見る』契機が生まれない）」が観測されたことで **誤りと判明** した。本書の第一の修正対象はこの既存 broker 枝 prose の **pull-first 化** である（§6 表 P1/P3a）。

**設計軸**: 受信契機を *push 配送イベント* から *エージェント主導の poll* へ移す。全役割の受信を **pull 一次（pull-first）= 自身が所有する cadence で能動 `check_messages`** に再定義する。broker の受信は構造的に pull だが、これを **役割 prose の挙動として明示** する（pull-first cadence）。`receive_mode="poll"` 自体は既存定数ではなく、§6.3 D1 で descriptor へ加算する Set D amendment 提案である（drift 防止のための定数化）。

---

## 2. 受信モデルの一次再設計 — pull-first cadence（役割別）

> **§9 による再位置付け（Issue #18）**: 本節の pull-first cadence は **#18 で『フォールバック層』に降格** した（push mode 失効時に自動発動する保険）。push mode（healthy な channel sidecar が登録・heartbeat 中）では、各役割の受信契機は **claude/channel push が一次**であり、本節の cadence はその一次が効かない場合の degrade 経路として読む。役割別の読み替え（worker / dispatcher / secretary それぞれで何が一次・何がフォールバックか）は **§9.6** を一次参照。本節の設計自体（誰が・いつ・どの粒度で `check_messages` するか）は **フォールバック層としてそのまま有効**。

全役割に **受信 cadence**（誰が・いつ・どの粒度で `check_messages` するか）を定義する。renga 枝（push 前提）は不変のまま、broker 枝にこの cadence を追加する（両系併記、§5.2(ii)）。

| 役割 | renga 枝（不変） | broker 枝（本書で再導出） | cadence の所有者 |
|---|---|---|---|
| **worker** | 進捗/ack/レビュー指摘/SUSPEND は in-band push。idle 待機で受信 | **2 フェーズ**: (a-1) **タスク実行中**は新規指示ターンの**冒頭**で 1 回 poll（ターン境界 poll）。(a-2) **完了報告（PR 作成）を送信したターンの終端アクションとして** `/loop Nm <review-watch>` を **実 invoke** して終える（自己宛 prose 指示ではなく実行）。loop body は `check_messages` でレビュー指摘/クローズ指示を pull し、クローズ指示受領で `/loop` 停止。**これは §3.3 の dispatcher /loop 実発火と同型** — 完了報告後 worker は idle に落ち push も来ないため、単発 poll では「完了報告→idle→レビュー指摘がキュー滞留」という **defect 3 が worker に再発** する。harness が再入をスケジュールする bounded /loop でのみ受信契機が生まれる | worker 自身（**タスク実行中**は長ターンを回すため /loop 不適 → ターン境界 poll。**完了後レビュー待機フェーズ**は長ターンを回さないため bounded `/loop Nm` を実発火） |
| **dispatcher** | poll_events + in-band push の混在。/loop 3m | **/loop 3m の各サイクルに `check_messages` を第一級ステップとして組込む**（DELEGATE 受領）。pane lifecycle の `poll_events` と並置。/loop は **dispatcher boot エントリ（`.dispatcher/CLAUDE.md`）末尾で実発火**（defect 3） | dispatcher の /loop 3m（既存常駐ループに受信を載せる） |
| **secretary** | worker 完了/dispatcher DELEGATED が in-band push。「受信したら最初に ack」（#312） | **ターン冒頭の必須 poll**（B1）+ **attention watcher のキュー監視拡張**（B2）。/loop 常駐は **採らない**（人間対話の応答性を blocking しないため） | 二層: B1=secretary 自身のターン規約 / B2=attention sidecar |

**cadence は役割で非対称**であることが要点。dispatcher は既に /loop 常駐なので受信を載せるだけ。worker は **タスク実行中はターン境界 poll、完了後レビュー待機フェーズは bounded `/loop Nm` を実発火**（フェーズで使い分ける — idle になる待機フェーズは /loop でしか受信契機が作れない）。secretary は人間対話主体なので /loop で blocking できず、ターン規約 + sidecar の二層で受信 gap を埋める。

---

## 3. defect 別 設計判断

### 3.1 defect 1 — ナッジが Claude セッションを起こさない（nudge 仕様判断）

**観測**: dispatcher 宛 DELEGATE がキュー滞留したまま idle。`send_keys` で「check_messages して」と打鍵するまで未処理（数分の監視 gap）。

**論点（Issue #16 の明示課題）**: nudge を「打鍵（実プロンプト投入）への昇格で push 同等にする」か「廃止して poll 一本化」か。

| 選択肢 | 内容 | 長所 | 短所 |
|---|---|---|---|
| **A. 打鍵昇格** | broker/adapter がナッジを target pane の tty への `send_keys`（例: 「check_messages して」+ Enter）として実投入し、push 同等に session を起こす | 低遅延・push パリティ | **割り込み副作用**（mid-turn / 人間 IME compose 中への注入はターン破壊・確定文字列破壊）・adapter が target tty と send_keys 権限を要する・新たな故障面 `nudge_failed`・**push 結合の再導入**（backend 固有化、§6.3 reconcile が排したのと同じ轍） |
| **B. 廃止して poll 一本化** | ナッジを使わず、全役割が所有 cadence で `check_messages` を能動 poll | backend 中立・割り込み副作用ゼロ・`receive_mode="poll"` と整合・最小のメンタルモデル | 遅延 = poll 間隔（dispatcher 3m。Set D Q9 best-effort 許容済 §6.3）・監視 gap = 最大 1 cadence |
| **C. pull 正準 + 打鍵 accelerator（defer）** | **poll を正しさの SoT** に据え（全役割が cadence で poll）、打鍵ナッジを **任意の低遅延 accelerator**（adapter が対応し target が割り込み安全な場合のみ）として後付け。accelerator 故障/不在なら poll に degrade（依然 correct） | B の中立性 + 必要時の低遅延・正しさは poll が担保 | accelerator 実装分の複雑性（ただし任意・後追い可） |

**推奨 — C（ただし accelerator は defer）。** これは **§6.3 が event 取得で下した判断（差分 reconcile を正準 + tmux hooks を任意 accelerator）と構造的に同型** である。poll が backend 中立で割り込み hazard が無い受信の正準路、打鍵ナッジは安全な場合のみの遅延短縮。3m cadence の監視 gap が実運用で不足と判明した時に初めて着手する（YAGNI、event accelerator F と同様 defer）。**baseline は B（poll 一本化）**。

- **重要な但し書き**: 打鍵 accelerator は **secretary に対しては原理的に不可**（人間 IME compose を破壊する）。よって defect 2 の答えには使えない（§3.2）。accelerator は dispatcher/worker など「割り込んでよい論理ペイン」に限定。
- **却下した A 単独採用の理由**: backend 固有の push 結合を broker 全体に再導入し、§6.3 が移植性のために排した control-mode 主軸と同じ過ちになる。割り込み副作用の常時露出も対話 TUI 前提（課金中立）と相性が悪い。

### 3.2 defect 2 — secretary はナッジの着地点が無い（受信ループ設計）

**観測**: 窓口は adapter 実体の無い論理ペインのため、worker 完了報告・dispatcher の DELEGATED 報告が **手動 poll するまで不可視**。「受信したら最初に ack」（#312）が push 前提で、broker では受信契機そのものが無い。

secretary は **人間対話主体** で、応答性を保つため **blocking な /loop は採れない**。二つの sub-problem（(i) nudge 着地点が無い、(ii) #312 の即 ack が push 前提）に対し:

| 選択肢 | 内容 | 評価 |
|---|---|---|
| **B1. ターン冒頭の必須 poll** | secretary は **各ターンの冒頭**（人間がターンを与えるたび、他の作業の前）で `check_messages` を呼ぶ規約。着信あれば即 ack | **採用（baseline）**。インフラ不要。人間が関与している間は「1 対話 gap より古い未読」を構造的に作らない。#312 の「即 ack」は意味を保存 — ack 契機が *push* から *poll 発見* に移るだけで、発見後 即 ack は不変（dead-lock 防止も維持） |
| **B2. attention watcher のキュー監視拡張** | 既存の attention sidecar（`org-attention-start` / `claude-org-runtime attention watch`、承認待ち/CI 失敗を OS 通知+音で能動通知）を **secretary の broker キュー poll** に拡張。worker 完了 / dispatcher DELEGATED / escalation 着信で attention を上げる | **採用（active-signal 層）**。**sidecar 配置形態（pane 常駐・通知 backend・dedup）は既存を再利用するが、watcher の入力経路は新設**: 現行 watcher の reader（`attention/readers.py`）は state.db events と pending_decisions.json しか読まず、メッセージキュー read 経路を持たない。よって S2 は readers.py への broker-queue poll source 追加 + secretary read-scope token ハンドリングの **net-new runtime 実装**（sidecar 骨格の再利用 ≠ queue read 機能の再利用）。人間不在 gap（B1 が発火しない時間帯）を能動通知で埋める。secretary 本体を起こさず *人間* に信号を出す（secretary は論理ペインのままでよい） |
| B3. secretary に実 adapter ペインを与える | ナッジが着地するよう窓口を実ペイン化 | **却下**。窓口は人間 IME 入力ペイン — 打鍵ナッジは compose 破壊。かつ実ペイン化しても agent ターンへ注入されないので解決にならない |

**推奨 — B1 + B2 の二層。** B1（ターン冒頭必須 poll）が **baseline 規約**: 人間関与中は受信新鮮性を保証し、「受信したら最初に ack」を **「ターン冒頭で check_messages → 着信あれば即 ack」** に置換する。B2（attention sidecar のキュー拡張）が **active-signal 層**: 人間不在 gap を既存通知機構で能動的に埋める。両者で「人間在席」(B1=agent ack) と「人間不在だが通知すべき」(B2=人間ページング) を被覆。#312 contract の **状態遷移条件**（受信→ack）は不変（手順レイヤの受信契機差し替えのみ）。

> **B2 の被覆範囲の明示（accept 済の設計点）**: B2 は *通知のみ* で secretary **agent 自身は起こさない**（§1 / B3 と同じ前提 — broker は idle agent を起こさない）。よって人間不在中は agent レベルの ack は発火せず、secretary の ack 遅延は「次に人間がターンを与えるまで」に bound される（そこで B1 が発火）。これは #312 の遷移条件を変えず受信契機を差し替えるだけであり、#312 の dead-lock 防止意図（worker を idle 放置しない）は **dispatcher 側の機械観測**（worker-monitoring の「人間運用契約 + 機械観測の二重化」）と人間ページング（B2 が拡張する attention 機構）で既に二重化されている前提を broker 枝でも踏襲する。「人間不在中も即時 agent ack」という要求は #312 には無く、本設計はそれを満たさないことを既知の設計点として明記する。

> B2 の実装は attention watcher 側の拡張（runtime / sidecar 層）。secretary キューの token を watcher が読めるよう、watcher 起動時に secretary 用 read scope の broker token を渡す（既存 attention.json テンプレに transport-aware なキュー source を 1 つ足す）。詳細仕様は §6 表 S2 / runtime Issue 行で分解。

### 3.3 defect 3 — dispatcher が監視ループを自己開始せず停止

**観測**: spawn-flow 完了後「次は /loop 3m で監視ループを開始してください」と **自己指示を出して idle**。renga の in-band push を前提にした運用 prompt が pull 環境で機能しない。

**根因**: /loop の起動が「dispatcher が自分宛に出す *prose の自己指示*」に依存していた。renga では次イベントの in-band push がループへ再入させるため自己指示の後でも動くが、pull 環境では idle の後に誰も session を再起動しないため、自己指示は **実行されずに stop** する。

**設計**: 監視 /loop を **dispatcher の boot/監視エントリの終端アクションとして実発火** する（自己宛指示ではなく)。具体的には **`.dispatcher/CLAUDE.md` L121/L134**（「最初のワーカー派遣完了後、`/loop 3m` で監視ループを開始する」自己開始 prose、および参照先 `.dispatcher/references/worker-monitoring.md`）を、印字する自己指示から、**最初のワーカー派遣完了の末尾で dispatcher 自身が `/loop 3m <monitor>` を実際に invoke して終える** アクションへ差し替える（/loop skill が harness 経由で再入をスケジュールするため、idle を跨いで生存する）。

- 監視ループ body には `check_messages`（DELEGATE 受領、defect 1 の poll baseline）を `poll_events`（pane lifecycle）と **並置** する。
- 再起動経路（dispatcher-resume）は既に `/loop 3m` を再 invoke しており **正常**（SKILL.md Step 5）。本 defect の gap は **初回 spawn 経路のみ**。よって修正は **`.dispatcher/CLAUDE.md` L121/L134（+ worker-monitoring.md）の 1 点**に閉じる。
- **対象の検証**: `/loop` の正本は dispatcher 自身の prose であり org-start には存在しない（`grep -c "loop 3m" .claude/skills/org-start/SKILL.md` = 0; org-start Block D は **secretary が dispatcher ペインを spawn/greet し identity を DB に記録する secretary 文脈**で、末尾は Block D-6 の JSON snapshot 再生成。/loop は持たない）。secretary は dispatcher の Claude session 内で `/loop` を invoke できない（自セッションで invoke するのは dispatcher のみ）ため、編集対象は **org-start ではなく dispatcher prose**。org-start L56 admonition の pull-first 化（defect 1）は別件で P3a が担当する。

### 3.4 defect 4 — tmux アダプタの観察性（独立 tmux セッション問題）

**観測**: tmux アダプタは split を「ペイン毎の独立 tmux セッション」として実装。人間が attach しても 1 ペインしか見えず、`Ctrl-b s` のセッション一覧で切替が必要。

**設計**: tmux アダプタを **単一 tmux セッション内の複数ペイン/ウィンドウ** 構成へ再構成する。例: セッション `claude-org` を 1 つ持ち、論理グループ毎に window、または 1 window 内を split。これにより **`tmux attach -t claude-org` 一発で org 全体が見え**、標準ペイン操作（`Ctrl-b` 矢印）が効く。

| トレードオフ | 単一セッション（推奨） | 独立セッション（現状） |
|---|---|---|
| 人間観察性 | ◎ attach 一発で全体・標準 nav | ✗ 1 ペインのみ・`Ctrl-b s` 切替 |
| 障害分離 | session 級障害が全ペインに波及 | ペイン単位で隔離 |
| 緩和 | ペイン死は **差分 reconcile（§6.3）が既に処理**。session 級障害は稀で、観察性の常時便益が上回る | — |

- **attach 導線** を runbook に明記（`tmux attach -t claude-org`、detached 起動でも後から観察可能）。
- これは **adapter（runtime）層** の変更で、design-only の本書ではスコープ外実装。§8 Issue A（`claude_org_runtime/terminal/` への terminal 抽出）に **adapter のセッション構成方針** として載せる（§6 表 R1）。
- dogfood で knowledge/raw に記録済の視認性トレードオフ（「detached spawn で常時視認が消える ↔ 画面サイズからの解放」）と整合: 単一セッション化は **常時視認を強制せず attach 時の観察性を回復** する（detached の画面解放は維持）。

### 3.5 追加 defect（介入層）— broker tmux adapter の key 語彙制限で Escape 介入が実行不能

**観測（窓口 2026-06-13 追加）**: broker の tmux アダプタは `send_keys` の raw キー語彙が限定されており、**Escape が送れない**（`[key_unsupported]`。**Enter / Ctrl+C / literal text のみ**サポート、full 語彙は Phase 4 / full backend adapter とのエラーメッセージ）。これにより org-delegate の **worker 介入手順**（`org-delegate/SKILL.md` L326: `inspect_pane` で深掘り確認 → **`send_keys(keys=["Escape"])` で中断** → tight な修正指示送信）が broker では **実行不能**。Shift+Tab（permission mode 切替、`renga-error-codes.md` L172）も同様に未サポートの隣接 gap。

**根因**: defect 1〜3 とは別系統 — push→pull ではなく **adapter primitive の語彙不足**（renga-decoupling.md §4.2 の「adapter 面を薄く保つ」方針 / §7.4 Phase 4 full backend adapter への意図的 defer）と、**Escape ベースの介入手順が hard-depend する renga 役割 prose** の衝突。ja-migration-plan §3.2-4「構造的相違・欠落（設計判断が要る）」の介入版。

**Ctrl+C 安全性評価（窓口の明示要求）**: Claude Code TUI のキー意味論で **Escape と Ctrl+C は等価でない**:

| キー | 意味論 | 介入用途での安全性 |
|---|---|---|
| **Escape**（renga 介入の正準） | 生成中ターンを **graceful に中断**、コンテキスト・入力を保持してプロンプトへ復帰 | ◎ 「暴走を止めて指示し直す」に最適。冪等的に再送可 |
| **Ctrl+C**（broker でサポート） | 単発で現行入力/生成を中断するが、**2 回連続で Claude が exit**（= worker session 喪失）。入力欄が非空なら生成中断ではなく入力 clear になりうる | △ **非冪等**（ナッジの冪等性契約と真逆）。再送/二重打鍵が **session 破壊**。意味が Escape より粗い |

→ **Ctrl+C は「緊急停止」としては使えるが、Escape の drop-in 代替にはならない**（非冪等・session 破壊リスク・粒度差）。

**設計（2 horizon）**:

1. **正準（推奨）— broker tmux adapter の key 語彙に Escape（+ Shift+Tab）を追加**: tmux は `send-keys Escape` を**ネイティブにサポート**するため、これは小さな adapter 補完で、renga の Escape ベース介入手順を **drop-in 不変** にできる（§3.3 の「形寄せ / drop-in」哲学・renga-decoupling.md §3.1 の surface parity と整合。Ctrl+C hazard を完全回避）。**Issue A（terminal adapter）+ Issue C（surface parity）** に載せる。Phase 4 full backend adapter の語彙拡張を、介入に必要な Escape/Shift+Tab に限って前倒しする位置づけ。
2. **暫定 fallback（語彙拡張前）— gated single Ctrl+C + pull-native idle redirect**:
   - **暴走生成の緊急停止**: `inspect_pane`/grid scrape で worker が **実際に生成中（スピナー有・shell プロンプトでない）** を pre-check → **Ctrl+C を厳密に 1 回だけ**送信（**再送・二重打鍵を adapter/retry 層が構造的に禁止** — ナッジと逆で Ctrl+C は非冪等）→ post-check で **session 生存**（pane 登録維持・プロンプト復帰・shell へ落ちていない）を確認、死んでいれば **人間へエスカレート**（session 喪失は respawn + 再委譲が必要・自動回復不可）→ tight 修正指示を literal text + Enter で送る。
   - **非暴走（idle）redirect**: worker がターン間 idle なら中断不要。修正指示の届け方は 2 経路を区別する: **(正準) pull** — `send_message` で broker queue に積み、worker が次のターン境界 poll（§2 worker (a-1)）で受領（PTY を使わない・poll 正準を保つ）。**(打鍵起こし、任意)** — idle worker を即時に動かしたい場合のみ、最小の literal text + Enter（語彙内の PTY push）で `check_messages` 起動を打鍵する（これは §3.1-C の打鍵 accelerator と同類で、idle worker は割り込み安全。**指示本文は PTY に流さず queue に積む** — 本文 PTY 混線を避ける §4.3 の原則）。Escape も Ctrl+C も使わない。

**既定 renga 不変**: renga の send_keys は Escape/Shift+Tab を従来どおり持つ（語彙不変）。本 defect の対処はすべて **broker 枝・adapter 加算**で、renga 介入手順 prose は不変（broker 枝に「Escape→語彙追加後 drop-in / 暫定は gated Ctrl+C」の条件分岐を併記）。

---

## 4. 既定 renga 経路の不変性（設計保証）

受け入れ条件「既定 renga 経路の不変性」を以下で構造保証する:

1. **すべて broker 枝に閉じる**: §2 の pull cadence、§3.1 の poll baseline、§3.3 の /loop 実発火 prose（`.dispatcher/CLAUDE.md` 監視エントリの broker 枝）、§2 worker の完了後 review-watch /loop、§3.2 B1 のターン冒頭 poll は、いずれも **`ORG_TRANSPORT=broker` 条件下の broker 枝**（§5.2(ii) 両系併記の broker 側）に書く。renga 枝の「in-band push → 即応答」prose は **一字も変えない**。
2. **加算のみ**: §3.2 B2（attention sidecar のキュー拡張）・§3.4（tmux 単一セッション）・§3.5（adapter key 語彙への Escape 追加 / 介入 prose の broker 枝）は **broker transport 選択時のみ作動する加算**。renga 時は watcher も adapter も現状経路、renga の send_keys は Escape/Shift+Tab を従来どおり保持。renga ツール（`mcp__renga-peers__*`）は 1 つも失われない。
3. **flag 既定は不変**: 本書は **挙動層の prose/cadence のみ** を扱い、`ORG_TRANSPORT` の既定値（移行期=renga、§5.1）は **変えない**。既定反転は §8 Issue G ゲート後の人間判断のままで、本書はそれを前倒ししない。
4. **切戻し忠実性**: §1〜§8 の挙動層変更（prose / cadence のみ）は §5.5 の切戻し 5 条件を増やさない。broker 枝 prose は flag=renga 再生成時に renga 枝へ戻るだけ（生成系シーム §5.2(i) が両系を render するため、prose の broker 枝は flag で非選択になる）。
   - **§9（push 一次）による補正（Issue #18）**: §9 の channel sidecar は **新規 live process**、daemon の delivery_mode / CLAIMED ライフサイクルは **新規 per-agent state** であり、これらは prose ではなく runtime 実体のため **切戻しドリルは増える**。よって「5 条件は増えない」は §9 適用下では **本書 §1〜§8 の prose 変更に限った主張**へ縮小し、§9 は **第 6 サブステップ**（per-pane channel sidecar の SIGTERM/unregister + 当該 agent の delivery_mode reset + delivery-scoped credential を §5.5 切戻し条件 (3) active ペイン respawn / (4) daemon 停止順序 / (5) token・queue 破棄の列へ enroll）を §5.5 に畳み込むことを要求する（§9.7）。**rollback drill は sidecar-reap サブステップを獲得するが、依然 bounded・flag-gated**（renga 経路は不変・第二 dev-channel を一切持たない、§9.7）。

> 検証観点（ja 反映時の golden）: flag=renga で生成される全 prose/settings が **本書の変更前と bit 等価**（§8 Issue D の切戻し忠実性テストに本書由来の broker 枝が混入していないことを含める）。

---

## 5. nudge 仕様判断のサマリ（Issue #16 明示要求への回答）

| 判断項目 | 結論 |
|---|---|
| nudge を打鍵昇格するか / 廃止して poll 一本化か | **pull 一本化を正準（baseline）**。打鍵昇格は **任意 accelerator として defer**（§3.1 選択肢 C）。§6.3 の reconcile 正準 + hooks accelerator と同型 |
| 打鍵 accelerator の適用範囲 | dispatcher/worker 等「割り込んでよい論理ペイン」のみ。**secretary には不可**（人間 IME compose 破壊） |
| 着手条件 | 3m cadence の監視 gap が実運用で不足と実測された時のみ（YAGNI）。Issue 分解では独立・低優先の spike（§6 表 N1） |

---

## 6. ja 反映変更一覧（層別・Issue 別の分解）

受け入れ条件「ja 反映タスクに分解可能な粒度」。各変更を **層**（役割 prose / SKILL / runtime / sidecar / 契約）と **§8 Issue** に対応付ける。**すべて broker 枝・加算で、renga 枝不変**（§4）。

### 6.1 prose / SKILL 層（→ §8 Issue E: ja prose + 契約改訂）

| ID | 対象ファイル | 変更内容 | 由来 defect |
|---|---|---|---|
| **P1** | `.claude/skills/org-delegate/SKILL.md` L42 broker 枝 admonition | **「ナッジを見たら check_messages」→「/loop / ターン境界で check_messages を *能動 poll*」** に修正（push 残留仮定の除去）。worker 受信経路に「**完了報告送信ターンの終端で bounded `/loop Nm <review-watch>` を実 invoke**（check_messages 込み）、クローズ指示で停止」（§2 worker (a-2)）を追記 — P5 と語彙統一 | 1,2 |
| **P2** | `.claude/skills/org-delegate/SKILL.md` Step 5（窓口の ack） | 「進捗/完了報告受信時の ack」を **「ターン冒頭で check_messages → 着信あれば即 ack」**（B1）へ broker 枝で再記述。#312「最初に ack」の意味保存を明記 | 2 |
| **P3a** | `.claude/skills/org-start/SKILL.md` L56 broker 枝 admonition | L56 の「ナッジを見たら」を P1 と同様 pull-first 化（defect 1）。**org-start に /loop は無い**（`grep -c "loop 3m"`=0）ため、defect 3 の /loop 修正は本行ではなく P3b が担当 | 1 |
| **P3b** | `.dispatcher/CLAUDE.md` L121/L134（+ `.dispatcher/references/worker-monitoring.md`） | **dispatcher の `/loop 3m` 自己開始 prose を「印字する自己宛指示」から「最初のワーカー派遣完了の末尾で dispatcher 自身が `/loop 3m` を *実 invoke* して終える」へ差替**（`check_messages`+`poll_events` 並置）。**注: 編集対象は org-start Block D ではない** — Block D は secretary 文脈の spawn/greet で /loop を持たず、secretary は dispatcher session 内で /loop を invoke できない | 3 |
| **P4** | `.claude/skills/org-delegate/references/instruction-template.md` / `worker-claude-template.md`（spawn 後即時復帰の必須文言付近） | 監視ロール brief の「spawn 後即時復帰し監視ループに制御を返す」に、broker 時は **「制御を返す先＝実発火した /loop 3m（check_messages 込み）」** を broker 枝注記として追加 | 3 |
| **P5** | worker brief テンプレ（`worker-claude-template.md` L155「PR 作成後はペインを保持してレビュー指摘待機」） | broker 枝で **「待機＝idle ではなく、完了報告ターン終端で実発火した bounded `/loop Nm`（check_messages 込み）。クローズ指示で /loop 停止」** と明記（§2 worker (a-2)、P1 と語彙統一） | 2 |
| **P6** | `.claude/skills/dispatcher-resume/SKILL.md` Step 5 / `secretary-resume` | resume 経路の /loop 再開 prose に broker 枝注記（dispatcher は既に正常、secretary-resume は B1 ターン冒頭 poll 規約を resume 後も適用）。**初回 spawn の P3b と再開経路の整合確認**（dispatcher 側で /loop 実発火が初回・再開とも対称） | 3 |

### 6.2 sidecar / runtime 層

| ID | 層 | 対象 | 変更内容 | §8 Issue | 由来 |
|---|---|---|---|---|---|
| **S2** | sidecar + runtime | attention watcher（`org-attention-start` SKILL + `claude-org-runtime attention watch` の `attention/readers.py` + `attention.example.json` テンプレ） | **readers.py に broker-queue poll source を新設**（現状 readers.py は state.db events + pending_decisions.json のみ読み、queue read 経路を持たない＝**net-new reader 配線**。sidecar 骨格＝pane 常駐・通知 backend・dedup は再利用）。worker 完了 / dispatcher DELEGATED / escalation 着信で attention を上げる。watcher 起動時に secretary read-scope の broker token を渡す導線。**真に埋める gap = secretary 未処理（state.db 未書込）の着信報告 / escalation / DELEGATED**（処理後 state.db 経路とは一部重複しうる）。transport-aware（renga 時は無効化、加算） | E（prose 起動）+ **Issue H（watcher input 拡張 = readers.py の新 source + token ハンドリング。ja-migration-plan §8 に新設、A の terminal 抽出とは独立）** | 2 |
| **R1** | runtime（terminal adapter） | `claude_org_runtime/terminal/`（tmux adapter） | **tmux アダプタを単一セッション複数ペイン構成へ再構成**（`claude-org` セッション、attach 一発で全体）。attach 導線を runbook 化 | **A**（terminal 抽出） | 4 |
| **R2** | runtime（terminal adapter） | broker tmux adapter の key 語彙（`send_keys` キーマップ） | **key 語彙に `Escape`（+ `Shift+Tab`）を追加**（tmux は `send-keys Escape` をネイティブ対応）。renga の Escape ベース介入手順を drop-in 不変にし Ctrl+C hazard を回避（§3.5 正準。Phase 4 full 語彙のうち介入必須分を前倒し） | **A**（terminal 抽出）+ **C**（surface parity） | 介入層 |
| **P7** | prose | `.claude/skills/org-delegate/SKILL.md` L326 介入手順 + `renga-error-codes.md` broker 節 | 介入手順に broker 枝を併記: **Escape は R2 で語彙追加後 drop-in / 暫定は gated single Ctrl+C**（生成中 pre-check → 厳密 1 回・再送禁止 → session 生存 post-check → 死亡時エスカレート）+ idle redirect は pull 受信で。`renga-error-codes.md` に broker の限定語彙（Enter/Ctrl+C/literal のみ・`[key_unsupported]`）と Ctrl+C 非冪等の注意を追記 | **E** | 介入層 |
| **N1** | runtime（任意・低優先） | broker nudge accelerator | 打鍵ナッジ accelerator の spike（poll 正準のまま同 path に低遅延補助）。**3m gap 不足が実測された時のみ着手**。secretary 除外 | **Issue H（N1 部）**（ja-migration-plan §8、F と同列の独立・低優先） | 1 |

### 6.3 生成系 descriptor 層（→ §8 Issue D: ja 統合シーム）

| ID | 対象 | 変更内容 | 由来 |
|---|---|---|---|
| **D1** | transport surface descriptor（§5.2(i)、runtime） | descriptor に **`receive_mode`（`"push"`/`"poll"`）と役割別 `receive_cadence` ヒント**（worker=turn-boundary+review-watch-loop / dispatcher=loop-3m / secretary=turn-prologue+sidecar）を加算フィールドとして持たせ、両生成器が prose render 時に broker 枝の cadence 文言を descriptor 駆動で出せるようにする（drift 防止）。golden test に追加。**— descriptor の `receive_mode` は新概念ではなく、backend-interface-contract.md §8.8（§1.5/§2.2 amend、broker では定数 `"poll"`、批准待ち）が規定する `list_panes`/`list_peers` 出力フィールド `receive_mode` の *上流 SoT* である**。同一の flag 由来値（renga→`push` / broker→`poll`）が (1) broker 枝 prose の cadence render と (2) 当該出力レコードフィールドの双方へ流れる単一 SoT とし、descriptor を rename しない（批准待ちフィールド名 `receive_mode` を ja 生成器が emit する整合を保つため）。§5.2(i) の「両生成器出力 == descriptor の golden test」に出力フィールド一致も含める | 1,2,3 |

> **粒度の所在**: P1/P2/P3a/P3b/P4/P5/P6/P7 は **prose（Issue E）**、S2 は **prose 起動（E）+ runtime watch 実装（Issue H）**、R1/R2 は **runtime terminal adapter（Issue A、R2 は C surface parity も）**、N1 は **任意 spike（Issue H の N1 部、F 同列）**、D1 は **descriptor（Issue D）**。挙動層の中心質量（受信 cadence prose）は Issue E に集中し、§5.6 の「worker/curator は messaging 4 ツール + 受信モデル prose のみ」という配線替え集中と整合する。ja-migration-plan §8 の Issue 表に A（+R1, +R2）/ C（+R2 parity）/ D（+D1）/ E（+P1-P7, S2 prose）/ H（新設: S2 runtime + N1）として反映済。

---

## 7. ja-migration-plan §5 との整合 / 新規 doc 判断

**比較結論 — 新規 doc（本書）を採用し、§5 へは相互参照ポインタのみ追記。**

| 観点 | §5 増補 | 新規 doc（採用） |
|---|---|---|
| 関心の分離 | §5 は *静的シーム*（flag+生成器+pin）の SoT。受信 cadence/役割セマンティクスは別軸（*挙動*）で、混在すると §5 の「シーム最小化」の焦点がぼける | 挙動層を 1 doc に独立。§5 は静的シームの SoT のまま保てる |
| 分量 | defect 4 件 + 受信モデル一次設計 + nudge 判断は §5.2(ii) の 1 段落に収まらない | 標準的な独立設計 doc 規模（`attention-notification.md` / `core-harness-extraction.md` 等と同列） |
| 既存構造 | §5.2(ii) / §8 Issue E は既に「prose を両系併記する」と *宣言* 済 — 中身を書く場所が必要 | 本書がその「中身」。§5.2(ii)・Issue E から本書を指す |

**§5 側への追記（最小）**: (1) §5.2(ii) に「受信モデル（push→pull）の cadence/役割設計は本書 `broker-native-roles.md` を一次参照」のポインタ、(2) §8 Issue E 完了基準に「本書 §6 の prose 変更一覧を反映」を追加、(3) 改訂履歴 1 行。実 prose の改変は本書 §6 に分解し ja 反映ゲートで実施（§5 は静的シーム SoT のまま）。

**反対仮説の明示と反証**: 本書の中身は相互参照上 §5/§6/§8 ノードの精緻化に見える（§4 不変性は §5.1/§5.5 を、§6 変更一覧は §8 Issue E/D/A・§5.2/§5.3/§5.6 を、§3.1-C は §6.3 と同型を参照）。よって「§5.2(ii).a の小節に畳むべき（単一 SoT 維持・二 doc drift 回避）」という反論が立つ。— 反証: (a) §6.3 D1 を除く実質（受信 cadence の一次設計・nudge 仕様判断・secretary 二層受信ループ・tmux 観察性）は §5.2(ii) が「両系併記する」と *宣言* した **prose の中身** であり、宣言（§5.2(ii)）と中身（本書）を分離するのは §5 の「シーム最小化」焦点を保つために妥当。(b) 唯一静的シームに接する D1 は、設計根拠こそ本書にあるが、ja 反映上の所在は §6.3 で既に Issue D / §5.2(i) descriptor へ割当済であり、本書は SoT を奪っていない。(c) 二 doc drift リスクは、まさに §5.2(i) の descriptor 駆動 + golden test（D1 が載る機構）が構造的に抑止する — 受信 cadence 文言は descriptor から render され、手書き drift の余地を残さない。よって畳み込みではなく「宣言/中身の分離 + D1 のみ descriptor 経由で静的シームへ接続」が整合的。

---

## 8. 未解決・defer

- **N1（打鍵 accelerator）**: defer（§5）。3m cadence gap の実測待ち。
- **secretary キュー token の scope**: B2 で watcher に渡す read-scope token の発行/失効を broker token ライフサイクル（runbook §3）にどう載せるかは runtime Issue（S2）の実装詳細。本書は「read-scope token を 1 つ渡す」方針提示に留める。
- **worker 完了後レビュー待機の no-re-entry gap は閉じた**: 完了報告後 worker が idle に落ちて review/close を取りこぼす failure（defect 3 の worker 再発）は §2 worker (a-2) の bounded `/loop Nm` 実発火で構造的に解消。残課題は **review-watch /loop の `Nm` 値選定**（レビュー到着遅延 vs ペイン占有のトレードオフ）に絞られる。最終 fallback として「/loop Nm 満了で worker が idle 復帰した場合、dispatcher 監視ループが当該 worker の未読 review/close を検知し send_keys で再起動」を degrade 経路に併置してよい。
- **worker turn-boundary poll の取りこぼし（タスク実行中）**: worker が長い単一ターン（大規模 Edit/Bash）の最中に来た SUSPEND は、ターン境界まで観測されない（最大 1 ターン遅延。これは *待機フェーズ* の no-re-entry とは別問題で、ターンが続く実行中フェーズの話）。SUSPEND の緊急性が問題化したら N1 accelerator を worker にも適用する候補（secretary 除外の例外ではない＝worker は割り込み安全）。
- transport 非依存の dogfood defect #5（ultracode brief 許可の不発）は ja#554 で別途扱い、本書スコープ外。

---

## 9. push 一次配送への再設計 — claude/channel 採用（Refs #18）

> **位置付け**: Issue #18（ユーザー判断による方向修正）の設計追補。#16 で本書 §1〜§8 が置いた「**pull-first cadence 正準 + nudge 任意 accelerator**」を、**「push 一次（claude/channel）+ pull フォールバック**」へ反転する。§2 / §3.1 の pull-first cadence は廃止ではなく **フォールバック層**（push mode 失効時・channel 非対応エージェント向け）へ降格する（読み替え規定は §9.6）。一次入力: (1) Issue #18 本文・受け入れ条件、(2) prior art **happy-ryo/claude-peers-mcp**（`broker.ts` / `server.ts` の実コードを照合）、(3) ratified Contract Set D（dev-channel injection は **撤回されていない** = §9.5 で詳述）。**design only**: 本節は設計判断・契約改訂提案・spike ゲート定義に閉じ、実装・production ja・GitHub 書込は行わない。

### 9.1 方向反転の根拠 — なぜ nudge 観測が push 一次と矛盾しないか

- **ユーザー判断**: ja#515 dogfood レビューで「**メッセージは push にすべき。pull の方がフォールバックであるべき**」の方向が示された（Issue #18 背景）。
- **prior art の決定性**: claude-peers-mcp は「**中央 broker daemon（localhost HTTP + SQLite）+ セッション毎の MCP サイドカー**」構成で、サイドカーが broker を 1 秒間隔で poll し、受信を **`claude/channel` プロトコル**（`notifications/claude/channel`）でセッションへ即時注入する。すなわち **配管層 = 高頻度 poll / モデル層 = push**。idle セッションも channel notification で起きる。
- **#16 の nudge 観測との関係（重要）**: §3.1 は「nudge（PTY 打鍵）は idle セッションを起こさない」を dogfood で観測し、それゆえ pull を正準に置いた。#18 はこの観測を **否定しない** — 否定するのは「push の*手段*」だけである。**PTY nudge は idle を起こさないが、`claude/channel` notification は idle を起こす**（harness がプロトコルとして受信をターンへ注入するため）。push の正準手段が「PTY 打鍵」から「claude/channel」へ替わったことで、#16 が pull に退避した根因（「broker には push 契機が構造的に無い」§1）が **構造的に解消**する。よって #16 の挙動層分析（cadence の役割非対称性・no-re-entry gap 等）はフォールバック層の設計としてそのまま生き、一次層だけが pull→push に戻る。

### 9.2 採用アーキテクチャ — daemon（権威）+ per-session channel sidecar（配送）

**設計 β を採用**（α 却下、後述）。所有境界を 2 層に分ける:

- **org-broker daemon（権威層・据え置き）**: 現 broker MCP サーバー（localhost HTTP、`--mcp-config` で consume）を **そのまま権威**として残す。全 13 ツール面・queue store（`.state/broker/`）・per-agent token・role tier・**帰属（token 由来 `from_id`・なりすまし不可）**・pane 操作実行・argv allowlist guard はすべて daemon が保持。FQ ツール名 `mcp__org-broker__*` は **不変**（drop-in 維持）。**「据え置き」が指すのはこのツール権威/store/tier/帰属の層のみ** — daemon の**配送ライフサイクル**（delivery_mode・三状態・claim/confirm endpoint）は §9.3 / §9.9 R4 で **加算**する（§9.7 の「daemon UNCHANGED ではない」補正を参照）。
- **channel sidecar（配送層・加算）**: 同一ペインに spawn される薄い **stdio MCP サーバー**（名 `org-broker-channel`）。`experimental: { "claude/channel": {} }` を宣言し、**delivery-scoped credential のみ保持**（§9.4）、**~1 秒の claim→push ループ**（§9.3）を回す。dev-channel flag（§9.5）はこの sidecar を指す。
- **ownership boundary（一文）**: **daemon = 単一のツール権威・store・帰属。channel sidecar = per-session の配送トランスデューサで、delivery-scoped credential 1 つだけを持ち、queue→`claude/channel` の変換に**専従する。sidecar は **droppable**: 落ちれば当該 agent は pull フォールバック（§9.6）へ自動 degrade する。

**α（sidecar が全ツールを proxy する collapse 案）却下**: per-session stdio sidecar を **エージェント対面の org-broker MCP サーバー本体**にして全ツールを daemon へ proxy する案（claude-peers の `server.ts` 形）。却下理由: (i) 既に批准方向の `--mcp-config` エージェント transport を **全ツールについて置換**する大改変、(ii) sidecar が **load-bearing 化**し落とせない（切戻し悪化）、(iii) 「push 一次 / pull フォールバック」の layering が不明瞭化。**α の唯一の実利（second credential を持たずに済む）は、§9.4 の delivery-scoped token で β が回収する** — daemon を単一のツール権威に保ち、sidecar を droppable に保ったまま α と同等の credential 分離を得る。よって β は α より厳密に層が綺麗。

### 9.3 配送ライフサイクル — daemon 所有の三状態（at-most-once / at-least-once の正準）

**β の中核**。prior art の単一 `delivered` boolean を**そのまま流用すると lost-message window が開く**ため、daemon 所有の **三状態ライフサイクル**に置き換える。

**lost-message window（流用すると起きる欠陥）**: claude-peers の `broker.ts` `handlePollMessages`（L266-275）は **drain 時点で `delivered=1`** をマークし、`server.ts` `pollAndPushMessages` は HTTP 往復が返った **後**に `mcp.notification`（L425→L447）を emit する。マークと emit が **daemon/sidecar 境界をまたぐ**ため原子化できず、sidecar が両者の間で死ぬ（ペイン kill / stdio pipe 断 / notification throw）と **「配達済みだがモデルに届いていない」**メッセージが残る。Contract Surface 2.3（「successful drain 後は再配達しない」）の下で、これは **沈黙の永久喪失**になり、フォールバックの `check_messages` は空を返す。spike 側も `broker.py` L458 が「**queue は agent_id 単位の inbox なので二重 spawn は message 横取りを生む**」と同根のハザードを警告している。

**設計 — 三状態 + claim-then-confirm**:

| 状態 | 意味 | 遷移 |
|---|---|---|
| `UNDELIVERED` | 投入済み・未配達 | 初期。`send_message` が投入 |
| `CLAIMED(lease, owner, epoch)` | ある drainer がリースで占有中（配達試行中） | drainer が claim。`owner`=drainer の credential、`epoch`=delivery_mode 世代、`lease`=期限 |
| `DELIVERED` | 配達確定（再配達しない） | `/confirm-delivered(id)` 受領で確定 |

- **sidecar push = claim-then-confirm**: poll は `/poll-claims`（**claim-with-lease**: `UNDELIVERED` を選び `CLAIMED(lease=now+T, owner=sidecar credential, epoch=現 mode-epoch)` にして行を返す）→ sidecar が各行を `notifications/claude/channel` で emit → **`mcp.notification` が resolve した行だけ** `/confirm-delivered(id)` で `DELIVERED` へ。
- **lease reaping**: `confirm` されないまま lease 失効した行（= sidecar が配達途中で死亡）は daemon が **`UNDELIVERED` へ戻す**（再 eligible）。これにより「**配達確定（`DELIVERED`）は notification emit の*後***」が成立し、sidecar 死亡時の lost-message window が閉じる。
  - **`DELIVERED` の意味の正確化（過大主張の回避）**: `mcp.notification` の resolve が保証するのは **harness transport が notification を受理した**ことまでで、「モデルのターンへ可視注入され処理された」ことの証明ではない（sidecar→harness 受理後・実表示前に harness 側で落ちる failure model は未定義）。よって `DELIVERED` = 「**harness 受理済**」と定義し、それ以上を主張しない。この残余 window（受理〜可視の間）こそ **at-least-once + 冪等表示**が許容する対象であり、**§9.5 の K1 spike に「`mcp.notification` resolve の可視性 / 障害境界」の実測を含める**（resolve が idle wake と等価かを検証するまで `DELIVERED` の意味は harness 受理に留める）。
- **配達保証の明示選択 — at-least-once + 冪等表示**: idle-wake 用途では **at-most-once + 喪失リスクより、at-least-once + 冪等表示**（同一メッセージの重複表示は良性、喪失は致命）を採る。`DELIVERED`（confirm 済）は**二度と再配達しない**（confirmed 上は at-most-once）。`CLAIMED` のまま reap された行は再 eligible 化される（ライフサイクル全体では at-least-once）。**Contract Surface 2.3 との整合**: 「`CLAIMED`-but-unconfirmed は *successful drain ではない*」ため再配達は契約合法。`DELIVERED` は再配達しない = Surface 2.3 の「drain 後再配達しない」を満たす（§9.9 S3 で Surface 2.3 を「`UNDELIVERED`-and-unclaimed をドレインする」へ加算 amend）。
- **push→pull flip = claim-issuance ゲート（drain-path ゲートではない）**: delivery_mode の PUSH→PULL 反転は「**新規 sidecar claim の発行を daemon が拒否する**」ことを意味する（既に in-flight な claim は **mode-epoch fencing** で扱う: flip 時に daemon が epoch を進め、旧 epoch の stale な sidecar drain/confirm を**拒否**して当該行を `UNDELIVERED` へ戻す）。これで flip は in-flight drain に対し **原子的**になる。
- **check_messages（両 mode）は claim-respecting view をドレインする**: `check_messages` は **`UNDELIVERED`-and-unclaimed + lease 失効で reclaim 済**の行のみを返し、**それ自体が claim を取る（または 1 daemon トランザクション内で `DELIVERED` 化）**。これにより (i) live な sidecar claim とは二重配達しない、(ii) 並行する 2 つの `check_messages` も二重ドレインしない。**single-drainer 性は『per-agent mode boolean』ではなく『daemon の行レベル claim 所有権』が担保する**（boolean は境界をまたぐ in-flight 操作に mutual exclusion を与えられないため）。
- **flapping/starvation 緩和**: lease `T` は **worst-case emit latency より保守的に**設定（prior art は L432 で配達ごとに `/list-peers` enrichment を 1 回行う — その遅延を勘定する）。`/confirm-delivered` は id で冪等。同一行の reclaim を N 回超えたら当該 sidecar を unhealthy 印字し当該行を pull 経路へ回す。

### 9.4 トラスト境界と sidecar credential — delivery-scoped token

- **「per-agent token を持つだけ＝ゼロ権威」は誤り**: spike `broker.py` で role tier は **token 由来 bind の `auth_role`（L482「権限 tier の唯一の根拠」）→ `role_tier` → `tools_for_role`** で決まる。すなわち **token の所持 = tier の所持**。ops 役割（dispatcher/secretary）の agent token をそのまま sidecar に持たせると、`close_pane` / `poll_events` / `spawn_agent` / `send_keys` 等の **pane 操作権威が第二プロセスへ漏出**する。「ツール非公開」は harness に対するツール宣言の話で、**token が daemon に対して何を呼べるか**とは無関係。よって素朴な β は **least-privilege どころか攻撃面を広げる**。
- **設計 — delivery-scoped credential を別発行**: sidecar には agent の full token ではなく、**配送専用 credential**（`tokens.py` の bind に `scope` フィールドを加算: `delivery` | `full`）を渡す。`scope=delivery` は **`/poll-claims` と `/confirm-delivered` のみ**を、かつ **`to_id == owner` の行のみ**に対して許可し、**全ツール/tier 操作を拒否**する。`/confirm` は id で冪等。
- **これが mutual exclusion の実装根拠でもある**: daemon は **token scope で sidecar-drain と agent-drain を識別**できる。PUSH mode では `/poll-claims` を delivery-scoped token にのみ供し、agent の full token による `check_messages` には（§9.3 の claim-respecting view 経由で）live claim 行を返さない。PULL mode ではその逆。**daemon だけがこの排他を強制できる**（両者は今日 `agent_id` が同一で素の drain primitive は区別不能、`broker.py` L458 の横取り警告がまさにこれ）。
- spawn 儀式（§9.5）は delivery-scoped credential を sidecar の env に注入する（agent の full token とは**別物**）。

### 9.5 spawn 儀式・トラスト承認・前提条件・**HARD spike ゲート**

- **注入（spawn 儀式）**: broker 枝の spawn は (a) `--mcp-config <daemon>`（全ツール + agent full token）に加えて、(b) channel sidecar を `--dangerously-load-development-channels server:org-broker-channel` で load し、(c) delivery-scoped credential を sidecar env に注入する。
- **トラスト承認**: dev-channel flag の再導入により「**Load development channel? (Y/n)**」prompt が **再出現**する（`--mcp-config`-only 設計が消した spawn-flow **3-3b 承認の broker 枝での再導入**）。これを `send_keys(enter=true)` で機械承認。folder-trust prompt も同様に機械承認。**これは ratified Contract Surface 1.2 / 5.1（dev-channel injection を MUST とする）への*回帰*であり、§9.9 S3 で「dev-channel 廃止提案の撤回」を明記する**。
- **前提条件（継承であり新規 hard dep ではない、明示）**: push 経路は **Claude Code ≥ v2.1.80 + claude.ai login**（channels の前提、prior art README）を要する。**pull フォールバックは auth 非依存**。org は ratified billing constraint #1（対話 TUI / Max subscription・headless 禁止・API-key 不可、renga-decoupling §1.2 / ja-migration §1）で既に claude.ai 系認証に固定されているため、これは **既存前提の範囲内**。「renga 実装依存を claude.ai-login 依存に置換した」という反論に対しては: pull フォールバックが auth 非依存で correctness を保つため、push は **hard dep を足さず graceful degrade する**。
- **「プロトコルに立つ」の正確化**: experimental `{claude/channel}` に立つことは「依存ゼロ」ではなく、renga 実装依存を **Claude Code バージョン + experimental capability 安定性の依存へ relocate** する（first-party vendor protocol なので *より良い*依存だが、結合の消滅ではない）。experimental は harness ベンダの SemVer 保証外。よって **pull-first cadence（§2/§3）は capability 退行への構造的保険**として明示的に残す。
- **HARD pre-ratification spike ゲート（K1）**: 以下は **未検証の load-bearing 仮定**であり、ratify 前の**必須ゲート**とする — Claude Code harness が (i) **tool-less** な（ツール宣言ゼロで `experimental{claude/channel}` のみ宣言する）stdio サーバーを `--dangerously-load-development-channels` 下で **load するか**、(ii) その `notifications/claude/channel` が **idle セッションを起こすか**、(iii) **renga-peers の channel と衝突せず coexist するか**。**prior art は単一サーバーに tools と channel を同梱**しており（`server.ts` は 4 ツール + channel 宣言 + push ループを 1 サーバーに同居）、**tool-less 単独 channel サーバーの先例が無い**。(i) が不成立なら **claude-peers 形へフォールバック**: sidecar に messaging 4 ツールを同梱する（sidecar が tool-less でなくなる → §9.4 の least-privilege を再評価し、delivery-scoped でなく messaging-tier scoped token に格上げ）。**このゲートは依存順で Issue E（S3 契約批准 / P8・P9 prose land）より*前*の独立ゲート**（§9.9 K1 行）。実走自体は G の dogfood 環境を流用してよいが、**ゲート PASS は E/G 批准の前提条件**であり、判定の所在は E より上流（G の完了基準*ではなく*）。

### 9.6 §2 / §3.1 pull-first cadence の「フォールバック層」への降格（読み替え規定）

§2 の役割別 cadence・§3.1 の poll baseline は、**push mode が効かないとき自動発動するフォールバック層**として読む。**フォールバック発動条件**: sidecar 不在 / unhealthy（heartbeat timeout で delivery_mode=PULL）/ channel 非対応エージェント（codex pull peer）/ claude.ai login 不在環境 / experimental capability 退行。役割別の読み替え:

| 役割 | push mode（一次・§9） | フォールバック（§2/§3.1、push mode 失効時） |
|---|---|---|
| **worker** | 指示・レビュー指摘・SUSPEND・クローズ指示は **channel push が一次**。idle worker も channel が起こす。完了後レビュー待機の bounded `/loop`（§2 worker (a-2)）は **「ペイン保持 + フォールバック poll」役割に縮小**（push が届くので /loop 無しでも受信はするが、ペイン保持と degrade 保険のため /loop は残す） | §2 worker (a-1) 実行中ターン境界 poll / (a-2) 完了後 bounded `/loop Nm` の `check_messages` |
| **dispatcher** | DELEGATE 受信は **channel push が一次**。`/loop 3m` は **pane lifecycle（`poll_events`）のため依然必須**で廃止しない | `/loop 3m` 各サイクルの `check_messages`（§3.3） |
| **secretary** | DELEGATED / 完了報告は **channel push が一次**（idle でも注入）。B2 attention sidecar（§3.2）は **依然 active-signal 層として有効**（push は agent を起こすが、人間不在 gap の*人間*ページングは別軸） | §3.2 B1 ターン冒頭 poll |

- **nudge（§3.1 の打鍵 accelerator N1）の位置づけ更新 — 撤回するのは*配送*ナッジに限る**: push の正準手段が `claude/channel` になったため、**broker の out-of-band *配送*ナッジ（N1 = 「📨 新着あり」をキューに積んで in-pane に出す信号）は撤回**する（PTY 配送ナッジは idle を起こさないことが dogfood で確定済、§3.1。channel sidecar に supersede され、フォールバック層は pull cadence が担うため残す役割が無い）。§5 / §6.2 N1 行・ja-migration §8 Issue H N1 部は §9.9 で「channel sidecar に置換・撤回」と更新する。
  - **§3.5 の*介入層* literal-text redirect は別物で、撤回しない**: §3.5 の暫定 fallback「idle worker へ literal text + Enter で `check_messages` を打鍵起こし」は、**プロンプトへの実 submission（= 介入）**であり、out-of-band *配送*ナッジ（不起床）とは機構が異なる（実打鍵は idle ペインのターンを実際に起こす）。これは **配送路ではなく*介入* accelerator**（割り込み安全な論理ペイン限定・secretary 除外）として、push フォールバック時に idle worker を動かす任意手段として残る。**「配送ナッジ撤回」と「介入打鍵存続」は両立する**（前者=delivery、後者=intervention）。
- **secretary への push は安全**: §3.2 B3 が却下した「secretary 実ペイン化 + 打鍵 nudge」は人間 IME compose 破壊が理由だったが、**`claude/channel` は PTY を経由しない in-band 注入**のため IME を破壊しない（renga の in-band push と同じ層）。よって secretary も push 一次の対象に含められる（B1 はフォールバック）。

### 9.7 既定 renga 経路の不変性（§4 の補正を含む）

- **すべて broker 枝・加算・flag-gated**: §9 の channel sidecar・dev-channel 再導入・daemon delivery lifecycle 改修は **`ORG_TRANSPORT=broker` 枝のみ**で作動する。renga は自前の in-band push（`server:renga-peers`）と自前 dev-channel を**従来どおり保持**し、renga ツールは 1 つも失われない。
- **launcher argv の bit 等価**: dev-channel flag 注入は **descriptor 駆動・broker 枝厳格**とし、**flag=renga 再生成は第二の dev-channel を一切 emit しない**（renga は `server:renga-peers` のみ）。これを **Issue D golden に launcher argv の bit/behavior 等価**として追加する（prose だけでなく起動 argv も等価検証）。
- **§4(4) の補正（再掲）**: §9 適用下では「切戻し 5 条件は増えない」は **§1〜§8 の prose 変更に限った主張**へ縮小する。§9 は §5.5 切戻しドリルに **第 6 サブステップ**（per-pane channel sidecar の SIGTERM/unregister + 当該 agent の delivery_mode reset + delivery-scoped credential の revoke を、条件 (3) active ペイン respawn / (4) daemon 停止順序 / (5) token・queue 破棄の列へ enroll）を要求する。**依然 bounded・flag-gated**（renga 経路不変）。
- **flag 既定は不変**: §9 は挙動層の一次/フォールバックの**向き**を反転するのみで、`ORG_TRANSPORT` の既定値（移行期=renga）は変えない（既定反転は ja-migration §8 Issue G ゲート後の人間判断のまま）。

### 9.8 claude-peers-mcp との差分

| 観点 | claude-peers（prior art） | org-broker（β・本書） |
|---|---|---|
| スコープ | machine-wide（全 Claude セッション） | org-scoped・**flag-gated**（renga と coexist 可、別名 `org-broker`） |
| credential | 単一 shared secret（`~/.claude-peers-token`、全 peer 共有） | **per-agent token（full）** + **sidecar 用 delivery-scoped credential**（§9.4） |
| 役割・権限 | なし（フラット） | **role tier**（worker/curator=messaging 4 / dispatcher/secretary=+pane 操作、構造遮断） |
| 帰属 | `from_id` は peer 自己申告 | **token 由来**（なりすまし不可） |
| sidecar 形 | tools + channel を **1 サーバーに同梱**（`server.ts`） | **tool-less 配送トランスデューサ**（§9.5 HARD spike で要検証。不成立なら同梱形へ fallback） |
| 配送状態 | 単一 `delivered` boolean（**lost-message window あり**） | **三状態 UNDELIVERED→CLAIMED(lease,owner,epoch)→DELIVERED**（claim-then-confirm、§9.3） |
| pane 操作 | なし | **daemon が権威**（spawn/close/inspect/send_keys/poll_events） |
| daemon 起動 | sidecar が auto-launch | **org-start の daemon entry**（runtime 同梱、ja-migration §4.6） |
| 注入 | dev-channel のみ | **`--mcp-config`（daemon）+ dev-channel（sidecar）の併用**（§9.5） |
| フォールバック | `check_messages` 手動 | **pull-first cadence（§2/§3）が構造的フォールバック層**（§9.6） |
| store | SQLite `~/.claude-peers.db` | **`.state/broker/` subtree**（Set C amendment、ja-migration §4.5） |
| auto-summary | gpt-5.4-nano（任意） | スコープ外（既存 set_summary を踏襲） |

### 9.9 ja 反映変更一覧の追補（§6 への加算）

§6 の prose/runtime/descriptor 分解に、push 一次配送分を加算する。**すべて broker 枝・加算で、renga 枝不変**（§9.7）。**N1（nudge accelerator）は撤回**（channel sidecar に supersede、§9.6）。

| ID | 層 | 対象 | 変更内容 | §8 Issue | 由来 |
|---|---|---|---|---|---|
| **P8** | prose（spawn 儀式） | `org-delegate/SKILL.md` Step 3（worker spawn）/ `org-start` Block D / `.dispatcher/references/spawn-flow.md` | broker 枝 spawn に **dev-channel sidecar load（`server:org-broker-channel`）+ 3-3b 機械承認の*再導入*** を記述。`renga-decoupling.md` §4.6（「dev-channel prompt は存在しない」）と contract Surface 5.1 廃止*提案*の **撤回注記**を併記（§9.5） | E（prose）+ G（spawn-flow AC） | push 一次 |
| **P9** | prose（受信モデル） | §6 P1/P2/P5 が触る worker/secretary/dispatcher 受信 prose + 本書 §2/§3.1 | 受信を **「push 一次（channel）/ pull フォールバック」**へ（§9.6 読み替え表）。§2/§3.1 を fallback 層と明記。**P1/P2/P5/P3a/P3b は撤回せず**「フォールバック層の cadence」として読み替え | E | push 一次 |
| **R3** | runtime（配送サイドカー） | `claude_org_runtime/broker/channel_sidecar.py`（新規） | **stdio MCP channel sidecar**: `experimental{claude/channel}` 宣言・delivery-scoped credential 保持・~1s の **claim→push ループ**（`/poll-claims`→`notifications/claude/channel`→`/confirm-delivered`）。heartbeat。org-start/spawn が per-pane で起動 | **A**（terminal/spawn）+ **B**（broker） | push 一次 |
| **R4** | runtime（daemon） | `broker/store.py` + `broker/tokens.py` + `broker/server.py` | **daemon delivery lifecycle 改修**: 三状態 schema（`CLAIMED(lease,owner,epoch)`）・`/poll-claims` + `/confirm-delivered` endpoint・per-agent `delivery_mode`（PUSH/PULL）+ heartbeat health・**delivery-scoped token scope**（`tokens.py` に `scope` 加算、§9.4）・mode-epoch fencing。`check_messages` を claim-respecting view 化 | **B**（broker） | push 一次 |
| **D2** | descriptor | transport surface descriptor（§6.3 D1） | broker の `receive_mode` を **`poll`→`push`**（fallback=`poll`）へ更新。launcher argv（dev-channel injection の有無）を descriptor 駆動化し **Issue D golden に launcher argv の bit 等価**を追加（§9.7）。**D1 の「broker=poll 定数」記述を本 D2 が supersede** | **D** | push 一次 |
| **S3** | 契約改訂提案 | Set D Surface 1.2 / 2.1 / 2.3 / 5.1 / 8 | **dev-channel 廃止提案の撤回**（Surface 1.2/5.1 の ratified dev-channel injection を `org-broker-channel` に対して再確認）。**Surface 2.1 の「push 廃し pull 統一」提案を「push 一次（channel）+ pull フォールバック」へ差し替え**。**Surface 2.3 に三状態（`CLAIMED`/`/confirm`/lease-reap）を SemVer-additive 加算**し drain semantics を「`UNDELIVERED`-and-unclaimed をドレイン」へ。Surface 8 に delivery-scoped token scope を加算。**いずれも改訂*提案***（contract は ratified SoT・批准 PR は人間ゲート） | **E**（契約） | push 一次 |
| **K1** | spike ゲート | Claude Code harness 実測 | §9.5 の **HARD pre-ratification spike**: tool-less channel server の load 可否 / idle wake / renga coexist の 3 点実測。**+ `mcp.notification` resolve の可視性/障害境界**（§9.3 末尾）。**不成立なら sidecar 同梱形へ fallback**。ratify 前の必須ゲート | **依存順で E より前の独立ゲート**（PASS が E=S3 契約批准 / P8・P9 prose land の前提。実走は G dogfood 環境を流用可だが判定は上流） | push 一次 |
| ~~N1~~ | （撤回） | broker nudge accelerator | **撤回**: push の正準手段が `claude/channel` になり nudge は wake 機構として不要（§9.6）。ja-migration §8 Issue H の N1 部は「channel sidecar に置換」と更新 | — | — |

> **ja-migration §8 への反映**: A（+R3 channel sidecar spawn）/ B（+R4 daemon delivery lifecycle + delivery-scoped token）/ D（+D2 receive_mode=push + launcher argv golden）/ E（+P8/P9 prose + S3 契約改訂）/ G（+3-3b 承認再導入 AC）/ H（N1 撤回・S2 attention sidecar は §3.2 のまま有効）。**K1 spike ゲートは依存順で E より前の独立ゲート**（E/G の批准前提・§9.5）。push 一次の中心質量（R3/R4 runtime + P8/P9 prose）は **新規 runtime（R3/R4）が Issue A/B に、prose が Issue E に**集中する。

---

## 改訂履歴

- 2026-06-13: 初版（design only / Refs #16）。transport-lab Issue #16 と ja#515 dogfood コメント（2026-06-13、defect 1〜4）を入力に、push→pull の挙動層を broker-native に再導出。受信モデルを **pull-first cadence**（役割別: worker=turn-boundary / dispatcher=loop-3m / secretary=turn-prologue+sidecar）として一次設計。nudge は **poll 正準 + 打鍵 accelerator defer**（§6.3 reconcile と同型、推奨 C）。defect 1（nudge wakeup）/2（secretary 受信ループ= B1 ターン冒頭 poll + B2 attention sidecar 拡張）/3（dispatcher /loop 実発火）/4（tmux 単一セッション再構成）に各々設計上の対処を明記。既定 renga 経路の不変性を 4 点で構造保証。ja 反映変更一覧（P1 / P2 / P3a / P3b / P4 / P5 / P6 / S2 / R1 / N1 / D1）を層別・§8 Issue 別に分解。**重要な発見**: 第 1 次 prose pass（org-delegate L42 / org-start L56）が「ナッジを見たら」という push 残留仮定を broker 枝に持ち込んでおり、これを pull-first へ修正（P1/P3a）。新規 doc 採用判断（§5 は静的シーム SoT、本書は挙動層 SoT、概ね直交）。
  - **同日 adversarial design review（4 lens × 検証、Blocker 1 / Major 7 を反映）**: (1) **[Blocker]** worker 完了後レビュー待機が defect 3 を worker に再発させる no-re-entry gap だったため、§2 worker を 2 フェーズ化（実行中=ターン境界 poll / 完了後=bounded `/loop Nm` 実発火）し P1/P5/§8 を整合（§3.3 dispatcher と同型）。(2) **[Major]** defect 3 の /loop 修正対象を **org-start Block D → `.dispatcher/CLAUDE.md` L121/L134** に是正（org-start に /loop は無い・secretary は dispatcher session で /loop を invoke 不可。grep 検証付き）。P3 を P3a（org-start L56 / defect 1）と P3b（dispatcher / defect 3）に分割。(3) **[Major]** §1/§2 の「`receive_mode` 定数が既存」表現を「broker は構造的 pull・`receive_mode="poll"` は §8.8 contract amendment / D1 提案で未実装」に是正。(4) **[Major]** D1 の `receive_mode` が既存 contract 出力フィールド（backend-interface-contract §8.8）と同名のため、descriptor を *上流 SoT* とする linkage を明記（rename しない）。(5) **[Major]** S2「新規インフラ不要・既存 sidecar 再利用」は誤り（`attention/readers.py` は queue read 経路を持たない）→「sidecar 骨格は再利用だが reader 入力は net-new」に是正し独立 runtime Issue 化。(6) **[Major→partial]** §7 に反対仮説（§5.2(ii).a 小節へ畳む案）の明示と反証を追加、「直交」を「概ね直交」に緩和。(7) **[Major→partial]** §3.2 に B2 の被覆範囲（通知のみ・agent は起こさない・人間不在 ack 遅延は次の人間ターンに bound・#312 遷移条件は不変で dispatcher 機械観測が backstop）を明示。
- 2026-06-13: **追加 defect（介入層）を反映（窓口追加観測 / Refs #16）**。broker tmux adapter の `send_keys` key 語彙制限（**Escape 不可** = `[key_unsupported]`、Enter/Ctrl+C/literal のみ）で org-delegate の Escape ベース worker 介入手順が実行不能、を §3.5 に追加。**Ctrl+C 安全性評価**: Escape（graceful 中断・冪等再送可）と Ctrl+C（非冪等・2 回で Claude exit = session 喪失・粒度粗）は等価でないと結論。**設計 2 horizon**: 正準 = adapter key 語彙に Escape/Shift+Tab 追加（tmux ネイティブ・drop-in 不変、R2 → Issue A+C）/ 暫定 = gated single Ctrl+C（生成中 pre-check・厳密 1 回再送禁止・session 生存 post-check・死亡時エスカレート）+ idle redirect は pull 受信。変更一覧に R2（adapter 語彙）/ P7（介入 prose broker 枝 + renga-error-codes 注記）を追加。既定 renga の介入手順・send_keys 語彙は不変（broker 枝 adapter 加算）。
  - **Codex セルフレビューゲート（full、Blocker/Major ゼロまで収束）での追加是正**: (i) §8 Issue 表に S2 runtime / N1 の所在が無く R1 が Issue A scope 外だったため、ja-migration-plan §8 に **Issue H 新設**（S2 watcher input 拡張 + N1 nudge accelerator）+ **Issue A に R1**（tmux 単一セッション化）+ **Issue D に D1**（receive_mode descriptor）を反映。(ii) ja-migration-plan §3.2 / renga-decoupling §7.1 受信モデル節の旧「ナッジを見たら」を pull-first 文言へ修正。(iii) renga-decoupling **§4.3 / §7.1 AC-1 / §7.3** がナッジを正準配送路・Phase 3 必須完了条件として扱っていた矛盾を、**ナッジ=任意 accelerator（N1）へ降格・正準=pull-first cadence** の再定義注記で解消（AC-1 は IME 非破壊の実証であり「idle を起こす」ことは実証しない、と射程を明示）。
- 2026-06-13: **§9 push 一次配送への再設計を追補（design only / Refs #18）**。ユーザー判断（ja#515 dogfood レビュー「push にすべき・pull はフォールバック」）+ 決定的 prior art（happy-ryo/claude-peers-mcp の `claude/channel` パターン、`broker.ts`/`server.ts` 実コード照合）を受け、配送モデルを **#16 の「pull-first 正準 + nudge 任意」から「push 一次（claude/channel）+ pull フォールバック」へ反転**。**§1〜§8 は撤回せず、§2/§3.1 を『フォールバック層』へ降格**（読み替え規定 §9.6）。採用 = **β: daemon（権威・全ツール・据え置き）+ per-session channel sidecar（配送トランスデューサ・droppable）**（α=全ツール proxy 案は却下、唯一の実利は delivery-scoped token で回収）。spawn 儀式に **dev-channel 再導入 + 3-3b 機械承認**、claude-peers との差分（§9.8）、ja 反映追補（P8/P9/R3/R4/D2/S3/K1、N1 撤回）。top banner + §2 reframe + §4(4) 補正を併記。**nudge の正準手段が PTY 打鍵 → claude/channel に替わったことで #16 の根因（push 契機なし）が構造的に解消**する点を明示（nudge 観測の否定ではなく push 手段の置換）。
  - **同日 adversarial design panel（3 lens 並列 × 検証、Blocker/Major 収束）を反映**: (1) **[Blocker]** prior art 単一 `delivered` boolean の **lost-message window**（`broker.ts` L266-275 が drain 時マーク・`server.ts` L425→L447 が後 emit、境界またぎで原子化不能 → sidecar 死で沈黙喪失）を、**daemon 所有の三状態 `UNDELIVERED→CLAIMED(lease,owner,epoch)→DELIVERED` + claim-then-confirm + lease-reap**で閉鎖（§9.3）。配達保証を **at-least-once + 冪等表示**に明示選択。(2) **[Blocker]** 「per-agent token を持つ sidecar = ゼロ権威」の誤り（`broker.py` L482 `auth_role` = tier の唯一根拠 → ops token は pane 操作権威を漏出）を、**delivery-scoped credential 別発行**（`scope` 加算・drain-own-inbox/confirm のみ）で是正（§9.4）。これが sidecar-drain/agent-drain 識別 = mutual exclusion の実装根拠も与える。(3) **[Major]** 「daemon UNCHANGED」を撤回し daemon delta（delivery_mode + caller-aware drain + CLAIMED schema、R4）を明記。(4) **[Major]** push→pull flip を **claim-issuance ゲート + mode-epoch fencing** に定式化（drain-path ゲートではない、§9.3）。(5) **[Major]** **tool-less channel server の load 可否は未検証 load-bearing 仮定**（prior art は tools+channel 同梱）→ **HARD pre-ratification spike ゲート K1**（不成立なら同梱形へ fallback）。(6) **[Major]** dev-channel 廃止は **ratified ではなく未批准提案**（Set D は dev-channel injection を依然 MUST）→ β は「提案の撤回」であり「批准済決定の反転」ではない、と framing 是正（§9.5 / S3）。(7) **[Major]** §4(4)「切戻し 5 条件は増えない」は §9 下で偽 → 第 6 サブステップ（sidecar-reap + delivery_mode reset）を §5.5 に畳む補正（§9.7）。(8) claude.ai-login は **既存 billing 制約の範囲内**（新規 hard dep ではない）、experimental capability への依存は結合の relocate（pull-first が構造的保険）と明示。
