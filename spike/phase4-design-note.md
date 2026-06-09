# Phase 4 設計ノート（実装前 codex design review 用）

Epic #6 Plan B Phase 4: ペイン操作 6 面を broker + adapter 経由に配線替え。
SoT: `docs/design/renga-decoupling.md` §4.2 / §4.6 / §4.7 / §7.4、Set D（backend-interface-contract）。
検証方式は Phase 3 と同じ **方式 B**（FakeAdapter / 無課金・決定的・CI 可）を踏襲する。

## スコープ（Issue #4 の 4 項目）

1. ペイン操作 6 面 = `spawn_agent` / `close_pane` / `list_panes` / `inspect_pane` / `send_keys` /
   `poll_events` を broker MCP surface に追加し、adapter 経由で実装する。
2. `poll_events` のイベント合成（list_panes 差分から `pane_started` / `pane_exited` /
   `events_dropped` を合成）。pane_exited 取りこぼしが list_panes reconcile で回復すること。
   dispatcher 監視ループ（3 分 cadence）の正しさを損なわないこと。
3. balanced split が backend の geometry 情報で現行（renga）と同等に機能すること。
4. dispatcher 向け broker MCP の最小 surface を確定（worker / curator には非公開、権限分離）。

完了基準: backend のみ（renga 不使用）で delegate → spawn → 監視（stall 検出 / 承認待ち観測）→
完了報告 → CLOSE_PANE → retro の 1 サイクルが AC harness（`run_ac4.py` 新規）で完走。

## 設計判断

### D1. role-scoped tool 公開（item 4・権限分離の構造化）

`AgentBind.role` を tier に写す。`tools/list` は tier でフィルタし、`call_tool` は tier 外ツールを
新コード `tool_forbidden`（Set D 6.2 の MAY-add 範囲内）で拒否する。worker/curator の token では
pane 操作ツールが **そもそも見えず・呼べない**（許可設定ではなく構造的遮断、§4.2 の狙い）。

| tier | role | 公開ツール |
|---|---|---|
| messaging | worker / curator | send_message / check_messages / list_peers / set_summary |
| ops | dispatcher | messaging + list_panes / inspect_pane / send_keys / poll_events / close_pane / spawn_agent / set_pane_identity |
| ops+ | secretary | ops と同一（generic spawn_pane は将来追加・本 Phase は非実装と明記） |

- **6 面 = `spawn_agent` / `close_pane` / `list_panes` / `inspect_pane` / `send_keys` / `poll_events`**
  （Issue #4 item 1）。`set_pane_identity` は Set D Surface 1.8 継承を **6 面と同時公開**する ops tier
  ツールであり「6 面」のカウント外（codex Minor 反映）。
- **wire 形状（codex Minor 反映）**: tier 外ツール呼出は MCP tool result `isError:true` +
  本文 `[tool_forbidden] ...`（既存 unknown_tool / auth と同形、`[<code>]` で機械分岐可）。併せて
  `tools/list` から tier 外ツールを除外し、構造的にも見えなくする。
- **role 信頼境界（codex Minor 反映）**: `spawn_agent` は ops tier の caller のみ。発行する新 token の
  role は caller が指定するが worker/curator tier に閉じ、caller 自身の tier 昇格はできない（監査点として明記）。

### D2. poll_events 合成（item 2）— codex Major 反映

broker に in-memory のイベント列 `_events: list[Event]`（ring cap 1000）、既知 pane の **record map**
`_known_panes: dict[native_id -> {name, role, agent_id}]`、単調 seq、累積ドロップ数 `_dropped_total` を持つ。
**唯一の合成点は `_reconcile_panes()`、かつ `_events`/`_known_panes`/seq/ring-trim は単一 `self._lock` 下**
で扱う（ThreadingHTTPServer 下で poll_events/spawn_agent/close_pane が同時に reconcile しても、
`_known_panes` 更新前の同一差分を二重 emit しない = pane_exited exactly-once。Set D 3.1）。

```
with self._lock:
  panes = adapter.list_panes()          # 例外時は何も合成しない（adapter_unavailable 扱い、誤合成回避）
  current = {native_id: meta}            # meta は bind 表から name/role/agent_id を解決
  for nid in current - known: emit pane_started(nid, meta)
  for nid in known - current: emit pane_exited(nid, known[nid])  # exit 後も meta を payload に載せる
  known = current
```

- **Event payload**: `{seq, type, pane_id(native), id(broker handle int), name, role, agent_id, ts}`。
  exit 後は list_panes から name/role を復元できないため、`_known_panes` に保持した meta を載せる
  （dispatcher の `name == "worker-{task_id}"` 照合・通知に必要。codex Major 対応）。
- **初回（since 省略）= 「今以降」**: baseline を張るだけ（emit せず）、`next_since` = 現在の最大 seq。
  Set D 3.1 の「初回は履歴 replay しない」を満たす。
- `spawn_agent` / `close_pane` は実行後に `_reconcile_panes()` を呼び、対応イベントを即時合成する。
- **取りこぼし回復**: イベントの唯一の出所が list_panes 差分なので、close を broker 経由「以外」で
  起こして（クラッシュ / 直 kill）も、次の reconcile で pane が消えていれば pane_exited が必ず合成
  される（構造的に回復）。known map で dedup するため二重 emit しない。
- **events_dropped（count 明文化、codex Major 対応）**: ring trim 時に捨てた件数を `_dropped_total` に
  加算。poll で `since` < 最古保持 seq を検出したら、合成 1 件 `{type:"events_dropped",
  count: (最古保持 seq - since 以降にドロップした件数), ...}` を **events[] 先頭**に置き、`next_since` =
  最古保持 seq とする（caller はそこから resume + list_panes reconcile）。Set D 3.1 Q9 best-effort + reconcile。
- long-poll: timeout_ms（≤30000 にクランプ）まで interval で reconcile を回し、イベント発生で早期 return。
  `types[]` フィルタ時もカーソルは filtered-out を越えて進める（重複スキャン無し）。

### D3. balanced split（item 3）— codex Blocker 反映

**現行 renga の balanced split SoT は `claude_org_runtime.dispatcher.runner.choose_split(panes)`**
（`pane-layout.md` §「ワーカーの balanced split 戦略」が明示、runtime が正準・doc は drift 許容）。
最大ペイン選択は不十分（role priority / MIN_PANE / SECRETARY 保険 / dispatcher 隣接 / `(priority desc,
metric desc, id asc)` sort / capacity 検出を欠く）。よって **broker は choose_split を再利用する**:
「現行同等」を再実装ではなく同一関数で構造的に保証する（doc prose は `_ROLE_PRIORITY` や
`SECRETARY_MIN_WIDTH=120` で runtime と既に drift しており、prose 移植は誤り）。

- broker は adapter geometry（`width`/`height`、`left/top`→`x/y`、`active`→`focused`）と bind 表の
  `name`/`role` を `runner.Pane` 列に正規化 → `choose_split` → `SplitChoice`（target/direction）。
- `choose_split` が `None` を返す = 候補空 = `SPLIT_CAPACITY_EXCEEDED`（escalate、spawn 中止）。Set D /
  verification.md 2.1 の k=9 escalate と同型。
- 定数（MIN_PANE 20/5・SECRETARY_MIN 120/30・role priority）は runtime SoT をそのまま使う（drift 排除）。
- adapter には `split(target, argv, cwd)` を追加（tmux=`split-window -t`、WezTerm=`split-pane`）。
- pane id: choose_split は int id を要求するため broker は native id（tmux `%N` 等）↔ int handle を
  対応付けて `choose_split` に渡し、返却 target を native に戻して adapter.split を呼ぶ。
- 依存: `claude_org_runtime`（pyproject 既存依存）を lazy import。未導入時は明示エラー。

### D4. inspect_pane / send_keys

- `inspect_pane(target, lines?, include_cursor?)`: broker が adapter.get_text で grid scrape、`lines` で
  末尾 N 行トリム、`include_cursor` は list_panes の cursor_x/y（tmux 同梱、WezTerm は欠落 → 省略）。
  adapter 変更不要（get_text 既存）。
- `send_keys(target, text?, keys?, enter?)`: adapter に `send_keys` プリミティブを追加。tmux は send-keys の
  キー名語彙（Enter/Tab/BTab/Escape/C-x/方向キー…）にネイティブ対応。Set D 1.9 の語彙を写す。

### D5. token / lifecycle 連携

`close_pane`（既存内部 API）は MCP 公開時も「kill → pane_exists 確認 → revoke」を踏襲し、加えて
`_reconcile_panes()` で pane_exited を合成する。`spawn_agent` は adapter.split → `issue_token` →
`bind_pane` → reconcile（pane_started）→ pane_id 返却。

## AC-4（run_ac4.py / FakeAdapter、方式 B）

`FakeAdapter` を Phase 4 面（geometry 付き list_panes / split / send_keys 記録 / inspect 用 screen /
pane 生死）まで拡張し、以下を機械判定する:

- **AC-4-surface**: 6 面が ops tier で往復、worker token では `tool_forbidden`（権限分離）。
- **AC-4-events（codex Major 反映の手順固定）**: **baseline poll（since=None → next_since 取得）→
  spawn_agent → poll(since=next_since) で pane_started を観測**（spawn 後の since=None では観測しない）。
  pane_exited も同型。**直 kill（broker 非経由）の取りこぼしが reconcile で回復**、events_dropped→reconcile
  （count 付き）。pane_exited payload に name/role/agent_id が載ること。
- **AC-4-split**: `choose_split` 再利用で現行同等。role priority 順・SECRETARY 保険・MIN_PANE 下限・
  候補空 → `SPLIT_CAPACITY_EXCEEDED` を、verification.md 2.1 相当のスナップショットで機械判定。
- **AC-4-cycle**: delegate → spawn_agent（balanced）→ 監視（inspect_pane で stall / 承認待ち観測）→
  完了報告（messaging）→ close_pane（pane_exited + token revoke）→ retro gate、の 1 サイクル完走。
- **AC-4-monitor-cadence**: 3 分 cadence を縮約した反復 reconcile で pane_exited 取りこぼしが
  次サイクルまでに回復（監視ループの正しさ）。

**実機 smoke（codex Major 反映 / スコープ強化）**: SoT §7.4 は「該当 backend 実機で 1 サイクル完走」を
要求する。本環境は Linux/WSL2 のため正準 backend は **tmux**（WezTerm は Windows 専用）。`run_ac4.py` に
`--real-tmux` smoke を追加し、実 tmux で spawn/split/send_keys/list_panes(geometry)/inspect/poll_events
reconcile を `cat` プロセス（無課金）で往復実証する（tmux 不在環境は graceful skip）。FakeAdapter は
決定的 CI 回帰として残す。

CI 常設: `tests/test_broker_phase4.py`（FakeAdapter）。既存 `run_ac3.py` / `test_broker_phase3.py` は不変。

## 非破壊・既知制限

- ja 本体不可触（Epic #6 完動ゲート前）。prose 書き換え・契約改訂は本体取り込みスコープ（本 Phase 非実施）。
- 実 Claude 課金実証（方式 A）は行わない（Phase 1/2 AC で実 TUI 既証）。実機 smoke は端末プリミティブのみ
  （Claude 起動・課金なし）。
- generic `spawn_pane`（secretary attention-watcher 用）は本 Phase 非実装（surface 表に予約のみ）。
  WezTerm split / send_keys は Windows 専用のため本環境では実機検証不可（実装は parity 目的、tmux を主）。

## codex design review（実装前 1 周）反映サマリ

- **Blocker（balanced split が現行非同等）** → choose_split 再利用で解消（D3）。
- **Major** → exactly-once（単一 lock）/ pane_exited payload に meta / AC baseline 手順固定 /
  events_dropped count 明文化 / 実機 tmux smoke 追加（D2・AC）で解消。
- **Minor** → tool_forbidden wire 形状固定 / role 信頼境界明記 / set_pane_identity 位置付け明記（D1）。
