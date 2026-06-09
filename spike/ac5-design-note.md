# AC-5 完動ゲート dogfood — 設計ノート（codex design review 反映版 / 実装着手前）

Epic #6（Plan B / renga 依存解消）の最終ゲート = 完動ゲート（Issue #5）。Phase 1-4 で
broker + tmux/WezTerm adapter + ペイン操作6面/監視 が揃った前提。本ノートは AC-5 dogfood
harness（`spike/run_ac5.py` + `tests/test_broker_dogfood.py`）の設計を実装前に固定する。

> **codex design review 1 周（2026-06-10）反映済み**: Blocker 2 / Major 7 / Minor 3 / Nit 1。
> 最重要は (B1) AC-5-resume が既存 `suspend()`=未読 queue 破棄（broker.py:443）と矛盾 → resume を
> 「破棄 + 新 token / 新 queue で成立 + stale 非継承」に再設計（むしろ isolation 証明が強化）、
> (B2) stall は観測だけでなく**観測後の broker 成立物（escalation enqueue）**を必須 assert 化。
> 各障害系を Phase 4 の再観測ではなく **AC-5 固有の broker 成立物**まで assert する形に強化した。

## 0. 完了基準（Issue #5 の 4 項目）と本 harness の対応

| # | Issue #5 完了基準 | AC-5 検証項目 | 方式 |
|---|---|---|---|
| 1 | backend(tmux)のみ・renga 不使用で 委譲サイクルを**複数回**完走 | AC-5-multi（3 サイクル連続 + cross-cycle isolation） | B（FakeAdapter）が本体 / 実 tmux smoke（cat）は backend 実在性の補助証跡 |
| 2 | 障害系 broker 成立: stall検出 / escalation / handover / resume | AC-5-stall / -escalation / -handover / -resume | B（FakeAdapter / broker API 直駆動） |
| 3 | 課金中立の実測（対話 TUI のみ・ヘッドレスに落ちない） | AC-5-billing（CI: argv builder 構造 assert）+ 実 claude idle attestation（手動） | CI で構造 assert / 手動で実測 attest |
| 4 | 設計書最終版（tmux 格上げ + Phase 結果反映） | §8 文書更新 checklist | 文書 |

> 役割分担（codex Minor 反映）: **方式 B（CI 常設）= 委譲サイクル本体 + 障害系 + argv 構造の決定的構造実証**。
> **手動 real-tmux = backend 実在性（cat）+ 課金中立の実測 attestation（実 claude idle）**。基準3 の「実測」は
> 手動ランナーが担い、CI は構造保証に閉じる。

## 1. 既到達点（Phase 1-4）と AC-5 の差分

Phase 1-4 が既に実証済み（**焼き直ししない**）:
- Phase 1/2: 実 Claude TUI で AC-1（ナッジ4状態）/ AC-2（起動・接続チェーン）。**実セッション往復の実在性**は既証。
- Phase 3: messaging 6 経路 1 サイクル（方式 B）。token 帰属 / なりすまし不可 / token ライフサイクル。
- Phase 4: ペイン操作6面 + 監視 1 サイクル（方式 B + 実 tmux smoke）。stall/承認待ちの独立観測・poll_events 合成・権限分離。

**AC-5 が新規に足すのは以下のみ**:
1. **複数サイクルの連続完走 + cross-cycle isolation**: 単一 broker / adapter 上で N サイクルを back-to-back で回し、
   サイクル間で token / handle / inbox / nudge dedup / event cursor が漏れないことを構造的に示す。Phase 4 は 1 サイクル。
2. **障害系4種を broker 成立物まで exercise**: 観測（Phase 4 既証）に留めず、**観測後の broker 経由の成立物**
   （stall→escalation enqueue / escalation の人間返答 worker 転送 + at-most-once / handover 中の監視 cursor 不喪失 /
   resume の新 lifecycle 成立 + stale 非継承）を必須 assert 化する。
3. **課金中立の実測 attestation**: argv builder の構造保証（CI）+ 実 claude idle ペインの描画観測（手動）。

## 2. 方式の選択（コスト判断 — 窓口/人間承認事項）

Phase 3/4 の前例（無課金・決定的・CI 可・prose 非破壊）を踏襲する。

- **Primary = 方式 B（FakeAdapter / broker queue 統合）**: 4 基準の**構造実証**（基準3 は argv 構造のみ。実測は §4）。決定的・CI 常設可・無課金。
- **Secondary = 実 tmux smoke（`cat` プローブ / 無課金）**: 多サイクル + 障害系の実 backend 実在性の補助証跡。
  sandbox の unix socket 制約のため CI 非常設（Phase 4 AC-4-real-tmux と同じ。手動ランナー / sandbox 無効で実行）。
- **実 Claude worker での複数サイクル = 非推奨（既証の焼き直し + token コスト）**。実セッション往復の実在性は
  Phase 1/2 AC-1/AC-2 で既証。複数サイクルを実 Claude で回しても**新規の構造的証拠は増えず token のみ消費**する。
- **課金中立の実測だけは実 claude を 1 ペインだけ使う**（§4 基準3）。**プロンプトを submit しない idle spawn**で、
  active な推論 turn を発生させない想定（課金メーターの検証ではなく、**対話 TUI が起動しヘッドレスに落ちていない**ことの attestation）。

### サイクル数の提案
- 方式 B: **3 サイクル**（"複数回" の最小妥当値。1→2 で cross-cycle、2→3 で反復安定性）。
- 実 tmux smoke: **2 サイクル**（cat プローブ。spawn→監視→close→次サイクル spawn が同一 broker で連続することを実機で）。

## 3. harness 構造（`spike/run_ac5.py`）

Phase 4 の `Cycle`（broker + FakeAdapter + 役割 token 結線）を再利用する。新規検証関数:

### AC-5-multi（複数サイクル連続完走 + cross-cycle isolation）
単一 `Cycle` 上で secretary/dispatcher を常駐させ、worker を **3 回** spawn→監視→完了報告→close する。
各サイクル k: `delegate(secretary→dispatcher)` → `spawn_agent(worker-k, balanced)` → `pane_started` 観測 →
`inspect_pane` で承認待ち/busy を独立観測 → `完了報告(worker-k→secretary)` → `close_pane`（token revoke + `pane_exited`）→
`retro gate(dispatcher→secretary)`。

**cross-cycle isolation assert（codex Major 反映で拡充）**:
- **inbox 分離**: サイクル k の `drain(secretary)` は k の完了報告 1 通のみ（前サイクル残留・先取りなし）。
  worker-k は spawn 時に dispatcher からの初期指示を 1 通受け、drain 後は空（漏れ込みなし）。dispatcher inbox の
  delegate / retro gate も各サイクルで過不足なく drain。**各サイクル終了時に全関係 inbox が empty**。
- **token 分離**: worker-k の token は close 後 `token_revoked` のまま（後サイクルで蘇生しない）。
- **handle / native id 分離**: FakeAdapter に **native id 再利用を強制**（close 後に同名 native を次 spawn で再発行）させ、
  **旧 handle での `inspect_pane` / `close_pane` が失敗**する（stale handle が新 pane に誤命中しない、Phase 4 round-2 修正の回帰）。
- **nudge dedup 残留なし**: close→次 spawn で nudge thread / dedup エントリが新 lifecycle に持ち越されない（Phase 4 round 3/4 の回帰）。
- **agent_id 再 spawn 規律**: 同一 active `agent_id` の二重 spawn は `[name_in_use]` 拒否（Phase 4 round 3/4）。
  サイクルは worker-k で名前を変えるが、**同一名で意図せず再 spawn した場合の二重発行拒否**も 1 ケース確認。
- **event cursor**: 各サイクルで cursor を進めた後、**次サイクルの `pane_started`/`pane_exited` のみ**が観測される
  （古い since の replay は許容＝cursor モデルとして正しいが、新 cursor 起点では旧 lifecycle が混ざらないことを assert）。

### AC-5-stall（連続 busy の独立観測 → **broker 成立物 = escalation enqueue**）
worker を**連続 busy**（flip させない）にし、dispatcher が `inspect_pane` を stall 閾値（既定 3 回連続 busy）まで
独立観測（自己申告に依らない）→ **stall 決定を broker 経由で成立**させる:
- dispatcher が stall 判定後、**secretary へ escalation を enqueue**（"worker-k が stall（連続 busy N 回）。介入要否?"）。
- assert（codex Blocker 2 反映 = 観測後の成立物を必須化）:
  - busy が閾値連続 → stall 判定 true。idle / input_pending では stall 判定 false（誤検出なし）。
  - stall 判定 → secretary inbox に escalation 1 通（`from_id`=dispatcher、token 由来）。
  - （任意拡張）worker へ介入 nudge を 1 回注入し idle 静止後配達される（介入経路も broker で成立）。

### AC-5-escalation（判断仰ぎの defer-then-deliver + 帰属 + **人間返答の worker 転送 at-most-once**）
secretary を busy/input_pending にした状態で worker→secretary に「判断仰ぎ」enqueue → defer →
secretary idle 遷移後に配達 → secretary が「人間に上げる」→ **人間返答を worker へ broker 経由で転送**。
- assert（codex Major 反映 = 一連の障害系として固定）:
  - 判断仰ぎが secretary busy 中 defer ≥1 回 → idle 後 1 通配達、`from_id` token 由来（worker）。
  - なりすまし: args に `from_id`/`from_name` 偽装注入しても broker が token 由来で上書き（Phase 3 既証の AC-5 文脈での再確認）。
  - **人間返答転送**: secretary→worker enqueue → worker 側 `drain` で 1 通取得（`from_id`=secretary）→ **2 回目 drain は空（at-most-once）**。

### AC-5-handover（secretary↔dispatcher の broker 経由引き継ぎ + **監視 cursor 不喪失**）
secretary が ops tier の `inspect_pane(dispatcher)` で prompt 状態観測 →
`send_keys(dispatcher, text="/clear", enter=true)` → prompt 空を inspect 確認 →
`send_keys(dispatcher, text="/dispatcher-resume", enter=true)`。
- assert（codex Major 反映 = cursor 不喪失を実証）:
  - 一連が ops tier で往復。**dispatcher ペインは生存**（`pane_exited` を emit しない・handle 安定）。
  - **handover 中に worker の lifecycle イベント（`pane_exited` または `pane_started`）を発生**させ、
    **handover 前 cursor から poll して取りこぼさない**（ペインを閉じないため監視 gap が生じない）。
- FakeAdapter は send_keys を記録、dispatcher pane を kill しないことで「ペイン保持」を表現。
- 注: 実 `/dispatcher-resume` の context リセットは dispatcher Claude 側の所作。broker 面の責務は
  **inspect_pane + send_keys が ops tier で成立し、ペインを閉じず監視を継続したまま引き継げる**こと。

### AC-5-resume（suspend = 全 revoke + 未読破棄 → **新 token / 新 queue で成立 + stale 非継承**）
**codex Blocker 1 反映**: 既存 `suspend()` は `revoke_token()` 経由で**未読 queue を破棄**する（broker.py:443、
Phase 3 で「退役 agent_id への未読を新 token に漏らさない」ために導入した正しい挙動）。よって resume の証明は
「未読 drain 継承」ではなく **「破棄 + 新 lifecycle で成立 + stale 非継承」** とする（むしろ isolation 証明が強い）。
- 1 サイクル稼働中に secretary 宛へ未配達を 1 通積む → `broker.suspend()`:
  - 戻り値 = revoke した token 数（全役割分）を assert。
  - **旧 token での送信/呼出は `token_revoked` で拒否**。
  - **suspend 前の未配達は破棄される**（既存方針）。
- resume = 各役割の `issue_token` 再発行（**別 token**、旧 token 再利用不可）→ register_local:
  - 新 token で送受信が成立（新規メッセージ enqueue→drain が通る）。
  - **suspend 前の未読が新 bind に漏れ込まない**（新 queue は空。stale 非継承）。
- assert: suspend 後 旧 token 全 revoke / 未読破棄 / resume 後 新 token 稼働 / 旧 lifecycle の未読が新 lifecycle に非継承。

### AC-5-billing（課金中立 = 対話 TUI argv builder の構造保証 / CI）
`broker.spawn_agent` の **argv builder を直接検査**（手動 1 ペインだけに依らず範囲を広げる、codex Major 反映）:
- 生成される `launch_argv` が `claude` 本体 + `--mcp-config <0600 path>` のみ。
- `-p` / `--print` / `--headless` / `--output-format` / Agent-SDK 系フラグを**含まない**（ヘッドレス落ちの構造的排除）。
- **role 差分（worker/dispatcher/secretary 等）があっても禁止 flag が混入しない**ことを各 role で確認。
- spawn_agent は argv をサニタイズして MCP 応答に token を載せない（Phase 4 既証の再確認）。

## 4. 実 tmux 手動ランナー（sandbox 無効 / 無課金 / 基準1・3 の実機 attestation）

`run_ac5.py --real-tmux`（既定 off。CI 非常設、Phase 4 と同じ方針）:
- **多サイクル smoke（cat / 無課金）**: 実 tmux で spawn→list_panes(geometry)→send_keys→inspect→poll_events
  →close を **2 サイクル連続**で往復（基準1 の実機証跡。委譲サイクル本体は FakeAdapter 側。これは backend 実在性の補助）。
- **課金中立 実測 attestation（実 claude 1 ペイン idle）**:
  1. 実 tmux に `claude`（**対話 TUI / プロンプト未投入**）を `spawn_agent` 経由で spawn（`--mcp-config` で broker 接続 = 起動チェーンも兼ねる）。
  2. **実 argv を観測**（adapter 記録 / `ps` で起動 argv を取得）し、`spawn_agent` 由来かつ headless/print 系 flag を含まないことを確認（codex Major 反映）。
  3. `inspect_pane` で TUI welcome / folder-trust prompt / `❯` プロンプトの**描画を観測**（ヘッドレス print-and-exit なら描画されない）。
  4. **user turn を一切 submit しない**（active な推論 turn を発生させない想定）。idle のまま観測のみ。
  5. `close_pane` で退役。
  - 実測できるのは「**broker の実 spawn argv が対話 TUI で headless/print 系でなく、画面が idle prompt まで描画された**」こと。
    これを「対話 TUI のみ・ヘッドレスに落ちていない」の実測 attestation とする（**課金メーターの検証ではない**、codex Major/Nit 反映）。

## 5. CI 常設 / 非常設の線引き（Phase 4 踏襲）

- `tests/test_broker_dogfood.py`: AC-5 の方式 B 検証（FakeAdapter / 決定的）を `unittest discover` で常設化。
- 実 tmux smoke（cat）+ 実 claude idle attest: sandbox unix socket 制約により CI 非常設。`run_ac5.py --real-tmux` 手動ランナー。

## 6. 既知の非対象（ja 不可触制約 / Epic #6 完動ゲート前）

- prose 書き換え・契約改訂（Set D/C・non-goals §12）は本体取り込みスコープ。本フォークでは行わない（Phase 3/4 と同じ）。
- WezTerm 実機 AC は Issue #9（Windows 専用）。本ゲートの backend は tmux 実機で実証（Phase 2/4 と同じ人間承認読み替え）。
- IME 状態2（変換中）の自動化は不能（既証）。本ゲートのスコープ外。

## 7. 窓口/人間に承認を仰ぐ事項（高コスト実行に入る前）

(a) サイクル数: 方式 B = 3 / 実 tmux smoke = 2 で妥当か。
(b) worker 種別: **シミュレート（方式 B）+ cat プローブ**を primary とし、実 Claude は**課金中立 attestation の idle 1 ペインのみ**。
    実 Claude で active な複数サイクルを回すか否かは人間判断（推奨: 回さない = 既証の焼き直し回避 + 無課金）。
(c) 課金中立の担保: argv builder 構造 assert（CI）+ 実 claude idle spawn の実測 attestation（turn 非消費 / argv・描画観測）。
(d) 障害系4種: stall=連続busy独立観測→escalation enqueue / escalation=defer-then-deliver+帰属+人間返答 worker 転送(at-most-once) /
    handover=inspect+send_keys でペイン保持引き継ぎ+監視 cursor 不喪失 / resume=suspend(全revoke+未読破棄)→token再発行→新lifecycle成立+stale非継承。すべて broker API で成立。

## 8. 完了基準4「設計書最終版」更新 checklist（codex Major 反映）

実装・検証後に `docs/design/renga-decoupling.md` へ反映する項目:
- [ ] AC-5 結果（dogfood 完走 / 障害系4種 GO / 課金中立 attestation）を §7 系（Phase 計画と移行完了判定基準）に追記。
- [ ] tmux を正準 backend として確定した旨を能力表 §4.7 周辺で最終化（Phase 2/4 で実測済みの再掲ではなく Issue #5 GO の確定）。
- [ ] dogfood harness（`run_ac5.py` / `test_broker_dogfood.py`）の存在と検証方式を明記。
- [ ] 既知制限（実 Claude 複数サイクル非実施 / 課金中立は attestation / WezTerm 実機=Issue #9 / prose・契約改訂=本体取り込みスコープ）を明記。
- [ ] **Issue #5 = 完動ゲート GO 判定**を文書に確定（フォーク側 Epic #6 足切り通過）。
- [ ] `spike/RESULTS.md` に AC-5 節（Phase 5 相当）を追加し GO/NO-GO と根拠を記録。
