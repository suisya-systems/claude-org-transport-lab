# broker dogfood 運用 runbook

`claude-org-runtime broker serve` は、renga-peers の代替となる **pure-backend 輸送層 (`org-broker`)** の daemon である。localhost の HTTP MCP サーバー + queue store + ナッジ配達を 1 プロセスで提供し、terminal adapter (tmux / WezTerm) を介して子ペインへナッジを注入する。本ドキュメントは Epic #6 Issue G(#515) の **本番 ja を `ORG_TRANSPORT=broker` で実走する前段**として、broker daemon の起動・停止・ライフサイクル・切戻しを運用手順に落としたものである。

設計 SoT は transport-lab `docs/design/ja-migration-plan.md` §5（ja 統合シーム）/ §5.5（併存・切戻し）/ §8 Issue G（dogfood ゲート）。契約面の正本は [`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md) Surface 8（broker auth & delivery、ratified 2026-06-14）。輸送両系の窓口運用差は [`CLAUDE.md`](../../CLAUDE.md)「輸送層（transport）両系」、spawn 儀式は [`.dispatcher/references/spawn-flow.md`](../../.dispatcher/references/spawn-flow.md) 3-3b を参照。

> **スコープと不可触制約**: 本 runbook は「実走を可能にする手順書」であり、**本番 ja の broker 実走（org-start ハイジャック）は後日のトラック 3（ユーザー hands-on）で行う**。本書の手順はすべて **テスト用 state-dir（`.state/broker/` ではない別ディレクトリ）** で daemon を起動・停止し、本番 `.state/` を汚さない前提で書く。**既定 `renga` は削除せず opt-in fallback として常時有効**（切戻しの安全装置）。

> **検証ステータス**: 起動・停止・ライフサイクル・dry-run の各コマンドは 2026-06-11 に **runtime 0.1.17** / tmux 3.2a / WSL2 の worker worktree 環境で実機検証済み（生ログの要点は各節に埋め込む）。§8 の attach 導線は 2026-06-13 に **runtime 0.1.22** で確認。**本書の broker surface 記述は runtime 0.1.22 に同期済み**（§1.1 setup / §2.1 serve flag に `--root-role` / `--root-cwd` / §3.6 journal / §3.8 admin RPC・sidecar / §5(4)-(5) sidecar disposal）。0.1.17 → 0.1.22 で増えた serve 面・admin 面・sidecar は `claude_org_runtime/broker/{cli,server,sidecar}.py`（0.1.22）から照合した（#515 dogfood の D2-D6 整理）。

---

## 1. 役割と前提

- **入力 / 制御**:
  - 環境変数 `ORG_TRANSPORT`（`renga` | `broker`、未設定 = 既定 `renga`）。daemon 自体は flag を読まないが、ja 側の生成器（§4）が flag に従って broker 面 allowlist を出す。
  - CLI 引数（`--port` / `--host` / `--state-dir` / `--backend` / `--no-nudge` / `--root-role` / `--root-cwd`、§2.1）。
- **出力 / 副作用**:
  - localhost HTTP MCP エンドポイント（既定 `http://127.0.0.1:48720/mcp`）と admin RPC エンドポイント（`/admin`、§3.8）。
  - queue store + JSONL journal（`<state-dir>/queue.jsonl`、既定 state-dir = `.state/broker`）。
  - daemon sidecar（`<state-dir>/daemon.json` 発見用メタ + `<state-dir>/admin.token` 0600 秘密、§3.8。graceful 停止で削除 / SIGTERM 残置）。
  - 子ペインへのナッジ注入（terminal adapter 経由、`--no-nudge` で無効化）。
- **依存方向（一方向）**: `broker → terminal / dispatcher.choose_split`。**claude-org-ja は broker を import しない**（flag 既定 renga で不活性）。
- **観察性（重要）**: tmux backend では broker が spawn する子ペイン（ディスパッチャー・ワーカー）が **detached な独立セッション**として起動し、デフォルトでは画面に出ない（窓口は logical pane で人間の手元 terminal に残る）。走行中の子ペインを read-only で覗く attach 導線は §8 を参照。
- **CLI 名の注意（重要）**: 起動コマンドは **`claude-org-runtime broker serve`**（top-level CLI のサブコマンド）。`claude-org-runtime-broker` は CLI の `prog` 名（`--help` のヘッダ表記）であって **console_script は存在しない**。`python -m claude_org_runtime.broker serve` でも等価に起動できる。

```
$ claude-org-runtime broker --help
usage: claude-org-runtime broker [-h] {serve} ...
    serve     org-broker daemon を localhost で起動する (Ctrl+C で停止)。
```

### 1.1 isolated venv のセットアップ（D6）

dogfood を本番環境から隔離するため broker org は **isolated venv（WSL/tmux 隔離 clone）** で走らせる。この venv には **`claude-org-runtime>=0.1.22`**（D2-D6 surface を持つ版）と **`core-harness>=0.3.2`** の両方が要る。

- **runtime は 0.1.22 以上が必須**: 本書の D2-D6（`--root-role` / `--root-cwd` / `/admin` RPC / sidecar）は **0.1.22 で入った surface**で、0.1.17-0.1.21 には無い。**ところが ja の現行 pin は `claude-org-runtime>=0.1.17,<0.2`（下限 0.1.17）** なので、`pip install -e .` は 0.1.22 を**保証しない**（下限 0.1.17 が解決されうる）。dogfood では明示的に `pip install 'claude-org-runtime>=0.1.22,<0.2'` で 0.1.22 以上を入れる（または `pip install -U` で最新 0.1.x に上げる）。下限を恒久的に上げる場合は `pyproject.toml` / `requirements.txt` の pin bump を別途行う（本 runbook の scope 外）。
- **core-harness は runtime 依存ではない**（runtime の `Requires-Dist` は `jsonschema` のみ。runtime は `core_harness` を import しない）。一方 **claude-org-ja 側のツール**（`tools/check_role_configs.py` 等）が `core_harness` を import するため、ja の org 運用には必須。`pip install claude-org-runtime` 単体では入らず隔離 venv で ja ツールが `ImportError` で落ちるので、**ja repo から `pip install -e .`** で `pyproject.toml` / `requirements.txt` の pin **`core-harness>=0.3.2,<0.4`** を解決する（最小構成で runtime だけ入れた場合は `pip install 'core-harness>=0.3.2,<0.4'` を明示的に足す）。
- pin 根拠: `core-harness` は 0.x なので x-bump（minor）が破壊的変更を含みうる方針で、`>=0.3.2,<0.4` に範囲固定している（`requirements.txt` のコメント / 設計 Q9-Q10）。

```bash
# isolated venv 例（隔離 clone のルートで）
python3 -m venv .venv && . .venv/bin/activate
pip install -e .   # core-harness>=0.3.2 と claude-org-runtime>=0.1.17 を pin どおり解決
# ただし -e . の runtime 下限は 0.1.17。D2-D6 surface には 0.1.22 以上が要るので明示で上書きする:
pip install 'claude-org-runtime>=0.1.22,<0.2'
# runtime だけ入れた最小構成なら core-harness も明示追加:
#   pip install 'core-harness>=0.3.2,<0.4'
# 確認:
python3 -c "from claude_org_runtime import __about__; print(__about__.__version__)"   # 0.1.22 以上
```

---

## 2. broker daemon 起動の実機確認

### 2.1 `serve` のオプション

```
$ claude-org-runtime broker serve --help
usage: claude-org-runtime broker serve [-h] [--port PORT] [--host HOST]
                                       [--state-dir STATE_DIR]
                                       [--backend {wezterm,tmux}] [--no-nudge]
                                       [--root-role {worker,curator,dispatcher,secretary}]
                                       [--root-cwd ROOT_CWD]
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--port` | `48720`（`DEFAULT_PORT`） | localhost bind ポート。`0` で ephemeral（OS 採番、起動ログの `listening on` に実ポートが出る）。 |
| `--host` | `127.0.0.1` | bind host。設計上 localhost 専用。 |
| `--state-dir` | `.state/broker`（`DEFAULT_STATE_DIR`、CWD 相対） | `queue.jsonl` / `daemon.json` / `admin.token` の書込先。**検証時は必ず別ディレクトリを渡す**（§2.3 / §7）。 |
| `--backend` | OS 自動選択（POSIX=`tmux` / Windows=`wezterm`） | terminal adapter。`VALID_BACKENDS = (wezterm, tmux)`。`--no-nudge` 時は無視。 |
| `--no-nudge` | （無効） | terminal adapter を生成せずナッジ配達を切る（**queue のみ**）。backend 非依存で疎通だけ見たいときに使う。 |
| `--root-role` | `worker`（`DEFAULT_ROOT_ROLE`） | 手動検証用 **root token を bind する権限 tier（auth_role）**。`tools/list` の公開面がこの tier で構造的に絞られる（§3.4）。受理集合 `ROOT_ROLE_CHOICES = (worker, curator, dispatcher, secretary)`。既定 `worker` = messaging 4 面で現行挙動不変、`secretary` で全 13 面。 |
| `--root-cwd` | （省略時は daemon の起動 cwd = `os.getcwd()`） | **root pane（人間駆動の窓口/secretary）の cwd を bind に持たせる**（runtime#61）。`spawn_*` の relative cwd はこの cwd を base に解決される（absolute は as-is）。relative を渡しても daemon 起動 cwd 基準で **absolute 化** して bind する（解決アンカーは常に absolute）。**daemon は session root から起動する運用契約**で、その起動ディレクトリが relative spawn の解決アンカーになる。session root 以外から起動する場合は本フラグで明示する。 |

> **0.1.17 → 0.1.22 の差分（D2）**: `--root-role` / `--root-cwd` は 0.1.22（runtime#61）で増えたフラグ。cwd を bind に持たないと人間駆動の窓口が投げる relative cwd の `spawn_*` が解決アンカーを失い拒否 / 誤 base に落ちる、が runtime#61 の根因。`--root-cwd` 省略時は daemon 起動 cwd を充てるため、**daemon は必ず session root から起動する**（または `--root-cwd` を明示する）。

`serve` は前景でブロックする。停止は (a) `Ctrl+C` / `SIGINT`、または (b) admin RPC `shutdown`（§3.8）の二経路（どちらも `run()` の `finally` を通り graceful に停止し、`broker_stopped` 記録 + sidecar 削除を行う）。起動時に admin token を 1 本生成して sidecar に 0600 で書き（§3.8）、手動検証用の root token を 1 本発行して `--mcp-config` に渡す JSON を標準出力に表示する:

```
org-broker listening on http://127.0.0.1:48803/mcp
admin RPC: http://127.0.0.1:48803/admin (token in /<state-dir>/admin.token)
daemon sidecar: /<state-dir>/daemon.json (backend=tmux)
queue store: /<state-dir>/queue.jsonl
manual test token (worker): <token>
root pane cwd (relative spawn anchor): /<root-cwd>
mcp-config: {"mcpServers": {"org-broker": {"type": "http", "url": "...", "headers": {"Authorization": "Bearer <token>"}}}}
root pane registered (logical, id=<pane_id>, role=worker)
```

> **起動時の副作用（0.1.22）**: 上記のとおり起動時に `<state-dir>/daemon.json`（発見用メタ、非秘密）と `<state-dir>/admin.token`（admin RPC 認証 token、0600 の秘密）を書き、root token を **logical pane** として pane 登録簿に載せる（`logical_pane_registered` journal、§3.6）。詳細は §3.8。

### 2.2 起動 / 停止コマンド（本番形）

本番 ja での起動（トラック 3、ユーザー hands-on）の形は次のとおり。**本節はコマンド形の提示で、本書の検証では §2.3 のテスト用 state-dir 版だけを実行する**。

```bash
# 起動（既定 state-dir = .state/broker、tmux backend 自動選択）
claude-org-runtime broker serve

# 停止（起動形態で場合分け）:
#   - 前景 serve（このシェルでブロック中）: Ctrl+C（SIGINT）。graceful 停止経路 =
#     run() の finally で stop() が走り journal 末尾に broker_stopped が 1 行残り、
#     daemon.json / admin.token sidecar も削除される。
#   - 背景 daemon（nohup ... & 等で起動）: SIGTERM を送る:
#       kill -TERM <pid>
#     背景 daemon に SIGINT（kill -INT）は効かずプロセスが残存する
#     （2026-06-13 切戻しドリルで 2 回再現）。背景は SIGTERM で止める。
#     ただし SIGTERM は run() の finally を経由しないため broker_stopped は emit されず、
#     daemon.json / admin.token sidecar も残置する（§5(5) で明示破棄）。
#     背景停止の確認は「プロセス消滅 + 未読突合 + sidecar 破棄」で行う（§5(4)/(5)）。
#   - graceful 代替（推奨、シグナル非依存）: admin RPC shutdown を叩く（§3.8）。
#     背景 daemon でも broker_stopped 記録 + sidecar 自動削除まで一括で済む。
```

### 2.3 テスト用 state-dir での起動→疎通→停止（本番 `.state` 不可侵の実証手順）

検証は **本番 `.state/broker/` を絶対に触らない**。一時ディレクトリを `--state-dir` に渡し、`queue.jsonl` がそのテストパスにのみ作られることを確認する。

> **cwd drift 注意（必須）**: `--state-dir` の既定は **CWD 相対** `.state/broker`。worker worktree と canonical な claude-org root では `.state/` が別物なので、相対パスを直接叩くと「どの `.state` を見ているか」が曖昧になり、誤った不可侵チェック / 本番 `.state` 汚染を招く。本書では **canonical root を絶対パス変数 `CANON_ROOT` で固定し、テスト state-dir も repo 外の絶対パス変数 `TEST_STATE` で固定**して、相対 `.state/broker` を素手で叩かない。

```bash
# 0) 前提変数を固定（相対パスを素手で叩かない）
CANON_ROOT=/home/happy_ryo/work/org/claude-org-ja   # 本番 .state/broker を持つ canonical root（環境に合わせる）
TEST_STATE=/tmp/claude/broker-smoke-A               # テスト用 state-dir（必ず repo 外の絶対パス）

# 1) テスト用 state-dir を用意（親ディレクトリ作成 + 既存ログ混入を避けるため未使用パスを使う）
mkdir -p "$TEST_STATE"
test -e "$TEST_STATE/queue.jsonl" && echo "WARN: 既存 queue.jsonl あり。別パスを使うか退避してから検証する" 

# 2) 起動（--no-nudge で backend 非依存に疎通だけ確認。-u で stdout を即時 flush）
python3 -u -m claude_org_runtime.broker serve \
    --state-dir "$TEST_STATE" --port 48799 --no-nudge
```

別ターミナル（または driver スクリプト）から、起動ログに出た token で HTTP MCP を叩く:

| ステップ | 期待 |
|---|---|
| `initialize` | `serverInfo = {"name": "org-broker", "version": "0.1.0"}` + `Mcp-Session-Id` ヘッダ採番 |
| `tools/list`（worker token） | `["check_messages", "list_peers", "send_message", "set_summary"]` の **messaging 4 面のみ**（tier gating、§3.4） |
| `tools/call send_message`（自分宛） | `{"ok": true, "delivered_to": "manual-test"}` |
| `tools/call check_messages` | 直前に送った `hello broker` を at-most-once で drain |

停止は serve に `SIGINT` を送る。**clean shutdown なら exit code 0**。

**`.state` 不可侵の確認（必須）**: 検証後に本番 `.state/broker/` が生成されていないことを確認する。queue は渡したテストパスにのみ書かれる。

```bash
# queue は TEST_STATE 配下にだけ存在する
ls "$TEST_STATE/queue.jsonl"
# 本番側（canonical root の絶対パス）が未生成であること。相対 .state/broker は使わない
test -e "$CANON_ROOT/.state/broker" && echo "NG: 本番 .state/broker が汚れた" || echo "OK: 本番 .state は不変"
# 現在の worktree 直下にも検証ゴミが落ちていないこと（CWD 相対既定の取り違え防止）
test -e "$PWD/.state/broker" && echo "NG: worktree 直下に .state/broker が生成" || echo "OK: worktree 直下も不変"
```

> **検証ログ（2026-06-11、実機）**: `--no-nudge` / `--backend tmux` の両系で `initialize → tools/list → send_message → check_messages` 往復が成功し、`SIGINT` で **exit 0**。`tools/list` は worker tier で messaging 4 面のみ。本番 `.state/broker/` は未生成（テストパスにのみ `queue.jsonl`）。tmux backend は live tmux server なしでも adapter が遅延生成され、起動・停止が成立する（ナッジを実際に注入する子ペインが無いので messaging probe は skip）。

---

## 3. start / stop / token / queue ライフサイクル

broker の内部状態遷移は `claude_org_runtime/broker/` の `server` / `store` / `tokens` / `surface` に分かれる。運用上把握すべき流れは次の 4 つ。

### 3.1 token 発行（`tokens.py`）

- spawn 時に **per-agent token** を 1 本発行する（`issue_token`、`secrets.token_urlsafe(32)`）。token ↔ `AgentBind`（`agent_id` / `name` / `role` / `auth_role` / `pane_id` / `cwd` / `kind`）。
- **`role`（表示専用、`set_pane_identity` で可変）と `auth_role`（不変の権限 tier、発行時確定）を分離**している。tier gating は `auth_role` のみで決め、表示 role の自己申告では昇格できない。spawn 子の `auth_role` は呼出元 tier で上限を切る（`capped_auth_role`）。
- `mcp_config_for(token)` が `--mcp-config` に渡す JSON を生成する（token を static header `Authorization: Bearer <token>` に埋める。env 参照 `${VAR}` は使わない）。
- journal: `token_issued`。

### 3.2 登録（`server.py` の HTTP handler）

- 子ペインの Claude / Codex が `initialize`（MCP）に到達した時点で `AgentBind.registered = True` になる（`registered_at` 記録）。**登録済み bind のみが配送先**になる（未接続 / DELETE 済み client への配送を防ぐ）。
- journal: `agent_registered`。

### 3.3 queue store + ナッジ配達（`store.py` / `server.py`）

- `send_message`（`enqueue`）は **token 由来の帰属**で entry を作る（自己申告不可）。宛先の registered 確認と queue append を**同一ロックスコープ**で原子的に行い、その後にロック外で `_journal` と `_trigger_nudge` を呼ぶ（queue 永続化と PTY 注入を結合させない / 非再入 Lock の二重取得デッドロック回避）。
- ナッジ配達は **定型 1 行のみ PTY 経由**で注入し、本文は通さない（受信側は `check_messages` で pull 取得 = push→pull モデル）。adapter 不通や対象未着のときは `nudge_defer_interval`（既定 2.0s）× `nudge_defer_max_tries`（既定 30）まで再試行する。
- `check_messages`（`drain`）は **at-most-once** で queue を空にして返す。
- journal: `message_enqueued` → `nudge_sent` / `nudge_deferred` / `nudge_failed` → `queue_drained`。

### 3.4 tier gating（`surface.py`）

公開面は `auth_role` で**構造的に**変わる（default-deny allowlist）。`tools/list` に出ないツールは呼んでも `[tool_not_authorized]` で弾かれる（allowlist は二重防御の片側）。

| auth_role tier | 公開面 |
|---|---|
| worker / curator / 未知 | messaging 4（`send_message` / `check_messages` / `list_peers` / `set_summary`） |
| dispatcher | messaging 4 + ops（`list_panes` / `inspect_pane` / `send_keys` / `poll_events` / `close_pane` / `set_pane_identity` / `spawn_claude_pane` / `spawn_codex_pane`） |
| secretary | dispatcher の面 + `spawn_pane`（secretary 専用） |

> `new_tab` / `focus_pane` は broker surface に**無い**（意図的除外）。初期 surface = 移植 12 面 + `spawn_codex_pane` = 13 面。

### 3.5 停止 / 失効

- graceful 停止（`run()` の `finally` → `stop()` + sidecar 削除）: graceful 停止経路は **(a) 前景 serve への SIGINT / Ctrl-C** と **(b) admin RPC `shutdown`（§3.8）** の 2 つ。どちらも `run()` が唯一の `stop()` 呼出元として `finally` を通り、`stop()` が HTTP server を shutdown + close して journal に `broker_stopped` を残し、続けて `daemon.json` / `admin.token` sidecar を削除する（`remove_sidecar`、§3.8）。**`broker_stopped` の emit と sidecar 削除は graceful 経路（SIGINT / admin RPC shutdown）でのみ起きる**。背景 daemon を `kill -TERM` で止めた場合は `run()` の `finally` を経由しないため `broker_stopped` は残らず、**`daemon.json` / `admin.token` sidecar も削除されず残置する**（停止確認はプロセス消滅 + 未読突合、後始末は sidecar の明示破棄で行う、§5(4)/(5)）。なお背景 daemon に SIGINT（`kill -INT`）は効かずプロセスが残存する（2026-06-13 切戻しドリルで 2 回再現）。
- session 終了（MCP `DELETE`）: 当該 bind の `session_id` を失効させ、`registered = False` に落とす（切断済み client を `list_peers` / 配送先に残さない）。journal: `session_closed`。
- pane クローズ（`close_pane`）: adapter で kill 後、registry pop と token revoke を 1 ロックスコープで原子的に行う。journal: `pane_closed` + event `pane_exited`。

### 3.6 journal イベント一覧（`queue.jsonl`）

`<state-dir>/queue.jsonl` に 1 行 1 JSON で追記される。運用での観測点:

```
broker_started → token_issued → logical_pane_registered (起動時の root pane)
  → agent_registered → message_enqueued
  → nudge_sent / nudge_deferred / nudge_failed → queue_drained
  → pane_spawned / pane_identity_set (spawn / identity 操作時)
  → session_closed / pane_closed → broker_stopped
```

> **0.1.22 で増えたイベント（D3）**: `logical_pane_registered`（起動時に root token を logical pane として登録、§3.8）/ `pane_spawned`（`spawn_claude_pane` / `spawn_codex_pane` / `spawn_pane`）/ `pane_identity_set`（`set_pane_identity`）。`broker_stopped` は graceful 停止（SIGINT / admin RPC shutdown）でのみ末尾に残る（§3.5 / §5(4)）。

> **検証ログ（実機、messaging 往復）**: `broker_started → token_issued → agent_registered → message_enqueued(chars=12) → queue_drained(count=1) → broker_stopped` を 1 サイクルで確認。

### 3.7 broker 追加エラーコード

renga コードに加え broker は次を返しうる。窓口 / ディスパッチャーは未知コードを default-branch で escalate に流す（[`CLAUDE.md`](../../CLAUDE.md)「エラー分岐」）。

| コード | 契機 |
|---|---|
| `[token_invalid]` | Bearer token が bind 表に無い / revoked（HTTP 401、JSON-RPC -32001） |
| `[session_invalid]` | `initialize` 前に他メソッドを呼んだ |
| `[tool_not_authorized]` | auth_role tier の公開面外のツールを呼んだ |
| `[no_backend]` | terminal adapter 不在（`--no-nudge` 起動）で pane 操作を呼んだ（= adapter_unavailable） |
| `[nudge_failed]` | ナッジ注入が defer 上限まで届かなかった |
| `[peer_not_found]` | `send_message` の宛先が registered な bind に無い |
| `[name_taken]` | pane name の重複 |
| `[admin_unauthorized]` | `/admin` RPC を admin token 無し / 不正 token で呼んだ（HTTP 401、§3.8） |

> 上表は `/mcp`（messaging / ops）面のコードに、admin 面の認証ゲート `[admin_unauthorized]`（運用頻度が高いため掲載）を加えたもの。admin 面（`/admin`）は per-agent bearer とは**別系統**の `admin_token` で認証し、`[admin_unauthorized]` 以外にも `[parse_error]` / `[invalid_params]` / `[unknown_admin_method]` / `[invalid_role]` / `[invalid_cwd]` / `[invalid_name]` を返しうる。**admin RPC のコード一覧は §3.8** を参照（経路が分かれるため上表には集約しない）。

### 3.8 admin RPC（token mint / graceful shutdown）と daemon sidecar

0.1.22 で **走行中 daemon を外部から制御する admin 面**と、**発見用 sidecar** が入った（runtime#61 / #63、`server.py` の `_handle_admin` / `sidecar.py`）。messaging / ops（`/mcp`）とは独立の制御面である。

**admin RPC（`/admin`）**:

- エンドポイントは `http://<host>:<port>/admin`（`broker.admin_url`）。`/mcp`（messaging / ops）とは別パス。
- 認証は per-agent bearer ではなく **`admin_token`**（`secrets.token_urlsafe(32)`、起動時生成）。`Authorization: Bearer <admin_token>` を定数時間比較（`hmac.compare_digest`）。**admin token 未設定なら経路ごと隠す（HTTP 404）**＝内部テスト用に admin 面を無効化できる。不正 / 欠落 token は HTTP 401 `[admin_unauthorized]`。
- メソッド（JSON-RPC 風 `{"method": ..., "params": {...}}`）:
  - `mint_token` — 走行中 daemon に対し新規 root token を mint する（`role` = auth_role）。root token と同様 spawn 子ではないため tier 上限切り（`capped_auth_role`）は適用せず要求どおりの tier で bind。`params.cwd` は relative spawn の解決アンカー（CLI と同様 absolute 化）。
  - `shutdown` — graceful shutdown を要求する。ack（`{"ok": true, "shutting_down": true}`）を先に返してから `request_shutdown()` を呼び、実停止（`stop()` + sidecar 削除）は `run()` の前景ループが行う（ハンドラスレッドから直接 `shutdown` を呼ぶデッドロックを避ける）。**シグナルに依存しない停止経路**で、SIGTERM/SIGINT を送りにくい環境（Windows 等）の graceful 停止手段になる。
- **`/admin` のエラーコード（§3.7 の `/mcp` 表とは別系統）**: 認証失敗 `[admin_unauthorized]`（401、§3.7 に掲載）に加え、JSON body 不正 `[parse_error]`（400）/ `params` が object でない `[invalid_params]`（400）/ 未知メソッド `[unknown_admin_method]`（400）。`mint_token` は引数検証で `[invalid_role]`（受理外 role）/ `[invalid_cwd]`（cwd が文字列でない）/ `[invalid_name]`（name が文字列でない）を返す（いずれも 400 / `{"ok": false, "error": ...}`）。窓口 / ディスパッチャーは未知コードを default-branch で escalate に流す（§3.7 と同じ方針）。

**daemon sidecar（`<state-dir>/` の 2 ファイル、`sidecar.py`）**:

| ファイル | 内容 | 秘密 | パーミッション | 停止時 |
|---|---|---|---|---|
| `daemon.json`（`SIDECAR_NAME`） | 発見用メタ（`pid` / `host` / `port` / `state_dir`(絶対) / `backend`(解決済み実値) / `started_at` / `journal_offset`） | 含まない | 通常 | graceful 停止で削除（SIGTERM で残置） |
| `admin.token`（`ADMIN_TOKEN_NAME`） | admin RPC 認証 token | **含む** | 0600（temp→atomic rename で torn read 回避。**Windows NTFS では read-only ビットのみ**で group/other read は本当には落ちない既知制限） | graceful 停止で削除（SIGTERM で残置 → §5(4)/(5) で明示破棄） |

- どちらも `os.replace` の atomic publish で公開し、部分書き / torn read を晒さない。`journal_offset` は run 開始時点の `queue.jsonl` バイト長で、停止確認（`broker_stopped` 検出）を当該 run のスライスに限定して**過去 run の残留による偽陽性を避ける**ための起点。
- **logical pane 登録**: 起動時に root token を pane 登録簿へ **logical pane** として載せる（`register_logical_pane`、journal `logical_pane_registered`）。`bind.pane_id = None` なので PTY ナッジは飛ばず（人間は `check_messages` で読む）、`list_panes` に窓口が出ることで子を 1 つだけ spawn した状態でも `close_pane` が `[last_pane]` 誤判定せず子を閉じられる（§8 の「窓口は logical pane」と整合）。

---

## 4. `ORG_TRANSPORT=broker` での settings 再生成 dry-run

Epic #6 D/E で入った **transport descriptor 駆動の生成器**を `ORG_TRANSPORT=broker` で dry-run し、broker 面 allowlist が出ることを確認する。**実ファイルは書かない**。

### 4.1 単一 SoT（descriptor）

ja 側の transport アクセサ [`tools/transport.py`](../../tools/transport.py) は runtime の transport surface descriptor（`claude_org_runtime.transport`）を唯一の SoT として consume する（ハードコードしない）。解決順は **explicit 引数 > `ORG_TRANSPORT` env > 既定 `renga`**。allowlist 生成は `claude_org_runtime.settings.generator.transport_allowlist(role, transport=...)` 経由。

### 4.2 role 別 allowlist の dry-run

```bash
# 既定 renga（無設定）と broker の射影を role 別に比較（read-only、書込み無し）
for role in worker curator dispatcher secretary; do
  echo "--- $role renga(default) ---"
  python3 -c "from claude_org_runtime.settings.generator import transport_allowlist as t; print(t('$role'))"
  echo "--- $role broker ---"
  ORG_TRANSPORT=broker python3 -c "from claude_org_runtime.settings.generator import transport_allowlist as t; print(t('$role'))"
done
```

| role | renga（既定） | broker（`ORG_TRANSPORT=broker`） |
|---|---|---|
| worker / curator | `mcp__renga-peers__*` 14 面 | `mcp__org-broker__*` messaging 4 |
| dispatcher | `mcp__renga-peers__*` 14 面 | messaging 4 + ops 8（`spawn_pane` を含まない） |
| secretary | `mcp__renga-peers__*` 14 面 | messaging 4 + ops + `spawn_pane` + `spawn_codex_pane`（13） |

> renga 既定は全ロール同一 surface（14 面）を allowlist で絞るモデル。broker は role tier を**構造的に**遮断するため、allowlist は二重防御の片側になる（安全側）。

### 4.3 `~/.claude/settings.json` の user_common allowlist 再生成 dry-run

[`tools/org_setup_prune.py`](../../tools/org_setup_prune.py) `--user-common-allowlist` は user_common（`~/.claude/settings.json`）の MCP `permissions.allow` を active transport へ射影する。**検証では実 `~/.claude/settings.json` を触らないよう `--user-common-settings-path` でテスト用パスに向け、`--dry-run` を付ける**。

```bash
# テスト用 settings（renga エントリ入り）を用意して dry-run
TEST_SET=/tmp/claude/usercommon-settings.json   # 実 ~/.claude/settings.json ではない

# renga messaging エントリ入りのテスト settings を作る（空/不存在だと drop renga の期待出力にならない）
mkdir -p "$(dirname "$TEST_SET")"
cat > "$TEST_SET" <<'JSON'
{
  "permissions": {
    "allow": [
      "Bash(git status:*)",
      "mcp__renga-peers__send_message",
      "mcp__renga-peers__check_messages",
      "mcp__renga-peers__list_peers",
      "mcp__renga-peers__set_summary"
    ]
  }
}
JSON

# 既定 renga: strict no-op（ファイルは一切触らない）
python3 tools/org_setup_prune.py --user-common-allowlist --dry-run \
    --user-common-settings-path "$TEST_SET"

# broker: renga-peers を drop、org-broker messaging tier を保証（dry-run は表示のみ）
ORG_TRANSPORT=broker python3 tools/org_setup_prune.py --user-common-allowlist --dry-run \
    --user-common-settings-path "$TEST_SET"
```

期待出力:

```
# renga（既定）
[org_setup_prune] user_common allowlist: transport=renga (既定); no-op — ~/.claude/settings.json は不変 ...

# broker
=== user_common allowlist (transport=broker): /tmp/claude/usercommon-settings.json ===
  - mcp__renga-peers__send_message      （以下 renga messaging を drop）
  + mcp__org-broker__send_message       （以下 org-broker messaging を add）
  ...
```

> **検証ログ（実機）**: 既定 renga は strict no-op（テストファイル 1 byte も不変）。`ORG_TRANSPORT=broker` で renga messaging 4 → org-broker messaging 4 の差分を dry-run 表示。**`--dry-run` のため実書込みゼロ**（テストファイル内容の不変を確認済み）。`Bash(...)` 等の非 MCP エントリは順序を保って残る。

---

## 5. 切戻し 5 条件の具体コマンド化（SoT §5.5）

`ORG_TRANSPORT=broker` → `renga` への完全な切戻しは、flag 戻しだけでは**実行中の broker-spawned ペインが即座には復帰しない**（`--mcp-config` / pull 前提の prose を抱えたまま）。SoT §5.5 の **5 完了条件**を順に実行する。

> **前提変数（cwd drift 回避）**: 以下のコマンドは相対 `.state/broker` を素手で叩かない。daemon が `serve --state-dir` で実際に使った state-dir を絶対パス変数で固定し、canonical root も明示する。本番反映（トラック 3）では `BROKER_STATE` が本番 `.state/broker` を指す。
>
> ```bash
> CANON_ROOT=/home/happy_ryo/work/org/claude-org-ja   # canonical root（環境に合わせる）
> BROKER_STATE="$CANON_ROOT/.state/broker"            # daemon が serve 時に渡した --state-dir
> ```

### (1) flag 戻し

```bash
# env を renga（既定）へ戻す。次に spawn される pane から renga に向く。
unset ORG_TRANSPORT
# 永続シェル設定に書いていた場合はそこからも除去する:
#   grep -rn "ORG_TRANSPORT" ~/.bashrc ~/.zshrc ~/.profile
```

**チェック**: `python3 -c "from claude_org_runtime.transport import resolve_transport as r; print(r())"` が `renga` を返す。

### (2) 生成物の再生成（renga allowlist へ）

flag が renga に戻れば**生成器（role 別 `settings.local.json`）は恒等（bit 等価）**に戻る。生成物を実際に再生成して renga 面に戻す。

```bash
# まず dry-run で差分確認（broker 面が残っていれば renga へ戻す差分が出る）
python3 tools/org_setup_prune.py --all --dry-run

# 問題なければ適用（renga allowlist を書き戻す。.bak が残る）
python3 tools/org_setup_prune.py --all
```

**user_common（`~/.claude/settings.json`）は別扱い（重要）**: `--user-common-allowlist` は **renga モードでは完全 no-op**（renga allowlist の SoT は org-setup スキル + permissions.md であってこのツールではないため、ファイルに一切触れない）。したがって dogfood で broker を適用済み（`mcp__org-broker__*` が user_common に入っている）の場合、`--user-common-allowlist --dry-run` を renga で回しても **broker 面は戻らない**。user_common は以下のいずれかで明示的に戻す:

```bash
# 方法 A（推奨）: broker 適用時に作られた .bak を復元する
#   backup 命名は settings.json.bak.<YYYYMMDD-HHMMSS>（backup_path）
ls -t ~/.claude/settings.json.bak.* 2>/dev/null | head     # 直近の backup を確認
# cp <確認した .bak> ~/.claude/settings.json               # 内容を目視確認のうえ復元

# 方法 B: backup が無い場合は messaging 面を手動 swap（org-broker → renga-peers）
#   ~/.claude/settings.json の permissions.allow 内
#   "mcp__org-broker__{send_message,check_messages,list_peers,set_summary}" を
#   "mcp__renga-peers__..." に置換する（非 MCP エントリは触らない）
```

**チェック**: role 別 `settings.local.json` と **user_common（`~/.claude/settings.json`）の両方**に `mcp__org-broker__*` が残っていないこと。

```bash
# repo 配下の role 別 settings。glob (*/.claude/) は hidden role dir
# (.dispatcher/.claude/ / .curator/.claude/ 等) を拾わず、zsh では no-match で
# grep 自体が走らず誤って OK になる。glob を使わず repo root から再帰 grep する
# (grep -r は hidden dir も降りる)。settings*.json に限定して誤検出を避ける。
if grep -rl --include="settings*.json" "mcp__org-broker__" . 2>/dev/null | grep -q .; then
  echo "NG: repo 側に broker 面が残存:"; grep -rl --include="settings*.json" "mcp__org-broker__" . 2>/dev/null
else
  echo "OK: repo 側 broker 面なし"
fi
# user_common（ホームの settings.json）も忘れず確認する
grep -l "mcp__org-broker__" ~/.claude/settings.json 2>/dev/null && echo "NG: user_common に broker 面が残存" || echo "OK: user_common broker 面なし"
```

### (3) active な broker ペインの respawn（renga 経路で再起動）

実行中の broker-spawned ペインは flag 戻しでは復帰しない。renga 経路で suspend/resume または respawn する。

```bash
# 現状の broker ペインを把握（renga 窓口/ディスパッチャーから）
#   mcp__renga-peers__list_panes  でペイン一覧を確認
# broker token を抱えたペインを順に close → renga 経路で再 spawn（org-delegate の通常委譲フロー）
# pane control は dispatcher/secretary に閉じるため、messaging を先に renga へ戻してから pane を後追いする（§5.5 の 2 段）。
```

**チェック**: `list_peers` / `list_panes` に broker bind のペインが残っていない。

### (4) broker daemon の停止順序（残ペイン revoke → daemon stop）

**順序が重要**: 先に残ペインを revoke（close）して配送先から外し、最後に daemon を止める。

**停止シグナルは起動形態で場合分けする（2026-06-13 切戻しドリルの実測反映）**: 前景 serve は Ctrl-C（SIGINT）で graceful に止まり `broker_stopped` を emit するが、`nohup ... &` 等で背景起動した daemon に **SIGINT（`kill -INT`）は効かずプロセスが残存する**（ドリルで 2 回再現）。背景 daemon は **SIGTERM（`kill -TERM`）** で止める。ただし SIGTERM は `run()` の `finally`（= `stop()` + sidecar 削除の唯一経路）を経由しないため、**(i) `broker_stopped` が emit されず**（journal 末尾は `broker_started` / `token_issued` 等のまま）、**(ii) `daemon.json` / `admin.token` sidecar が削除されず state-dir に残置する**（D4）。`admin.token` は admin RPC の認証 secret なので、残置は (5) で明示破棄する。したがって停止確認手段も後始末も経路ごとに分ける。

> **背景 daemon を graceful に止めたいとき（推奨代替）**: SIGTERM の代わりに **admin RPC `shutdown`（§3.8）** を叩けば、`run()` の `finally` を通って `broker_stopped` 記録 + sidecar 自動削除まで一括で済む（シグナル非依存）。admin token は `<state-dir>/admin.token` から読む。SIGTERM で止めた場合のみ (5) の sidecar 破棄が必要になる。

```bash
# 1) 残っている broker ペインを close（token revoke される。close_pane の journal: pane_closed）
#    renga/dispatcher から各 broker ペインを close_pane する。
# 2) すべて revoke したら daemon を停止（起動形態で場合分け）:
#    - 前景 serve（このシェルでブロック中）: このコマンドは実行せず Ctrl-C（SIGINT）を打つ。graceful 停止。
#    - 背景 daemon（nohup ... & 等）: SIGTERM を送る。SIGINT（kill -INT）は効かない。
kill -TERM <broker_pid>   # 背景 daemon の停止。前景 serve なら代わりに Ctrl-C を打つ
# 3) 停止確認（経路で手段が異なる）:
#    a) graceful 停止（前景 SIGINT / Ctrl-C）した場合のみ journal 末尾に broker_stopped が残る:
tail -n 3 "$BROKER_STATE/queue.jsonl"
#    b) SIGTERM（背景 daemon）で止めた場合は broker_stopped が emit されないので、
#       プロセス消滅 + 未読突合で確認する（§5(5) の未読突合スクリプトと整合）。
#       SIGTERM 直後は終了処理中で誤判定しうるため短い timeout loop で消滅を待つ:
for i in $(seq 1 10); do
  kill -0 <broker_pid> 2>/dev/null || { echo "OK: daemon プロセス消滅"; break; }
  sleep 1
done
kill -0 <broker_pid> 2>/dev/null && echo "NG: daemon がまだ生きている"
#       未読突合（enqueued vs drained）は §5(5) のスクリプトを実行する（ここでは重複させない）。
```

> **runtime follow-up 候補（実装はこのタスクのスコープ外）**: runtime 側の SIGTERM ハンドラが `run()` の `finally` 経路（`stop()` + `remove_sidecar`）を通すようになれば、SIGTERM 停止でも `broker_stopped` emit と sidecar 自動削除（`daemon.json` / `admin.token`）が走り、**停止確認の場合分けも (5) の sidecar 手動破棄も不要**になる。本 runbook は手順の明文化に留め、runtime 実装は別 Issue 化を検討する。現状の graceful 代替は admin RPC `shutdown`（§3.8）。

### (5) 旧 token / queue store / sidecar の破棄確認（`.state/broker/` の未読・bind・sidecar 残存なし）

```bash
# 未読（enqueue されたが drain で消されていない message）が残っていないか journal を突合する。
# queue_drained は count=N を持つので「イベント件数」ではなく N の総和で比較する（複数 drain の誤判定回避）。
BROKER_STATE="${BROKER_STATE:?BROKER_STATE を先に固定する（§5 前提変数）}" \
python3 - <<'PY'
import json, os
p = os.path.join(os.environ["BROKER_STATE"], "queue.jsonl")
enq = drained_msgs = 0
try:
    for line in open(p, encoding="utf-8"):
        rec = json.loads(line); ev = rec.get("event")
        if ev == "message_enqueued": enq += 1
        if ev == "queue_drained": drained_msgs += int(rec.get("count", 0))  # N を合算
except FileNotFoundError:
    print("OK: queue.jsonl が無い（破棄済み）"); raise SystemExit
unread = enq - drained_msgs
print(f"enqueued={enq} drained_msgs={drained_msgs} unread={unread}")
print("OK: 未読なし" if unread <= 0 else f"NG: 未読 {unread} 件が残存（daemon 停止前に drain される必要）")
PY

# sidecar 残置の確認と破棄（D4）。graceful 停止（SIGINT / admin RPC shutdown）なら
# run() の finally が daemon.json / admin.token を自動削除済み。SIGTERM で止めた場合は
# 両者が残置するので明示破棄する。admin.token は admin RPC の認証 secret なので必ず消す。
for f in admin.token daemon.json; do
  if [ -e "$BROKER_STATE/$f" ]; then
    echo "残置: $BROKER_STATE/$f（SIGTERM 停止の名残。破棄する）"
    rm -f "$BROKER_STATE/$f"   # rm 不可環境では shred/truncate 等、運用ルールに従う
  else
    echo "OK: $f なし（graceful 停止で削除済み or 未生成）"
  fi
done

# per-agent token / bind 表はプロセス内 in-memory（daemon 停止で消える。永続化されない）。
# 永続するのは journal（queue.jsonl）と、SIGTERM 残置時の sidecar 2 ファイルだけ。
# queue store ファイルを破棄して跡を残さない（rm 不可環境では truncate / アーカイブ）:
#   mv "$BROKER_STATE" "$BROKER_STATE.archived-$(date +%Y%m%d)"   # または運用ルールに従い削除
```

> **token / bind / sidecar の永続性（D4）**: per-agent の `AgentBind`（token 値・bind 表）は daemon プロセスの in-memory のみで永続化されない（journal には `token_issued` の事実は残るが値は残らない）。**例外は admin token**: これは `<state-dir>/admin.token`（0600）として**ディスクに書かれる秘密**で、graceful 停止（SIGINT / admin RPC shutdown）なら `run()` の `finally`（`remove_sidecar`）が `daemon.json` とともに自動削除するが、**SIGTERM 停止では削除されず残置する**。したがって state-dir に残りうるのは `queue.jsonl`（journal + 未 drain message）に加え、SIGTERM 残置時の `admin.token` / `daemon.json` の計 3 ファイル。(5) はこの未読突合と、これら 3 ファイルの破棄に閉じる。

---

## 6. 課金中立 attestation の取り方

broker が spawn する全エージェントが **対話 TUI（ヘッドレス不可）**であることを実 argv で確認する。これは課金中立（API 課金が走る `claude -p` / `codex exec` 等の非対話起動をしていない）の証跡になる。

### 6.1 多層防御の構造（spawn 時の guard）

broker の課金中立は **spawn 時の default-deny allowlist** で構造的に保証されている（`surface.py`）:

- `build_claude_argv` / `build_codex_argv` が対話 TUI 用 flag のみ許可し、`_guard_interactive_claude_argv` / `_guard_interactive_codex_argv` で **allowlist 外 token（flag 後サブコマンド / bare positional / `--` / 未知 flag / headless flag）を一律拒否**する。
- claude 側 headless blacklist: `-p` / `--print` / `--headless` / `--output-format` / `--input-format` 等。codex 側はサブコマンド（`exec` / `review` / `*-server` / `apply` / `sandbox` 等）が bare positional として落ちる。
- 値を取る flag は arity を持たせ（値位置の headless flag も二段で弾く）、`argv[0]` は basename 判定（絶対パス起動を false-reject しない）。

### 6.2 実 argv 検査（runtime attestation）

本番ホスト（broker ペインが live なセッション）で、実際に走っている argv を ps で検査する。**ヘッドレス flag / サブコマンドが 1 つも無いこと**を確認する。

**broker-spawned ペインに対象を絞る**のが要点。ホスト上には本 attestation と無関係な headless 実行（CI / 手動 `claude -p` 等）が並走しうるので、全 claude/codex を無差別に grep すると false positive を拾い、逆に対象識別漏れも起きる。broker が spawn したプロセスは argv に **`--mcp-config` で broker の MCP config（`org-broker` を含む）を抱える**ので、これで母集合を絞る。

```bash
# 1) broker-spawned に限定して argv を列挙（--mcp-config に org-broker を含むものだけ）
ps -eo pid,args | grep -iE "(^| )(claude|codex)( |$)" | grep -v grep \
  | grep -- "--mcp-config" | grep -i "org-broker"

# 2) 課金中立の negative check: 上で絞った broker ペインの argv に headless / exec 系が無いこと
ps -eo args | grep -iE "(^| )(claude|codex)( |$)" | grep -v grep \
  | grep -- "--mcp-config" | grep -i "org-broker" \
  | grep -nE -- "-p( |$)|--print|--headless|--output-format|--input-format| exec | review |--mcp-server" \
  && echo "NG: broker ペインに headless/exec flag を検出（課金が走る起動）" \
  || echo "OK: broker ペインに headless/exec flag なし（対話 TUI = 課金中立）"

# 3) 母集合の突合（任意・推奨）: list_panes の broker bind ペイン数と (1) の件数が一致することを確認
#    （ディスパッチャー/窓口の list_panes と pid を突合し、識別漏れ・余剰を検出する）
```

期待: 各 broker ペインの argv が `--mcp-config <broker>` / `--model` / `--permission-mode` 等の **対話 flag のみ**で構成され、negative check が `OK` を返す。

> **注意**: ps の検査は **broker ペインが live なホストセッション**で行う（PID namespace を分離した sandbox 内からは実ペインが見えない）。spawn 時の guard（§6.1）が一次防御、ps による runtime attestation が二次確認という二段で課金中立を担保する。`--mcp-config` でのフィルタは broker ペインの構造的特徴に基づく一次絞り込みであり、厳密性が要るときは (3) の `list_panes` 突合で母集合の過不足を閉じる。

---

## 7. 検証ゴミの cleanup（条件 (5) の dogfooding）

本 runbook の検証で作ったテスト state は **repo 外のテスト用ディレクトリ**に閉じており、本番 `.state/broker/` は生成しない。検証後は §5(5) の手順を**テストパスに対して**実行し、跡を残さない。

```bash
CANON_ROOT=/home/happy_ryo/work/org/claude-org-ja   # canonical root（環境に合わせる）

# 検証で使ったテスト state-dir を確認（repo 外であること）
ls -d /tmp/claude/broker-smoke-* /tmp/claude/usercommon-settings.json 2>/dev/null

# journal の未読突合（§5(5) のスクリプトを BROKER_STATE=テストパスに向けて実行）→ 問題なければ破棄
# （/tmp 配下は ephemeral。運用ルールに従いアーカイブまたは削除）

# 本番 .state/broker が未生成であることの最終確認（canonical root の絶対パス + 現 worktree 直下の両方）
test -e "$CANON_ROOT/.state/broker" && echo "NG: 本番 .state/broker が存在" || echo "OK: 本番 .state は不変"
test -e "$PWD/.state/broker" && echo "NG: worktree 直下に .state/broker が存在" || echo "OK: worktree 直下も不変"
```

---

## 8. 観察性 — 走行中の org を覗く（attach 導線）

broker（tmux backend）は **spawn する子ペイン（ディスパッチャー・ワーカー）** を専用 socket 上の **detached な独立 tmux セッション**として起動する。renga の「同一タブ内の可視 split ペイン」と違い、これら子ペインはデフォルトでは人間の画面に出ないため、「ワーカーが何人動いていて、どれが止まっているか」が視界に入る *ambient awareness*（何もしなくても全体がなんとなく見える状態）が静かに失われる。既存の俯瞰手段だけではこの体験は埋まらない:

| 手段 | 提供するもの | 足りないもの |
|---|---|---|
| ダッシュボード（`localhost` の状態 UI） | `state.db` ベースの状態俯瞰（worker 一覧・遷移・activity） | 各ペインの**生画面**ではない |
| attention watcher（[`attention-watch.md`](attention-watch.md)） | 異常・gate 時の push 通知 | 「健全時に眺めて安心する」常時観察ではない |
| **tmux attach（本節）** | **broker-spawned 子ペイン（ディスパッチャー・ワーカー）の生画面** | 下記のとおり現状は per-session attach（単一セッション化は §8.2 の将来形） |

本節は走行中の broker org を **read-only で覗く attach 導線**を示す。**この導線は tmux backend（POSIX / WSL2）固有**である。WezTerm backend（Windows、`isolated_session=False`）は各ペインを GUI ウィンドウとして spawn するため画面は元から可視で、attach は不要。

> **対象範囲（重要）**: attach で見えるのは **broker が `adapter.spawn` した子ペイン（ディスパッチャー・ワーカー）** のみ。**窓口（root secretary）は adapter 実ペインを持たない logical pane**（bookkeeping entry。`register_logical_pane`、`claude_org_runtime/broker/server.py`）であり、org を起動した人間の手元 terminal でそのまま動く（spike socket には現れない）。したがって本導線が埋めるのは「ワーカー群 / ディスパッチャーの生画面が見えない」ギャップであって、窓口は元々人間の眼前にある。

### 8.1 現状 — 独立セッションへの attach（runtime terminal adapter）

現行 runtime の terminal adapter（tmux、`claude_org_runtime.terminal.tmux`）は、broker が spawn する子ペイン（ディスパッチャー・ワーカー）を**専用 socket `claude-org-spike` 上の独立 detached セッション**として作る（セッション名 `spike-<pid>-<連番>`、`isolated_session = True`）。既存 tmux サーバー（renga 等）とは socket 分離されているため、観察には socket 名 `-L claude-org-spike` の明示が要る。

```bash
# 1) 現存する broker セッションを一覧（読み取りのみ。socket 明示が必須）
tmux -L claude-org-spike list-sessions
#   例:  spike-12345-1: 1 windows (created ...)   ← 各行が 1 子ペイン（連番は 1 始まり）

# 2) 覗きたいセッションへ read-only で attach（-r が read-only。誤打鍵で worker を壊さない）
tmux -L claude-org-spike attach -r -t spike-12345-1
```

attach 後の操作（prefix は既定 `Ctrl-b`）:

| 操作 | キー | 用途 |
|---|---|---|
| detach（観察をやめて抜ける） | `Ctrl-b` → `d` | セッションは生かしたまま離脱（プロセスに影響しない） |
| 別セッションへ切替 | `Ctrl-b` → `s` | セッション一覧から選択。**現状は per-session なので全体を見るには切替が要る** |

> **read-only `-r` を既定にする理由**: 独立セッションへの attach は worker の生 TUI に直接つながる。`-r` なしで attach すると観察中の打鍵が worker セッションに入りうる（介入は窓口/ディスパッチャーの `send_keys` 経路に閉じる設計のため、人間の手 attach は観察に限定する）。

> **検証ログ（2026-06-13、runtime 0.1.22）**: socket 名 `claude-org-spike` / セッション名 `spike-<pid>-<連番>` / `isolated_session = True` は `claude_org_runtime/terminal/tmux.py`（`SPIKE_SOCKET` 定数・`_new_session_name`）を実機で確認。`list-sessions`（複数セッション列挙）/ `attach -r`（read-only flag の受理）/ `kill-server`（後始末）の各コマンド形は scratch socket で疎通確認済み（実 broker org への attach は対話ブロックのため本検証では未実施）。

### 8.2 将来 — 単一セッション化で `attach` 一発（transport-lab 設計、未 land）

transport-lab `docs/design/broker-native-roles.md` §3.4（defect 4 対処）が、tmux adapter を **単一 `claude-org` セッション内の複数ペイン/ウィンドウ**構成へ再構成する設計を確定済み。land 後は次の 1 コマンドで broker-managed なペイン群（ディスパッチャー・ワーカー）が一望でき、標準ペイン nav（`Ctrl-b` 矢印）が効くため §8.1 の per-session 切替が不要になる:

```bash
tmux attach -r -t claude-org   # 単一セッション化（§3.4 / R1）後の導線（-r=read-only）。socket -L の指定も不要になる
```

- これは **runtime の terminal adapter（`claude_org_runtime/terminal/`）の変更**であり、ja は runtime の pin bump で consume する（ja 側の本 runbook 手順ではない）。**現行 runtime（独立セッション）では §8.1 が唯一の attach 導線**。
- ペイン死は差分 reconcile が処理する設計のため、単一セッション化のトレードオフ（session 級障害が全ペインに波及）は観察性の常時便益が上回る、と §3.4 が結論している。
- 観察性ギャップ対応として検討された observer 専用コマンド（broker-managed ペインの read-only タイル表示）/ ダッシュボードへのペイン生画面タイル表示は、単一セッション化後の `attach -r -t claude-org`（read-only）が同等の俯瞰を与えるため重複となり、本 runbook では採らない（要否は単一セッション化後の実運用で再判断）。

---

## 9. 関連

- 設計 SoT: transport-lab `docs/design/ja-migration-plan.md` §5（統合シーム）/ §5.5（併存・切戻し）/ §8 Issue G（dogfood ゲート）
- 契約: [`docs/contracts/backend-interface-contract.md`](../contracts/backend-interface-contract.md) Surface 8（broker auth & delivery、ratified 2026-06-14）
- 輸送両系の窓口運用差: [`CLAUDE.md`](../../CLAUDE.md)「輸送層（transport）両系」
- spawn 儀式（dev-channel 承認 → folder-trust 承認）: [`.dispatcher/references/spawn-flow.md`](../../.dispatcher/references/spawn-flow.md) 3-3b
- transport アクセサ（ja 側単一シーム）: [`tools/transport.py`](../../tools/transport.py)
- user_common allowlist 射影: [`tools/org_setup_prune.py`](../../tools/org_setup_prune.py) `--user-common-allowlist`
- attention watcher の運用文体: [`attention-watch.md`](attention-watch.md)
- 観察性の単一セッション化設計（§8.2 の将来形）: transport-lab `docs/design/broker-native-roles.md` §3.4（defect 4 — 独立 tmux セッション問題）
