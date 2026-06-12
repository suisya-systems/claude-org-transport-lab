# broker-native な役割要素の設計再導出 — push→pull の挙動層

> **status / 位置付け**: design only。Epic #6（renga 依存解消 / Plan B）の挙動層設計。`docs/design/ja-migration-plan.md` の **§5.2(ii)（静的 prose の両系併記）** と **§8 Issue E（ja prose + 契約改訂）** が「受信モデル（push→pull）の prose を両系併記する」と宣言した、その **prose の中身（受信 cadence と役割セマンティクス）を再導出する** 文書。§5 は ja 改変を「1 flag + 1 生成系シーム」に閉じる *静的シーム* の SoT であり、本書はそのシームを通過する *挙動* の SoT。両者は **概ね直交** する（例外: §6.3 D1 の descriptor フィールド追加だけは §5.2(i) 静的シームに接する。§7 で整合と反対仮説の反証を明記）。
>
> **入力**: (1) transport-lab Issue #16 本文、(2) suisya-systems/claude-org-ja#515 の dogfood 実走観測コメント（2026-06-13、flag=broker で委譲サイクル実走中に観測された defect 1〜4 + transport 非依存の #5→ja#554）。
>
> **不可触制約**: 本タスクは設計のみ。production claude-org-ja / runtime 挙動 / GitHub への書込は行わない。本書は transport-lab worktree 内の設計 doc 追加と、`ja-migration-plan.md` への相互参照ポインタ追記に閉じる。ja への実反映は §6 の変更一覧として分解し、人間ゲート後に窓口/ユーザー判断で行う。

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

---

## 4. 既定 renga 経路の不変性（設計保証）

受け入れ条件「既定 renga 経路の不変性」を以下で構造保証する:

1. **すべて broker 枝に閉じる**: §2 の pull cadence、§3.1 の poll baseline、§3.3 の /loop 実発火 prose（`.dispatcher/CLAUDE.md` 監視エントリの broker 枝）、§2 worker の完了後 review-watch /loop、§3.2 B1 のターン冒頭 poll は、いずれも **`ORG_TRANSPORT=broker` 条件下の broker 枝**（§5.2(ii) 両系併記の broker 側）に書く。renga 枝の「in-band push → 即応答」prose は **一字も変えない**。
2. **加算のみ**: §3.2 B2（attention sidecar のキュー拡張）と §3.4（tmux 単一セッション）は **broker transport 選択時のみ作動する加算**。renga 時は watcher も adapter も現状経路。renga ツール（`mcp__renga-peers__*`）は 1 つも失われない。
3. **flag 既定は不変**: 本書は **挙動層の prose/cadence のみ** を扱い、`ORG_TRANSPORT` の既定値（移行期=renga、§5.1）は **変えない**。既定反転は §8 Issue G ゲート後の人間判断のままで、本書はそれを前倒ししない。
4. **切戻し忠実性**: §5.5 の切戻し 5 条件は本書の変更で増えない。broker 枝 prose は flag=renga 再生成時に renga 枝へ戻るだけ（生成系シーム §5.2(i) が両系を render するため、prose の broker 枝は flag で非選択になる）。

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
| **N1** | runtime（任意・低優先） | broker nudge accelerator | 打鍵ナッジ accelerator の spike（poll 正準のまま同 path に低遅延補助）。**3m gap 不足が実測された時のみ着手**。secretary 除外 | **Issue H（N1 部）**（ja-migration-plan §8、F と同列の独立・低優先） | 1 |

### 6.3 生成系 descriptor 層（→ §8 Issue D: ja 統合シーム）

| ID | 対象 | 変更内容 | 由来 |
|---|---|---|---|
| **D1** | transport surface descriptor（§5.2(i)、runtime） | descriptor に **`receive_mode`（`"push"`/`"poll"`）と役割別 `receive_cadence` ヒント**（worker=turn-boundary+review-watch-loop / dispatcher=loop-3m / secretary=turn-prologue+sidecar）を加算フィールドとして持たせ、両生成器が prose render 時に broker 枝の cadence 文言を descriptor 駆動で出せるようにする（drift 防止）。golden test に追加。**— descriptor の `receive_mode` は新概念ではなく、backend-interface-contract.md §8.8（§1.5/§2.2 amend、broker では定数 `"poll"`、批准待ち）が規定する `list_panes`/`list_peers` 出力フィールド `receive_mode` の *上流 SoT* である**。同一の flag 由来値（renga→`push` / broker→`poll`）が (1) broker 枝 prose の cadence render と (2) 当該出力レコードフィールドの双方へ流れる単一 SoT とし、descriptor を rename しない（批准待ちフィールド名 `receive_mode` を ja 生成器が emit する整合を保つため）。§5.2(i) の「両生成器出力 == descriptor の golden test」に出力フィールド一致も含める | 1,2,3 |

> **粒度の所在**: P1/P2/P3a/P3b/P4/P5/P6 は **prose（Issue E）**、S2 は **prose 起動（E）+ runtime watch 実装（Issue H）**、R1 は **runtime terminal adapter（Issue A）**、N1 は **任意 spike（Issue H の N1 部、F 同列）**、D1 は **descriptor（Issue D）**。挙動層の中心質量（受信 cadence prose）は Issue E に集中し、§5.6 の「worker/curator は messaging 4 ツール + 受信モデル prose のみ」という配線替え集中と整合する。ja-migration-plan §8 の Issue 表に A（+R1）/ D（+D1）/ E（+P1-P6, S2 prose）/ H（新設: S2 runtime + N1）として反映済。

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

## 改訂履歴

- 2026-06-13: 初版（design only / Refs #16）。transport-lab Issue #16 と ja#515 dogfood コメント（2026-06-13、defect 1〜4）を入力に、push→pull の挙動層を broker-native に再導出。受信モデルを **pull-first cadence**（役割別: worker=turn-boundary / dispatcher=loop-3m / secretary=turn-prologue+sidecar）として一次設計。nudge は **poll 正準 + 打鍵 accelerator defer**（§6.3 reconcile と同型、推奨 C）。defect 1（nudge wakeup）/2（secretary 受信ループ= B1 ターン冒頭 poll + B2 attention sidecar 拡張）/3（dispatcher /loop 実発火）/4（tmux 単一セッション再構成）に各々設計上の対処を明記。既定 renga 経路の不変性を 4 点で構造保証。ja 反映変更一覧（P1 / P2 / P3a / P3b / P4 / P5 / P6 / S2 / R1 / N1 / D1）を層別・§8 Issue 別に分解。**重要な発見**: 第 1 次 prose pass（org-delegate L42 / org-start L56）が「ナッジを見たら」という push 残留仮定を broker 枝に持ち込んでおり、これを pull-first へ修正（P1/P3a）。新規 doc 採用判断（§5 は静的シーム SoT、本書は挙動層 SoT、概ね直交）。
  - **同日 adversarial design review（4 lens × 検証、Blocker 1 / Major 7 を反映）**: (1) **[Blocker]** worker 完了後レビュー待機が defect 3 を worker に再発させる no-re-entry gap だったため、§2 worker を 2 フェーズ化（実行中=ターン境界 poll / 完了後=bounded `/loop Nm` 実発火）し P1/P5/§8 を整合（§3.3 dispatcher と同型）。(2) **[Major]** defect 3 の /loop 修正対象を **org-start Block D → `.dispatcher/CLAUDE.md` L121/L134** に是正（org-start に /loop は無い・secretary は dispatcher session で /loop を invoke 不可。grep 検証付き）。P3 を P3a（org-start L56 / defect 1）と P3b（dispatcher / defect 3）に分割。(3) **[Major]** §1/§2 の「`receive_mode` 定数が既存」表現を「broker は構造的 pull・`receive_mode="poll"` は §8.8 contract amendment / D1 提案で未実装」に是正。(4) **[Major]** D1 の `receive_mode` が既存 contract 出力フィールド（backend-interface-contract §8.8）と同名のため、descriptor を *上流 SoT* とする linkage を明記（rename しない）。(5) **[Major]** S2「新規インフラ不要・既存 sidecar 再利用」は誤り（`attention/readers.py` は queue read 経路を持たない）→「sidecar 骨格は再利用だが reader 入力は net-new」に是正し独立 runtime Issue 化。(6) **[Major→partial]** §7 に反対仮説（§5.2(ii).a 小節へ畳む案）の明示と反証を追加、「直交」を「概ね直交」に緩和。(7) **[Major→partial]** §3.2 に B2 の被覆範囲（通知のみ・agent は起こさない・人間不在 ack 遅延は次の人間ターンに bound・#312 遷移条件は不変で dispatcher 機械観測が backstop）を明示。
  - **Codex セルフレビューゲート（full、Blocker/Major ゼロまで収束）での追加是正**: (i) §8 Issue 表に S2 runtime / N1 の所在が無く R1 が Issue A scope 外だったため、ja-migration-plan §8 に **Issue H 新設**（S2 watcher input 拡張 + N1 nudge accelerator）+ **Issue A に R1**（tmux 単一セッション化）+ **Issue D に D1**（receive_mode descriptor）を反映。(ii) ja-migration-plan §3.2 / renga-decoupling §7.1 受信モデル節の旧「ナッジを見たら」を pull-first 文言へ修正。(iii) renga-decoupling **§4.3 / §7.1 AC-1 / §7.3** がナッジを正準配送路・Phase 3 必須完了条件として扱っていた矛盾を、**ナッジ=任意 accelerator（N1）へ降格・正準=pull-first cadence** の再定義注記で解消（AC-1 は IME 非破壊の実証であり「idle を起こす」ことは実証しない、と射程を明示）。
