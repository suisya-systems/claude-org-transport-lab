# 機構解明 — なぜ WezTerm 素は IME 変換窓のアンカーを奪い、tmux は奪わない可能性があるか

> ステータス: **propose-only / 機構の根拠付け**。本書は仮説と一次情報の対応付けであり、
> 最終的な GO/NO-GO は人間の目視実走（[`manual-ac-ime-parity.md`](./manual-ac-ime-parity.md)）で確定する。
> ワーカーは日本語 IME 入力をタイプできない（変換窓は OS 側オーバーレイで grid scrape から観測不能）ため、
> 本書は「**なぜ壊れる/壊れない可能性があるか**」を一次情報で根拠付けるところまでを担い、断定しない。

タスク: ime-backend-parity-spike（Refs #6 #9）。
設計 SoT: [`docs/design/renga-decoupling.md`](../../docs/design/renga-decoupling.md) §1.2（IME 制約）/ §7.1（AC-1 受信側 4 状態）、
[`docs/design/ja-migration-plan.md`](../../docs/design/ja-migration-plan.md)（次段移行方針）。

---

## 0. このタスクが再検証する制約と、旧 AC-1 との違い

renga-decoupling.md §1.2 の**確定制約 #2**は次のとおり:

> **IME 制約 — WezTerm 素のままは不成立**: 単一ペインでも Claude Code のスピナー描画
> （「✻ Cogitating...」等）が IME 変換窓のアンカーを奪う（ユーザー実測）。renga は
> hardware-cursor caret 制御でこの問題を解決している。よって人間が日本語入力する端末
> （窓口ペイン）は renga を継続使用する。

本タスクは、この制約を**新方針の下で再検証**する。新方針＝「ユーザーが IME overlay の快適さを
放棄してでも、tmux なら tmux へ／WezTerm なら WezTerm へ**完全移行して組織起動できること**を
許容する」。その上で問うのは 2 点:

1. IME 問題は **WezTerm 固有**なのか、それとも **tmux-in-ホスト端末でも起きる**のか。
2. tmux を「人間入力端末の唯一の backend」にできるか（renga なしで組織を起動できるか）。

> **旧 AC-1（既存 [`../manual-ime-test.md`](../manual-ime-test.md)）との明確な違い**:
> 旧 AC-1 は「**broker のナッジ打鍵注入**が IME 変換中に混線するか」という**輸送層**の検証だった
> （外部からキーを注入する刺激）。本タスクの刺激は **Claude 自身のスピナー自己再描画**であり、
> 外部注入は無い（backend の**描画層**の検証）。同じ「4 状態」の枠を借りるが、観測対象が異なる:
>
> | | 旧 AC-1（manual-ime-test.md） | 本タスク（ime-backend-parity） |
> |---|---|---|
> | 刺激 | broker ナッジのキー注入 | Claude スピナーの同位置連続再描画（外部注入なし） |
> | 問う対象 | 注入が変換中入力を壊すか | backend が IME 変換窓と同位置再描画を共存させられるか |
> | backend | WezTerm（broker 経路） | **tmux 素 vs WezTerm 素の parity** |
> | 4 状態 | 受信側状態 ×ナッジ | idle / 長文入力中 / streaming / IME 変換中 ×スピナー |

---

## 1. 前提となる層構成（WSL2 / ホスト端末 = Windows Terminal を想定）

検証環境は WSL2（Ubuntu-24.04）。**「ホスト端末」とは、WSL のプロセスが書き出す ANSI/PTY を
実際にピクセルとして描画し、かつ Windows の IME 入力コンテキストを所有する Win32 ウィンドウ**を指す。
本タスクでは原則 **Windows Terminal** を想定する（実測: `WT_SESSION` 環境変数が立っている）。
他に WezTerm（Windows GUI）/ VSCode 統合端末もホスト端末になりうるため、前提を明記して扱う。

2 つのレッグの層構成は次のとおり**根本的に違う**。これが parity の有無を分ける核心である。

```
レッグ A: tmux（ホスト端末 = Windows Terminal の中で動かす）
┌─────────────────────────────────────────────────────────────┐
│ Windows Terminal (Win32, TSF/IMM 所有者・IME ウィンドウ描画者)   │ ← IME アンカーはここが管理
│   └ ConPTY ─ wsl.exe ─ (WSL2) ─ tmux ─ claude (TUI, スピナー)   │ ← tmux/claude は IME 非関与
└─────────────────────────────────────────────────────────────┘
   日本語変換窓 = Windows Terminal の TSF レイヤが、自分が描く grid カーソルに紐付けて描画
   claude のスピナーは「文字 grid を書き換える ANSI」として WT に届くだけ

レッグ B: WezTerm 素（＝ WezTerm 自身がホスト端末）
┌─────────────────────────────────────────────────────────────┐
│ WezTerm (terminal emulator 兼 IME-aware Win32/IMM 所有者)       │ ← IME アンカーも grid 描画も同一プロセス
│   └ (pty) ─ wsl.exe ─ (WSL2) ─ claude (TUI, スピナー)           │
└─────────────────────────────────────────────────────────────┘
   日本語変換窓 = WezTerm 自身が、自分の grid カーソルセルに紐付けて preedit/候補窓を描画
   claude のスピナーが grid カーソルを動かすと、同一プロセス内で IME アンカーも動く
```

> **「WezTerm 素」の同定（重要・前提の明示）**: 本タスクの apples-to-apples 比較は
> 「renga / Windows Terminal を**素の WezTerm に置き換えた**ら IME がどうなるか」である。
> したがって **WezTerm 素 = Windows 側 `wezterm.exe`（GUI 端末）が WSL を起動している構成**を第一に想定する。
> もし「WezTerm 素」が **Linux ビルドの wezterm を X サーバ/Wayland 上で動かす構成**を指す場合、
> IME スタックは Windows の TSF/IMM ではなく **fcitx5 / ibus**（`use_ime` + X11/Wayland IME プロトコル）になり、
> 以降の TSF/IMM 議論はそのまま適用できない（別途 fcitx5 の preedit anchor 挙動の検証が要る）。
> 実走時はどちらの WezTerm かを必ず結果テンプレに記録すること（[`manual-ac-ime-parity.md`](./manual-ac-ime-parity.md) §環境記録）。

---

## 2. IME アンカーの基本モデル（一次情報）

日本語 IME の UI は 3 つの窓からなる: **status / composition（変換窓・未確定下線文字列）/ candidates（候補リスト）**
（Microsoft Learn「Status, Composition, and Candidates Windows」[1]）。問題の核心は composition/candidates 窓の**位置**である。

一次情報が定める位置決めの契約（要点）:

- アプリは IME に対して**テキストカーソル（caret）の位置**を伝え、IME はそこに変換窓・候補窓を出す。
  「変換が無いときは候補窓をテキストカーソル位置に置く。変換中は対象（target）の先頭に置くことが推奨される」[2]。
- **「画面上のカーソル位置が変わる限り、アプリは候補窓の位置を能動的に更新しなければならない」**[2]。
  ＝ IME 窓は「カーソル位置」という移動する錨に縛られており、**カーソルが動けば IME 窓も追従する**のが正しい挙動。
- アプリには 2 立場がある[1]:
  - **IME-unaware**: OS（DefWindowProc）が IME 窓を自動管理。
  - **IME-aware**: アプリが「IME 窓の動作・**位置**・外観」を自前で制御する。端末エミュレータはこちら
    （grid カーソルセルに preedit/候補を合わせる必要があるため）。

帰結: **IME 窓のアンカー＝アプリが IME に報告する「カーソルセル」**である。
ここに「スピナーが同位置で連続再描画してカーソルを揺らす」が衝突する。

---

## 3. なぜ WezTerm 素は壊れうるか（grid カーソルと IME アンカーが同一プロセスで結合）

WezTerm は **IME-aware な端末エミュレータ**であり、自分の grid カーソルセルに IME preedit/候補窓を**結合**している。
情報の対応（[4][5][6] が一次＝公式 doc / 実 issue、[3] は二次＝解説 wiki）:

- WezTerm は Windows で **IMM32**（`ImmSetCompositionWindow` / `ImmSetCandidateWindow`）を使い、
  IME 候補/変換窓を**カーソル相対**に配置する（`set_ime_window_position`。挙動の解説は DeepWiki [3]＝**二次情報**。
  一次の裏付けは下記 `composition_status` [4] と issue #2569/#1922 [5][6]）。
- `composition_status` は「**変換中にカーソル位置に表示されているのと同じ**未確定テキスト」を返す [4]。
  ＝ WezTerm は preedit を**カーソルセルにインライン描画**する設計。
- 既知挙動: IME preedit 文字列が「**全ペインのカーソル位置に**描画される」（issue #2569 [5]）、
  「Windows 中国語 IME 使用時にカーソルが文字で埋まる」（issue #1922 [6]）。
  いずれも **WezTerm の preedit/IME 窓が grid カーソルセルに強く結合している**ことの傍証。

ここに Claude Code のスピナー描画が衝突する機構（仮説、ハーネスで再現）:

1. Claude の TUI は、応答生成中に画面の固定位置（ステータス行）で「✻ Cogitating… (Ns)」を**同位置連続再描画**する。
   実装上、典型的には **カーソル保存（DECSC `ESC 7`）→ スピナー行へ絶対移動（CUP）→ 再描画 → カーソル復元（DECRC `ESC 8`）**
   の往復、もしくは行の消去（`ESC[2K`）＋ CR で同じ行を上書きする。
2. この往復のたびに、ごく短時間でも **grid カーソルが入力欄からスピナー位置へ移動し、戻る**。
   再描画頻度は ~10Hz（後述ハーネスの既定）程度。
3. WezTerm は §2 の契約どおり「カーソルが動いたら IME 窓位置を更新」する立場なので、
   スピナーのカーソル往復に追従して **変換窓・候補窓が毎フレーム揺れる／別位置に飛ぶ／未確定文字列の
   インライン描画が上書きで壊れる**——これが「**スピナーが変換窓のアンカーを奪う**」の正体（仮説）。
   WezTerm は grid 描画も IME アンカーも**同一プロセス・同一カーソル**で持つため、両者が直結している。

> 補足: VSCode 統合端末（xterm.js 系）でも、対話型 CLI ツール（Gemini/Copilot）使用時に
> **中国語 IME の変換テキストがカーソルから切り離されて端末右端に飛ぶ**不具合が報告されている
> （microsoft/vscode #282621 [7]、ラベル `upstream` / `terminal-input`）。
> これは「**自己再描画する CLI TUI が端末エミュレータの IME 位置計算を崩す**」という、
> Claude スピナーと**同種の現象**であり、本問題が WezTerm だけの奇癖ではなく
> 「IME アンカーを grid カーソルに結合する端末エミュレータ一般」で起きうることを示す（後述の tmux 安全視も鵜呑みにしない根拠）。

---

## 4. なぜ tmux は壊れない可能性があるか（IME 所有者が別レイヤの Windows Terminal）

レッグ A では、**IME を所有するのは tmux でも claude でもなく、ホスト端末の Windows Terminal** である。

- **tmux は IME に一切関与しない**。tmux は端末多重化器であり、IME という概念を持たない。
  日本語の**変換（composition）はホスト端末の入力レイヤで起き、確定（確定 = commit）した後の
  バイト列だけが PTY → tmux に届く**。変換中は tmux には何も届かない（未確定文字列は PTY を通らない）。
  ＝ スピナーが grid カーソルを揺らしても、それは tmux の内部スクリーンモデルと、それを写す
  Windows Terminal の grid 再描画の話であって、**IME アンカーの所有者（WT の TSF レイヤ）とは層が分かれている**。
- Windows Terminal は **TSF（Text Services Framework）**で IME を実装している（ソースツリー `src/` 配下の
  TSF 実装 [8]。`TSFInputControl` が `TerminalControl` からカーソル位置とフォント情報を得て候補を描画し、
  確定テキストをバッファに書く）。歴史的に「IME UI がカーソルに追従しない」不具合（#459）も
  **PR #1919 で修正済み [9]（一次）**。「ネイティブ端末では本種の問題が起きにくい」という整理も流布するが、
  これは**二次情報**であり、下記の留保（VSCode #282621）どおり鵜呑みにしない。

**tmux が壊れない可能性の機構（仮説、人間実走で確定）**:

1. 変換窓は Windows Terminal の TSF が、**自分が描く grid カーソルセル**に紐付けて出す。
2. スピナーの再描画は「grid を書き換える ANSI」として tmux 経由で WT に届くが、
   - WT の TSF 実装は Microsoft 製で東アジア IME を一級サポートし、カーソル追従が成熟している（#1919 修正済）。
   - スピナーは**カーソルを保存・復元する（DECSC/DECRC）か、入力欄とは別行を上書きする**ので、
     **静止時（再描画フレーム間）のカーソルは入力欄に戻っている**。WT が候補窓を入力欄カーソルに保ち続ければ、
     変換窓は安定する。
3. ＝ レッグ A は「IME アンカー（WT 所有）」と「スピナーが揺らす grid カーソル（claude→tmux→WT に流れる描画）」の
   結合が、**レッグ B（同一プロセス直結）ほど密ではない**可能性がある。これが parity 差の仮説。

> **重要な留保（断定しない理由）**:
> §2[2] の契約は「**カーソルが動けば IME 窓を更新せよ**」であり、Windows Terminal も例外ではない。
> したがって「WT だから絶対に壊れない」とは言えない。むしろ §3 補足の VSCode #282621 は
> 「自己再描画 CLI が端末側 IME 位置を崩しうる」直接の反例である（あちらは xterm.js だが、
> 同位置再描画が IME 位置計算を崩すという機構は共通）。
> 加えて、スピナーが **DECSC/DECRC を使わず絶対 CUP で入力欄カーソル位置そのものを動かす**実装だった場合、
> WT であってもカーソルが揺れて変換窓が追従する余地が残る。
> ゆえに「tmux なら安全」は**仮説に留め、4 状態の人間実走で確定**する。ハーネスは
> **DECSC/DECRC 往復モードと絶対 CUP モードの両方**を再現して、どちらの再描画様式が IME を崩すかを切り分けられるようにする。

---

## 5. renga の解（§1.2「hardware-cursor caret 制御」）が示す統一機構

renga が IME 問題を解いている方法は、本問題の機構を逆から照らす。
renga は **hardware-cursor caret（システムキャレット）制御**で IME アンカーを安定させている（§1.2）。
統一的に言うと:

- **問題の本質**: IME 窓のアンカー＝「アプリが IME に報告するカーソル」。これが**アプリ grid カーソル
  （スピナーが揺らす）**に結合していると、スピナーの往復で IME 窓が奪われる。
- **renga の解**: IME アンカーを、スピナーが触る grid カーソルとは別に管理される
  **ハードウェアキャレットを入力欄セルに固定**して与える。これで IME 窓は grid カーソルの揺れから
  **デカップル**され、スピナーが何回再描画しても変換窓は入力欄に座り続ける。
- **3 つの backend の位置付け（統一視点）**:

  | backend | IME アンカーの源 | スピナーの grid カーソル揺れとの結合 | 予想（人間実走で確定） |
  |---|---|---|---|
  | renga | 固定したハードウェアキャレット（入力欄に pin） | **デカップル**（§1.2 で解決済） | 安定（既知） |
  | WezTerm 素 | 自プロセスの grid カーソルセル（IMM/`set_ime_window_position`） | **直結** | 崩れる懸念（ユーザー実測の再現） |
  | tmux + Windows Terminal | WT の TSF が描く grid カーソルセル（別レイヤ） | 間接（成熟 TSF + DECSC/DECRC で静止時は入力欄） | **要実走**（崩れない可能性／#282621 型の反例リスク） |

> 結論の方向性（断定せず）: 「IME 問題が WezTerm 固有か tmux でも起きるか」は、
> **IME アンカーの所有者がスピナーの揺らす grid カーソルと同一プロセスで直結しているか否か**で説明できる、
> というのが本書の仮説である。WezTerm 素は直結（崩れやすい）、tmux は別レイヤの WT が所有（崩れにくい可能性）。
> ただし IME 位置契約 [2] と VSCode #282621 [7] が示すとおり「別レイヤ＝絶対安全」ではないため、
> 最終判定は 4 状態の目視実走に委ねる。

---

## 6. 実走で確定すべき問い（人間が [`manual-ac-ime-parity.md`](./manual-ac-ime-parity.md) で埋める）

本書の仮説に対し、人間実走が答えるべき具体的問い:

1. **WezTerm 素**で、スピナー再描画中に日本語変換窓は壊れるか（ユーザー実測の再現確認）。
   DECSC/DECRC 往復モードと絶対 CUP モードで差は出るか。
2. **tmux + Windows Terminal**で、同じスピナー再描画中に変換窓は壊れるか（§4 仮説の検証）。
   壊れないなら「IME 問題は WezTerm 固有」、壊れるなら「tmux-in-ホスト端末でも起きる」と確定する。
3. 4 状態（idle / 長文入力中 / streaming / IME 変換中）それぞれで GO/NO-GO。
   特に **streaming（スピナー稼働）× IME 変換中**が本問題の中心セル。
4. 「IME overlay を放棄してでも完全移行できるか」の運用判断材料: 変換窓が多少揺れても
   **確定文字列が正しく入る／未送信テキストが壊れない**なら、overlay の見た目を諦めれば移行可、という線引きを記録する。

---

## 参考情報（一次／二次を明記）

- [1]（一次）Microsoft Learn — *Status, Composition, and Candidates Windows*: <https://learn.microsoft.com/en-us/windows/win32/intl/status--composition--and-candidates-windows>
- [2]（一次）Microsoft Learn — *Installing and Using Input Method Editors*（候補窓はテキストカーソル位置に置き、カーソルが動けば能動更新）: <https://learn.microsoft.com/en-us/windows/win32/dxtecharts/installing-and-using-input-method-editors>
- [3]（**二次・解説 wiki**。一次の裏付けは [4][5][6]）WezTerm — Windows Integration 解説（`set_ime_window_position` / `ImmSetCandidateWindow` / `ImmSetCompositionWindow` への言及）: <https://deepwiki.com/wezterm/wezterm/4.3-windows-integration>
- [4]（一次・公式 doc）WezTerm — `window:composition_status()`（変換中にカーソル位置に出ている未確定テキストを返す）: <https://wezterm.org/config/lua/window/composition_status.html>
- [5]（一次・実 issue）WezTerm issue #2569 — IME preedit string が全ペインのカーソル位置に描画される: <https://github.com/wezterm/wezterm/issues/2569>
- [6]（一次・実 issue）WezTerm issue #1922 — Windows 中国語 IME 使用時にカーソルが文字で埋まる: <https://github.com/wezterm/wezterm/issues/1922>
- [7]（一次・実 issue）microsoft/vscode issue #282621 — 対話型 CLI ツール使用時に中国語 IME 変換テキストが端末右端に誤配置（`upstream`/`terminal-input`）: <https://github.com/microsoft/vscode/issues/282621>
- [8]（一次・ソースツリー）microsoft/terminal — TSF 実装（`TSFInputControl` 等を含む `src/` 配下）: <https://github.com/microsoft/terminal/tree/main/src>
- [9]（一次・実 PR）microsoft/terminal PR #1919 — #459「IME UI does not follow the cursor in Windows Terminal」の修正: <https://github.com/microsoft/terminal/pull/1919>
- （一次・公式 doc）WezTerm `use_ime`（**現行は全プラットフォーム既定 `true`（20220319 以降）。Windows では IME 常時有効で `use_ime` は効果なし／無効化不可**。X11/Wayland で本設定が効く）: <https://wezterm.org/config/lua/config/use_ime.html>

> 情報の読み方の注意: 「ネイティブ端末では起きにくい」という整理は**二次的な一般論**であり、[7] のように
> 自己再描画する CLI と組み合わせると native（端末エミュレータ）でも崩れうる。本書はこの緊張をそのまま残し、
> 実走で WezTerm 素 vs tmux の parity を確定する立場を取る。
