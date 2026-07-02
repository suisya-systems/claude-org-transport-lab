# Herdr Socket API 実測レポート (herdr-socket-spike)

- タスク: `herdr-socket-spike` / Refs #27
- 目的: [Herdr](https://herdr.dev) を実機インストールし、Socket API の挙動を実測して claude-org terminal adapter が要求する操作の実現可否を裏取りする。
- 本レポートは**観測事実に徹する**。設計判断は並走する `herdr-adapter-design` のスコープであり、ここでは扱わない。

## 0. 環境と検証手順

| 項目 | 値 |
| --- | --- |
| OS | WSL2 (Linux 6.18) |
| Herdr version | `0.7.1` (stable channel, protocol `14`) |
| インストール | user-local (`~/.local/bin/herdr`)。sudo 不要。本タスク開始時点で導入済み |
| 検証セッション | `herdr-spike`（専用・隔離。稼働中の claude-org broker/tmux には非接触） |
| API socket | `~/.config/herdr/sessions/herdr-spike/herdr.sock` |
| server 起動 | `herdr --session herdr-spike server`（headless。TUI クライアント非接続でも PTY を pump し画面バッファを保持することを実測） |

検証は headless server に対し、**newline-delimited JSON を Unix domain socket へ直接投入**して行った（CLI `herdr <subcommand>` は同 socket の薄いラッパ）。以下の JSON は実測の request(`>>>`)/response(`<<<`) 抜粋であり、長い行は本文都合で改行・省略している箇所がある。

## 1. プロトコル基礎

### 1.1 フレーミングと接続寿命

- **1 行 = 1 JSON リクエスト**（newline-delimited JSON）。レスポンスはリクエストと同じ `id` を反響する。
- **通常リクエストは one-shot**: サーバは 1 リクエストへ 1 レスポンスを返した**直後に接続をクローズ**する。1 本の接続でリクエストをパイプラインすると 2 発目で `BrokenPipe` になる。→ リクエストごとに接続を張り直す実装が必要。
- **例外は購読系** (`events.subscribe`): レスポンス後も接続を open のまま保持しイベントを push し続ける。

```jsonc
>>> {"id":"ping1","method":"ping","params":{}}
<<< {"id":"ping1","result":{"type":"pong","version":"0.7.1","protocol":14,"capabilities":{"live_handoff":true}}}
```

### 1.2 ソケットパス解決順序 (検証項目 8)

`herdr status server` の解決先を env / flag を変えて実測。優先順位は **`--session` flag > `HERDR_SOCKET_PATH` > `HERDR_SESSION` > default socket** で、公式ドキュメント記載順と一致した。

| 条件 | 解決された socket | server |
| --- | --- | --- |
| (a) env なし・flag なし | `~/.config/herdr/herdr.sock`（default） | not running |
| (b) `HERDR_SESSION=herdr-spike` | `.../sessions/herdr-spike/herdr.sock` | running |
| (c) `HERDR_SOCKET_PATH=<spike sock>` | `.../sessions/herdr-spike/herdr.sock` | running |
| (d) `HERDR_SOCKET_PATH=<spike>` + `HERDR_SESSION=bogus` | spike（= `HERDR_SOCKET_PATH` 勝ち） | running |
| (e) `--session bogus2` + `HERDR_SOCKET_PATH=<spike>` | `.../sessions/bogus2/herdr.sock`（= flag 勝ち） | not running |

## 2. 検証項目別 実測結果

### 項目1: workspace / tab / pane の create・list・close + split direction/ratio

`workspace.create` はワークスペースと同時に **tab と root pane を自動生成**し、生成物一式を返す（headless でも実 PTY で shell が起動する）。

```jsonc
>>> {"id":"wc1","method":"workspace.create","params":{"cwd":"<worker_dir>","label":"spike-ws"}}
<<< {"id":"wc1","result":{"type":"workspace_created",
      "workspace":{"workspace_id":"w1","pane_count":1,"tab_count":1,"active_tab_id":"w1:t1",...},
      "tab":{"tab_id":"w1:t1","workspace_id":"w1",...},
      "root_pane":{"pane_id":"w1:p1","terminal_id":"term_655a292ef22ed1","workspace_id":"w1","tab_id":"w1:t1",
                   "cwd":"<worker_dir>","foreground_cwd":"<worker_dir>","agent_status":"unknown","revision":0}}}
```

- **識別子体系**: `workspace_id="w1"`, `tab_id="w1:t1"`, `pane_id="w1:p1"`（階層をコロンで表現）。pane には安定した `pane_id` に加え内部 `terminal_id="term_..."` が付く。
- **`pane.split`**（direction + ratio 指定）:

```jsonc
>>> {"id":"sp1","method":"pane.split","params":{"pane_id":"w1:p1","direction":"right","ratio":0.333,"env":{"HERDR_ROLE":"tests"}}}
<<< {"id":"sp1","result":{"type":"pane_info","pane":{"pane_id":"w1:p2","terminal_id":"term_655a29547d4412",...}}}
```

`direction` は `right|down` のみ（`left/up` は不可 → §4 error 参照）。`ratio=0.333` は**元 pane 側の取り分**で、幅 54 cells が p1=18 / p2=36 に分割された。split でも `env` を注入できる。

- **list / close**: `workspace.list` / `tab.list` / `pane.list`、`pane.close` / `tab.close` / `workspace.close` はいずれも動作。close 系の成功レスポンスは `{"result":{"type":"ok"}}`。close 後の `pane.list` は空を返す。`tab.list` は tab 単位に集約された `agent_status` を含む（下記）。

```jsonc
>>> {"id":"tl2","method":"tab.list","params":{"workspace_id":"w1"}}
<<< {"id":"tl2","result":{"type":"tab_list","tabs":[
      {"tab_id":"w1:t1","pane_count":3,"agent_status":"working",...},
      {"tab_id":"w1:t2","pane_count":1,"agent_status":"unknown",...}]}}
```

### 項目2: pane.send_text / special keys 送信

- `pane.send_text` はリテラル文字列を投入（Enter は付かない）。`pane.send_keys` は key-combo 文字列の配列（`keys`）を取る。両者とも成功時 `{"result":{"type":"ok"}}`。

```jsonc
>>> {"id":"st1","method":"pane.send_text","params":{"pane_id":"w1:p1","text":"echo HELLO_HERDR_$((2+3))"}}
<<< {"id":"st1","result":{"type":"ok"}}
>>> {"id":"sk1","method":"pane.send_keys","params":{"pane_id":"w1:p1","keys":["enter"]}}
<<< {"id":"sk1","result":{"type":"ok"}}
```

- **special keys / ctrl chord の実効性を検証**: `sleep 30` 実行中に `ctrl+c` を送出 → `^C` が画面に出て中断、`SLEPT_DONE` は出ずプロンプト復帰。**ctrl chord が PTY に届いている**ことを確認した。

```jsonc
>>> {"id":"sk4","method":"pane.send_keys","params":{"pane_id":"w1:p1","keys":["ctrl+c"]}}
<<< {"id":"sk4","result":{"type":"ok"}}
// pane.read 後: "...sleep 30 && echo SLEPT_DONE\n^C\n...❯\n"
```

- キー語彙は `enter` / `esc` / `ctrl+<x>` / `alt+<x>` 等。未知キーは拒否される（§4）。

### 項目3: pane.read による画面スクレイプ

`pane.read` は `source` と `lines`、`format`(`text|ansi`) を取る。`source` は **`visible`（現在表示バッファ）/ `recent`（スクロールバック）/ `recent-unwrapped`（ソフトラップ無視）**。

```jsonc
>>> {"id":"rd2","method":"pane.read","params":{"pane_id":"w1:p1","source":"visible","lines":25}}
<<< {"id":"rd2","result":{"type":"pane_read","read":{"pane_id":"w1:p1","source":"visible","format":"text",
      "text":"...\n❯ echo HELLO_HERDR_$((2+3))\nHELLO_HERDR_5\n...❯\n","revision":0,"truncated":false}}}
```

- **`source":"recent"` は、まだスクロールアウトが発生していない時点では空文字を返す**（`text":""`）。ライブ画面を取るには `visible` を使う。この差は adapter が「画面が空」と誤判定しやすいので注意点。
- `format":"ansi"` は raw エスケープ込みで返す（色・カーソル制御の解析が要る用途向け）:

```jsonc
<<< {"...","format":"ansi","text":"...[0m[38;5;76m❯[0m [38;5;2mecho[0m HELLO_HERDR_..."}
```

### 項目5: agent.start で claude を起動できるか + argv/env 注入 (窓口補足3)

`agent.start` は `name`（ラベル）, `cwd`, `argv`（配列）, `env`（オブジェクト）, `split`, `workspace`, `tab` を取る。**任意 argv・任意 env を注入可能**なことを、注入値を実行時に echo する probe で確定した。

```jsonc
>>> {"id":"as1","method":"agent.start","params":{"name":"probe","cwd":"/tmp","split":"down",
      "env":{"HERDR_ROLE":"probe-role","MY_FLAG":"xyz"},
      "argv":["bash","-lc","echo ARGV_OK $1 $2; echo ROLE=$HERDR_ROLE MYFLAG=$MY_FLAG; echo PANE=$HERDR_PANE_ID SOCK=$HERDR_SOCKET_PATH; sleep 300","--","argA","argB"]}}
<<< {"id":"as1","result":{"type":"agent_started","agent":{"pane_id":"w1:p3","name":"probe",...},"argv":[...]}}
// pane 実出力:
//   ARGV_OK argA argB
//   ROLE=probe-role MYFLAG=xyz
//   PANE=w1:p3 WS=w1 SOCK=/home/.../sessions/herdr-spike/herdr.sock
```

→ 任意フラグ付き起動（例: `claude --mcp-config ...`）は argv 配列でそのまま渡せる。Herdr 側は起動プロセスへ **`HERDR_SOCKET_PATH` / `HERDR_PANE_ID` / `HERDR_WORKSPACE_ID` / `HERDR_TAB_ID` / `HERDR_ENV=1` を自動注入**する。

**実 claude の起動と自動検出**: `argv:["claude"]` で本物の claude を起動。Herdr は同梱の agent-detection manifest（`claude` v`2026.06.10.3`, remote 取得）で**画面を passive scrape し、2 秒以内に `agent="claude"`, `agent_status="idle"` を自動付与**した。

```jsonc
>>> {"id":"cs1","method":"agent.start","params":{"name":"claude","cwd":"<worker_dir>","argv":["claude"]}}
<<< {"id":"cs1","result":{"type":"agent_started","agent":{"pane_id":"w1:p4","name":"claude","agent_status":"unknown",...}}}
// 2秒後 pane.get:
<<< {"...","pane":{"pane_id":"w1:p4","label":"claude","agent":"claude","agent_status":"idle","revision":0}}
```

`agent.explain` で検出ロジックが観測できる。**優先度つきルール**を画面領域に当て、マッチしたルールの `state` を採用する（今回は `live_prompt_box` prio950 → `idle` がマッチ）。ルール例:

| rule id | priority | region | 判定 state |
| --- | --- | --- | --- |
| `osc_title_working` | 1100 | osc_title | working |
| `live_blocked_form` (`enter to select` / `esc to cancel`) | 980 | after_last_horizontal_rule | blocked |
| `bash_permission_prompt` / `generic_permission_prompt` (`do you want to proceed?`) | 850/840 | ... | blocked |
| `live_prompt_box` (`^\s*❯`) | 950 | prompt_box_body | idle |
| `osc_title_idle` / `osc_progress_idle` | 250 | osc_title / osc_progress | idle |

（＝ claude の「承認待ち(blocked)」「稼働中(working)」「入力待ち(idle)」を screen-scrape で機械判定している。ANSI/OSC タイトルも判定材料。）

### 項目6: events.subscribe / events.wait の long-lived stream 挙動

- **`events.subscribe`**: `subscriptions` 配列を取り、各エントリは `type` と **`pane_id`（必須）** を持つ。`pane_id` を省くと `invalid_request: missing field pane_id` になる。**ワイルドカード（全 pane 購読）は不可** — 監視対象の pane_id を事前に知っている必要がある。購読成立で `subscription_started` を返し、以後イベントを push:

```jsonc
>>> {"id":"sub_p2","method":"events.subscribe","params":{"subscriptions":[
      {"type":"pane.agent_status_changed","pane_id":"w1:p2"},
      {"type":"pane.exited","pane_id":"w1:p3"},{"type":"pane.closed","pane_id":"w1:p3"}]}}
<<< {"id":"sub_p2","result":{"type":"subscription_started"}}
<<< {"data":{"agent":"spike-bot","agent_status":"working","pane_id":"w1:p2","workspace_id":"w1"},"event":"pane.agent_status_changed"}
<<< {"data":{"agent":"spike-bot","agent_status":"blocked","pane_id":"w1:p2","workspace_id":"w1"},"event":"pane.agent_status_changed"}
<<< {"data":{"agent":"spike-bot","agent_status":"idle","pane_id":"w1:p2","workspace_id":"w1"},"event":"pane.agent_status_changed"}
<<< {"data":{"pane_id":"w1:p3","type":"pane_closed","workspace_id":"w1"},"event":"pane_closed"}
```

- **購読可能 event type**（`events.subscribe` の topic 文字列, dotted 表記）: `pane.created` `pane.closed` `pane.exited` `pane.focused` `pane.moved` `pane.agent_detected` `pane.agent_status_changed` `pane.output_matched`。
- **`events.wait`（JSON メソッド）は v0.7.1 で未実装**: 呼ぶと `{"error":{"code":"not_implemented","message":"method not implemented yet"}}`。CLI の `herdr wait agent-status` / `herdr wait output` は別経路（`events.subscribe` / `pane.wait_for_output`）で実現されている。
- **`pane.wait_for_output`（実装済み）**: `match` は internally-tagged enum `{"type":"substring"|"regex","value":...}`。マッチで `output_matched`（`matched_line` + read スナップショット）を返す。

```jsonc
>>> {"id":"wo2","method":"pane.wait_for_output","params":{"pane_id":"w1:p1","match":{"type":"substring","value":"HELLO_HERDR_5"},"source":"visible","timeout_ms":2000}}
<<< {"id":"wo2","result":{"type":"output_matched","pane_id":"w1:p1","matched_line":"HELLO_HERDR_5","read":{...}}}
```

- **CLI の agent-status wait は edge-triggered**: p2 が既に `idle` の状態で `herdr wait agent-status w1:p2 --status idle --timeout 3000` を実行すると、**現在値が idle でも「idle への遷移」が起きないため 3s でタイムアウト**（`timed out waiting for agent status change`）。level ではなく transition を待つ点に注意。

### 項目7: pane.report_agent の状態値

`pane.report_agent` は外部ソースが状態を push する API。`pane.get` の `agent` / `agent_status` に反映され、購読者へ `pane.agent_status_changed` が飛ぶ。

```jsonc
>>> {"id":"ra1","method":"pane.report_agent","params":{"pane_id":"w1:p2","source":"custom:spike","agent":"spike-bot","state":"working","message":"doing work"}}
<<< {"id":"ra1","result":{"type":"ok"}}
>>> {"id":"pg_p2a","method":"pane.get","params":{"pane_id":"w1:p2"}}
<<< {"...","pane":{"pane_id":"w1:p2","agent":"spike-bot","agent_status":"working",...}}
```

- **受理される state は `idle` / `working` / `blocked` / `unknown` の 4 値のみ**。ブリーフィングにあった `done` は**拒否**される:

```jsonc
>>> {"...","state":"done"}
<<< {"id":"","error":{"code":"invalid_request","message":"invalid request: unknown variant `done`, expected one of `idle`, `working`, `blocked`, `unknown` ..."}}
```

`done` は `herdr wait agent-status --status <...|done>` の**待受側の擬似ステータス**としては現れるが、**report（push）できる状態値ではない**。report_agent の関連メソッド: `pane.report_agent_session` / `pane.report_metadata` / `pane.release_agent` / `pane.clear_agent_authority`。

### 窓口補足1: pane の cwd を Socket API から取得できるか → **可**

`pane.get` / `pane.list` / `agent.list` の各 pane オブジェクトが `cwd` と `foreground_cwd` を持つ。**ライブ追従**することも確認: pane に `cd /tmp` を送ると両フィールドが `/tmp` に更新された。

```jsonc
// cd /tmp 送出後
<<< {"...","pane":{"pane_id":"w1:p2","cwd":"/tmp","foreground_cwd":"/tmp",...}}
```

加えて `pane.process_info` で foreground プロセスの `pid` / `name` / `argv` / `cmdline` / `cwd` まで取得できる:

```jsonc
>>> {"id":"pi1","method":"pane.process_info","params":{"pane_id":"w1:p1"}}
<<< {"...","process_info":{"pane_id":"w1:p1","shell_pid":49409,"foreground_process_group_id":49409,
      "foreground_processes":[{"pid":49409,"name":"zsh","argv":["/usr/bin/zsh"],"cmdline":"/usr/bin/zsh","cwd":"<worker_dir>"}]}}
```

### 窓口補足2: pane geometry (位置・サイズ) の取得精度 → **可（端末セル単位）**

`pane.layout` が `area` と pane ごとの `rect{x,y,width,height}`（**端末セル単位**）、および split ツリー（`direction` / `ratio`）を返す。`pane.edges` は各辺が境界か（隣接 pane の有無）を bool で返す。

```jsonc
>>> {"id":"play2","method":"pane.layout","params":{"pane_id":"w1:p1"}}
<<< {"...","layout":{"area":{"x":26,"y":1,"width":54,"height":23},"focused_pane_id":"w1:p1",
      "panes":[{"pane_id":"w1:p1","rect":{"x":26,"y":1,"width":18,"height":23}},
               {"pane_id":"w1:p2","rect":{"x":44,"y":1,"width":36,"height":23}}],
      "splits":[{"id":"split_0_root","direction":"right","ratio":0.333,"rect":{"x":26,"y":1,"width":54,"height":23}}]}}}
```

精度はピクセルではなく端末セル（列/行）。座標は端末全体を原点とする（`area.x=26` はサイドバー等のオフセット）。

### 窓口補足4: events の overflow 挙動 → **サイレントドロップ・通知なし**

購読接続を張ったまま**読み取りを止め**、別接続から `report_agent` で status 変化 **1500 件を ~2159/s で flood** → その後まとめて drain。

```text
FLOODED 1500 status changes in 0.7s (2159/s)
RECEIVED 518 total lines; 518 agent_status_changed; 0 other
DELIVERED 518 / 1500 -> DROPPED 982
```

- **982 件が欠落**。欠落分に対する **overflow イベント・エラー・"dropped" マーカーは一切来ない（`other`=0）**。購読者が追いつけない場合、イベントは**サイレントに失われる**。
- ＝ 監視側は「全遷移を漏れなく受信できる」前提を置けない。取りこぼし検知や現状復元は `pane.get` / `agent.list` の polling で補う必要がある（本欠落が server 側 bounded queue のドロップか coalescing かまでは特定していない。観測される事実は「無通知の欠落」）。

## 3. terminal adapter 要求操作との対応

| adapter 要求操作 | Herdr Socket API | 可否 | 備考 |
| --- | --- | --- | --- |
| spawn（pane 生成） | `workspace.create` / `tab.create` / `pane.split` / `agent.start` | ✅ | headless でも実 PTY 起動。任意 argv/env 可 |
| close | `pane.close` / `tab.close` / `workspace.close` | ✅ | 成功 `{"type":"ok"}` |
| list_panes | `pane.list` / `tab.list` / `workspace.list` / `agent.list` | ✅ | 階層 id・cwd・agent_status 込み |
| inspect_pane | `pane.get` / `pane.process_info` / `pane.read` / `pane.layout` / `pane.edges` | ✅ | cwd/geometry/プロセス/画面すべて取得可 |
| send_keys | `pane.send_keys`（enter/esc/ctrl chord） / `pane.send_text` | ✅ | ctrl+c で実中断を確認 |
| pane identity | `pane_id`（`w1:p1` 安定） + `terminal_id` | ✅ | id は close まで安定 |
| agent 状態取得 | `agent_status`（`pane.get` push 反映 or 自動検出） | ✅ | 値は idle/working/blocked/unknown |
| agent 状態 push | `pane.report_agent` | ✅ | ただし `done` は push 不可 |
| 状態変化の待受 | `events.subscribe`（pane_id 必須・ロスあり） / `pane.wait_for_output` | ⚠️ | `events.wait` は未実装。購読は overflow でサイレントロス。edge-triggered |

## 4. エラーレスポンス語彙 (検証項目9)

実測で観測した `error.code`（ドキュメントの `not_found` より**細分化**されている点に注意）:

| code | 発生条件（実測） |
| --- | --- |
| `pane_not_found` | 存在しない pane_id への操作（`message":"pane w9:p9 not found"`） |
| `workspace_not_found` | 存在しない workspace_id への操作 |
| `invalid_request` | スキーマ違反全般: 未知メソッド / 必須フィールド欠落（`missing field ...`）/ 未知 enum variant（`unknown variant ...`）/ 型不一致 |
| `invalid_key` | `send_keys` の未知キー（`message":"unsupported key ctrl+shift+notakey"`） |
| `not_implemented` | 未実装メソッド（`events.wait`） |

- **id の反響**: 意味エラー（`pane_not_found` 等）は request の `id` を反響するが、**パース失敗（`invalid_request`）では `id` が空文字 `""`** になる（id 抽出前に失敗するため）。
- 未知メソッドの `invalid_request` は**受理メソッド一覧を message に列挙**する（下記 §5 はこれを転記）。

## 5. protocol 14 の全メソッド語彙

未知メソッドエラーが列挙した、v0.7.1 / protocol 14 の受理メソッド一覧（そのまま転記）:

```text
ping, server.stop, server.live_handoff, server.reload_config, server.agent_manifests,
server.reload_agent_manifests, notification.show, client.window_title.set, client.window_title.clear,
workspace.create, workspace.list, workspace.get, workspace.focus, workspace.rename, workspace.close,
worktree.list, worktree.create, worktree.open, worktree.remove,
tab.create, tab.list, tab.get, tab.focus, tab.rename, tab.close,
agent.list, agent.get, agent.read, agent.explain, agent.send, agent.rename, agent.focus, agent.start,
pane.split, pane.swap, pane.move, pane.zoom, pane.layout, pane.process_info, layout.export, layout.apply,
pane.neighbor, pane.edges, pane.focus_direction, pane.resize, pane.list, pane.current, pane.get, pane.rename,
pane.send_text, pane.send_keys, pane.send_input, pane.read, pane.report_agent, pane.report_agent_session,
pane.report_metadata, pane.clear_agent_authority, pane.release_agent, pane.close,
events.subscribe, events.wait, pane.wait_for_output,
integration.install, integration.uninstall,
plugin.link, plugin.list, plugin.unlink, plugin.enable, plugin.disable,
plugin.action.list, plugin.action.invoke, plugin.log.list, plugin.pane.open, plugin.pane.focus, plugin.pane.close
```

- Herdr は主要 CLI エージェントの検出 manifest を同梱（`claude` `codex` `gemini` `cursor` `devin` `copilot` `amp` `grok` `cline` `opencode` ほか、`server.agent_manifests` で確認）。

## 6. 実装者向け gotcha 一覧（事実のみ）

1. 通常リクエストは **1 接続 1 往復**で切断される。購読のみ接続維持。
2. **命名の非一貫性**: `events.subscribe` の topic は dotted（`pane.agent_status_changed`）、push イベントの `event` 名も dotted だが一部は snake（`pane_closed`）、`events.wait` の `match_event.event` は snake（`pane_agent_status_changed`）を要求（ただし `events.wait` 自体は未実装）。
3. **`events.wait` は未実装**（`not_implemented`）。待受は `events.subscribe` か `pane.wait_for_output`。
4. **購読は overflow でサイレントロス**（通知なし）。全遷移受信は保証されない。
5. **agent-status の CLI wait は edge-triggered**（現在値一致では発火しない）。
6. `pane.read source=recent` は**スクロールアウト未発生時に空**。ライブは `visible`。
7. `pane.report_agent` の受理 state は **idle/working/blocked/unknown の 4 値**。`done` は push 不可。
8. `pane.split` の direction は **`right|down` のみ**。
9. error `id` はパース失敗時に空文字になる。

---

### 付記: 検証で作成したリソースは全て後始末済み

`herdr-spike` セッションの workspace/tab/pane は検証末尾で `pane.close` → `tab.close` → `workspace.close` により破棄し、`pane.list` が空を返すことを確認した。稼働中の claude-org broker/tmux セッションには一切接触していない。
