# AC-9: WezTerm backend 実機 AC 証跡 (Issue #9)

**ゴール**: WezTerm 実機 (Windows) で **backend のみ (renga 不使用)** の組織運用 1 サイクルを完走する。
Phase 4 (`run_ac4.py`) は「該当 backend 実機で 1 サイクル完走」を Linux/WSL2 では正準 backend の
**tmux** に読み替えて完走済み。本 AC はその **WezTerm (Windows 専用) 側の実機担保** を埋める。

- 実行: `py -3 spike/run_ac9.py`（**GUI ウィンドウは画面に出ない** — §0 参照。headless mux 運用）
- 環境: WezTerm `20240203-110809-5046fc22` / `C:\Program Files\WezTerm\wezterm.exe` / Windows 11
- 判定: **GO (全 5 項目 green)** — `broker-state/ac9/result.json`
- 課金中立: spawn する全プロセスは無課金 probe (`wezterm_probe.py`、実 Claude 不起動)。
  probe-only スコープは窓口/ユーザー判断 (2026-06-13) で承認。実 Claude TUI on WezTerm は
  AC-2 Phase 1 で既証明 + #515 本番サイクルに委譲。実 argv は attestation として記録。

---

## 0. 重要な訂正: 本 AC は headless mux 運用であり、GUI ウィンドウは画面に出ない

**事実訂正 (2026-06-13、ユーザー指摘 → 実機再検証で確定。C 案)**: 当初この文書は「実 WezTerm ウィンドウが
画面に出る」前提で書かれていたが、それは**誤り**だった。本 AC の pane 操作はすべて
`wezterm cli`（`--no-auto-start`）経由で **headless の `wezterm-mux-server.exe`** に対して行われ、
**GUI（`wezterm-gui.exe`）は一切起動せず、画面には何も描画されない**。pane は mux 内の実 PTY なので
`cli list` / `get-text` / `send-text` の機械観測は本物だが（AC-9 GO×5 は有効）、その実体は
**tmux の detached/headless 運用と同格**であり、可視 GUI ではない。

このプロジェクト用語で言えば「`run_ac9` を実行すると WezTerm が見える」という主張は**成立しない**。
機械的 AC（GO）の価値は否定しないが、可視性に関する記述は本節の通り訂正する。

詳細な機序・検証ログ・可視化の成立条件は **§5（headless mux と可視化、#540）** に記す。

---

## 1. 着手前に発見したブロッカー級 defect (geometry 無正規化)

本 AC を組む前の実機確認で、**実 WezTerm の pane-ops 経路がそもそも動かない** defect を発見した。
方針に従い自己修正せず窓口へスコープ確認 → ユーザー判断で「A 案 (本タスク内で正規化修正し full AC を通す)」承認。

### 症状 (修正前、実機再現)

実 WezTerm の `wezterm cli list --format json` は geometry を **ネストした `size:{cols,rows}` +
`left_col` / `top_row` + `is_active`** で返す:

```json
{ "pane_id": 0, "tab_id": 0, "window_id": 0,
  "size": { "rows": 36, "cols": 130, "pixel_width": 1040, "pixel_height": 576, "dpi": 0 },
  "left_col": 0, "top_row": 0, "is_active": true, "cursor_x": 19, "cursor_y": 0, ... }
```

一方 `broker.mcp_list_panes()` は **flat な** `width`/`height`/`x`/`left`/`active` を読む
(`int(rec["width"])` / `rec.get("x", rec.get("left", 0))` / `rec.get("focused", rec.get("active", False))`)。
これは tmux adapter (`TmuxAdapter.list_panes`) が flat に正規化して返すことに合わせた設計。
**WezTerm adapter は生 json を無正規化で素通ししていた** ため、実 WezTerm で:

```
broker.mcp_list_panes()  →  KeyError: 'width'   (broker.py:1014)
```

影響: `mcp_list_panes` に依存する **`spawn_agent` / `resolve_balanced_split` / pane-ops MCP 面が
実 WezTerm で全滅**。この経路は過去 AC で WezTerm 実機未実行だった
(Phase 1/2 AC-2 は adapter 直叩きの spawn/get_text/send_text/kill のみ実証、Phase 4 は FakeAdapter)。
→ Issue #9 goal (3)「tmux 実機との geometry 差分」がそのままブロッカー化したもの。

再現ログ: `broker-state/ac9/defect-geometry.json` (gitignore 配下、`run_ac9` 前段の repro で再生成可)。

### 修正 (A 案、tmux と対称の flat 正規化)

`spike/wezterm_adapter.py` の `WezTermAdapter.list_panes()` で正規化を 1 箇所に閉じた:

| WezTerm 生キー | → 正規化キー (broker 期待) |
|---|---|
| `size.cols` | `width` |
| `size.rows` | `height` |
| `left_col` | `left` |
| `top_row` | `top` |
| `is_active` | `active` (bool) |

`pane_id` / `tab_id` / `window_id` / `cursor_x` / `cursor_y` は spawn/split の追跡・inspect cursor 用に温存。
broker 本体・tmux adapter は無改変 (正規化は adapter 境界に閉じる)。

### 修正後 (通過証跡)

```json
// broker.mcp_list_panes() が実 WezTerm で例外なく geometry を返す
{ "id": 1, "name": "secretary", "role": "secretary", "focused": false,
  "x": 0, "y": 0, "width": 64, "height": 36, "cursor_x": 0, "cursor_y": 34 }
```

`AC-9-geometry: GO` — 修正前 `KeyError('width')` → 修正後 2 pane の geometry を例外なく返す。

---

## 2. AC-9 判定結果 (実 WezTerm、FakeAdapter 不使用)

| 項目 | 判定 | 根拠 (要旨) |
|---|---|---|
| **AC-9-attestation** | **GO** | secretary/dispatcher pane の get-text に probe バナー `[lab9-probe]` を確認 (実 argv=`py -3 wezterm_probe.py` の実プロセス起動を裏取り。課金中立 attestation。WARN ではなく AC 判定に反映) |
| **AC-9-geometry** | **GO** | 正規化後 `mcp_list_panes()` が実 WezTerm `cli list` を flat geometry へ写し例外なく返す (defect 修正の通過証跡) |
| **AC-9-surface** | **GO** | 実 2 pane の geometry で `resolve_balanced_split` が `choose_split` 再利用で split 対象/方向を解決 (`target=2 dir=vertical`)。worker token は pane 操作 6 面を `call_tool` しても `[tool_forbidden]` で構造的に弾かれる |
| **AC-9-cycle** | **GO** | delegate→spawn(実 split)→監視(承認待ち/stall を実 get-text→classify で独立観測)→完了報告(token 由来 from)→CLOSE_PANE(token revoke + pane_exited)→retro の 1 サイクルが renga 不使用で完走 |
| **AC-9-events** | **GO** | broker 非経由の直 kill (クラッシュ) が次 poll の `list_panes` reconcile で `pane_exited` として回復し、reap で token も revoke |

監視の状態観測は **実 WezTerm get-text → `classify_pane_state`** で行っている (Fake の合成画面ではない)。
probe が claude 2.1.168 較正描画を実 PTY に出し、それを scrape して idle / input_pending(承認待ち) / busy(stall) を判定。

### 実 argv attestation (課金中立の裏取り)

spawn した全 pane の実 argv (= 無課金 probe) と、起動済み pane の get-text に probe バナー
(`[lab9-probe]`) が出ることを確認済み:

| role | agent_id | argv | spawn 方法 |
|---|---|---|---|
| secretary | — | `py -3 …\wezterm_probe.py` | `adapter.spawn(new_window=True)` |
| dispatcher | — | `py -3 …\wezterm_probe.py` | `adapter.split(secretary)` |
| worker | worker-ac9 | `py -3 …\wezterm_probe.py` | `broker.spawn_agent(balanced split, inject_mcp_config=False)` |

### スクリーンショット相当 (実 get-text、stall=busy 観測)

`broker-state/ac9/screens/worker-busy.txt` (実 WezTerm pane の grid scrape):

```
──────────────────────────────
[lab9-probe] state=busy
──────────────────────────────
応答を生成中…
  (esc to interrupt)
──────────────────────────────
```

→ `inspect_pane` → `classify_pane_state` が `busy` と判定 (連続 3 回 busy = stall 候補)。
他の証跡: `screens/worker-input_pending.txt` (承認待ち), `worker-roundtrip.txt` (`hello-lab9-smoke` 文字往復),
`worker-spawned.txt` / `{secretary,dispatcher}-banner.txt` (argv 実行確認)。

### ライフサイクル journal (renga 不使用の 1 サイクル)

`broker-state/ac9/state/queue.jsonl` の主イベント列:

```
token_issued secretary / dispatcher / worker-ac9
message_enqueued→queue_drained  (delegate: secretary→dispatcher / 完了報告: worker→secretary / retro: dispatcher→secretary)
nudge_sent secretary
token_revoked worker-ac9   (CLOSE_PANE)
token_revoked dispatcher   (直 kill → reap)
```

---

## 3. tmux 実機との差分 (Issue #9 goal 3、事実のみ)

| 観点 | tmux (POSIX 正準) | WezTerm (Windows) |
|---|---|---|
| **geometry キー** | `list-panes -F` が flat `left/top/width/height/active` を直接出す | `cli list` が **ネスト `size:{cols,rows}` + `left_col/top_row/is_active`**。broker は flat 期待 → adapter で正規化必須 (**本 AC の defect**) |
| **pane_id 型** | 文字列 `%N`。専用 socket `-L claude-org-spike` で既存サーバーと分離 | 整数 (例 6)。native int と broker handle int の取り違え回避のため MCP 面は handle のみ露出 |
| **pane lifecycle** | spawn=`new-session -d` (detached、GUI 不要、CI/WSL2 で無頭運用可) | spawn=`cli spawn --new-window` だが接続先は **headless `wezterm-mux-server`**。**GUI ウィンドウは出ない**（§0/§5）。`--new-window` は mux モデル上の論理ウィンドウを作るだけ。adapter は `--no-auto-start` のため事前に mux が要る（本 spike は手動 bootstrap 依存）。→ **実質 tmux と同じ headless 運用** |
| **events** | `list-panes` 差分から `pane_started/pane_exited` 合成 (`broker._reconcile`、backend 非依存) | 同一経路。`cli list` 差分から同様に合成。直 kill 取りこぼしも list 反映で回復 (**同型**) |
| **focus モデル** | `pane_active` は **session 内で単一** (active pane は 1 つ) | `is_active` は **tab/window ごと**。複数ウィンドウ/タブ構成では **複数 pane が同時に `is_active:true`** になる (本 AC 着手時の初回 `cli list` で window0 pane0 と window1 pane1 が両方 true を実測)。正規化で `is_active→focused` のため、マルチウィンドウ org 配置では `focused` が複数 true になり、tmux 単一 active / renga 単一フォーカス前提と乖離する。本 AC は 1 ウィンドウのため顕在化せず GO だが、**runtime 取り込み時に `choose_split` の focused 依存ロジックが要確認**（記録のみ・本 AC では自己修正せず） |
| **get-text** | `capture-pane -p` は描画済み行中心 | `cli get-text` は **viewport 全高 (空行込み)** を返す。busy ヒントを下部へ寄せないと `classify` の tail-20 走査外に落ちる (probe 較正で確認。実 Claude TUI は元々下部描画) |
| **キー入力** | `send-keys` が一級 (`Enter`/`C-c`/`-l` literal)。素直 | `send-text` の小細工 (`--no-paste`+CR で Enter、paste で未送信置き)。adapter が吸収 |

**結論**: broker のイベント合成・ライフサイクル・権限分離・balanced split は backend 非依存に同型で成立。
唯一の実質差は **geometry 表現** で、これは adapter 境界の正規化 1 箇所で吸収できる (本 AC で実施・実証)。

---

## 4. 人間の目視確認について (訂正: 現状 GUI では目視できない)

**訂正**: 当初ここには「実ウィンドウで分割/状態切替/消滅を目視確認」と書いていたが、§0 の通り
**本 AC は headless mux 運用で GUI ウィンドウが出ない**ため、これらは**現状 GUI で目視できない**。
`run_ac9` が実機で行った以下は **get-text scrape による機械観測のみ**で確認済み（GUI 目視ではない）:

1. secretary / dispatcher / worker の 3 pane への balanced split（`mcp_list_panes` の geometry と
   `pane_started` イベントで確認）。
2. worker pane の `input` / `busy` / `idle` 切替（`inspect_pane`→`classify_pane_state` で確認。
   スクリーンショット相当は §2 の `screens/worker-busy.txt` 等）。
3. CLOSE_PANE / 直 kill 後の pane 消滅（`pane_exited` イベント + `list_panes` 差分で確認）。

可視 GUI で目視するための成立条件は §5 を参照（現行 adapter/config では不可）。

---

## 5. headless mux と可視化の成立条件 (#540 判断材料)

### 機序 (なぜ GUI が出ないか)

`wezterm cli`（adapter が使う）は **headless `wezterm-mux-server` 専用**で、`wezterm-gui` を駆動できない。
本 spike の pane は常にこの headless mux に入り、画面には描画されない（§0）。

### 実機検証ログ (2026-06-13、クリーン状態で再現確認)

| 試した手動操作 | 結果 |
|---|---|
| `wezterm connect <name>` | config の名前付き mux ドメインを要求。未定義名は `desired default domain '…' was not found in mux!?; terminating` で即終了。ユーザー config (`~/.wezterm.lua`) に `unix_domains` 定義は**無い** |
| 素の `wezterm-gui` 起動 | **自分専用の別 mux**（`gui-sock-<pid>`）を作る。headless mux-server の pane（別ソケット）は**映らない** = gui-sock 分離 |
| GUI 起動中に `wezterm cli --no-auto-start list`（adapter と同一モード） | `failed to connect to Socket("gui-sock-<pid>"); terminating` で**一貫失敗**（3 秒後再試行も同一 = race ではない）。→ **Windows では cli が gui に接続不能** |

→ 結論: **「人間が必要時に `wezterm-gui` を attach して中を覗く」第 3 案は、現行（adapter 不変 + config 不変）では成立しない**。
cli が gui を駆動できず、gui は headless mux に attach できず、adapter の spawn は名前付きドメインに入らないため。

### 可視化を成立させる条件（将来課題 #540 / 緊急 attach 手順）

可視化（人間が GUI で中を覗く／緊急 attach）には **以下の両方**が必要:

- **(a) ユーザー config への `unix_domains` 追加**: `~/.wezterm.lua` に名前付き unix domain（例 `name='org-mux'`）を定義し、
  cli/gui が共有できる名前付き mux ソケットを用意する（**個人設定変更 = 人間判断**。本タスクでは提案・適用しない）。
- **(b) adapter の `--domain-name` spawn**: spawner が既定ドメインではなく (a) の名前付きドメインに spawn するよう
  `cli spawn --domain-name org-mux …` に変える（**adapter 変更**。C 案の「adapter 不変」と両立しないため別タスク）。

(a)+(b) が揃えば `wezterm connect org-mux` で人間がいつでも GUI を attach し、組織サイクルを覗ける（= #540 の緊急 attach 手順）。
本 AC（C 案）は (a)(b) いずれも行わず、headless mux + 機械観測を lab9 の担保とする。
