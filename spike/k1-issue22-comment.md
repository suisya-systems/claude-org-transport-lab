<!-- Issue #22 への投稿用ドラフト。worker は GitHub 書込不可のため窓口が投稿する。 -->

# K1 spike 結果: push 一次配送の批准前 HARD ゲート — **総合 PASS（全 4 AC GO）**

設計 SoT: `broker-native-roles.md` §9.5 / `ja-migration-plan.md` §8 K1。
検証した核心仮定: **tool-less**（ツール宣言ゼロ・`experimental{claude/channel}` のみ）な channel sidecar を spawn 経路で load し、idle セッションを**能動 poll なしに** push で起こせるか。
prior art（claude-peers-mcp）は tools+channel 同梱で wake 実証済だが、tool-less 単独 channel の先例が無い（これが核心）。

## 環境
WSL2（Linux 6.18）/ tmux 3.4（専用 socket）/ **Claude Code 2.1.177** / Sonnet 4.6（対話 TUI・最小トークン）。
隔離 state-dir（`/tmp/claude/broker-k1-spike/*`）。本番 ja `.state/`・本番 `~/.claude-peers.db` 不可触（AC-3 で mtime 不変を実証）。

## 反証可能な wake 観測（重要）
push 本文に小文字 hex `base` を載せ「**大文字**にして 1 行で出力せよ」と指示し、`base.upper()` の出現を検出する。
変換後トークンは**本文に存在しない**ため、画面に出現すればモデルの実ターン以外ありえない（注入メッセージの echo では一致しない）。
これにより「echo を wake と誤判定する」confound を排除した（初版は verbatim nonce + substring 一致で coexist が false-positive になっていた指摘を、self-review で検出・修正済）。

## 判定（全 PASS 必須 → 全 PASS）

| AC | 内容 | 判定 | 実機根拠（要点） |
|---|---|---|---|
| 1 | tool-less channel を load + dev-channel 機械承認 | **PASS** | tool-less stdio サーバーを `--dangerously-load-development-channels server:org-broker-channel` で load。folder trust + dev-channel 警告の 2 プロンプトを `send-keys` で機械承認 |
| 2 | idle→daemon queue→sidecar claim→push で能動 poll なしに起きる | **PASS** | idle 後 pane へ**一切入力せず** transform プローブを enqueue → 3.0s 後にセッションが自発ターンで大文字 target（例 `● B32C12B7`、base=`b32c12b7`）を出力。target は本文に無く echo では一致しない。**加えて** tool-less ゆえ poll ツールが構造的に存在しない（二重の反証） |
| 3 | renga と coexist | **PASS** | 1 セッションに org-broker-channel + **隔離した実 claude-peers**（別 db/port/token）を同居 load → 2 系へ push し**両方ともモデルが実ターンで変換出力**（`● 592D9B05` / `● B31DB427`）、source で区別・互いを block せず。本番 `~/.claude-peers.db` mtime 不変 |
| 4 | 課金中立（対話 TUI・実 argv attestation） | **PASS** | 自分が spawn した claude プロセスの ps 実 argv に `-p`/`--print`/`--headless`/`--output-format` を一切含まず、idle `❯` 対話プロンプト描画を観測 |

## 結論（設計への影響）
- **§9.5 の design fallback（tools+channel 同梱形）は不要**。tool-less channel-only での idle wake が実機で成立したため、delivery-scoped credential / droppable sidecar（§9.4 least-privilege）をそのまま採れる。
- **Issue E（S3 契約批准・P8/P9 prose land）の前提条件（tool-less 仮定の成立）を充足**。
- 課金中立 allowlist（`is_interactive_claude_argv`）に `--dangerously-load-development-channels` を追加（§9.5 ceremony の false-reject 回避・headless 系でないので安全）+ 許可/拒否回帰テストを常設。本体取り込み時に継承。

## スコープ境界（honest scoping）
- 本ゲートが実証したのは **§9.5 (i)(ii) の tool-less channel-only の load+wake**（先例が無い核心）。本番 §9.5 spawn 構成（`--mcp-config <daemon=全ツール>` **と** dev-channel sidecar を同一セッションに同居）の整合は **Issue G dogfood のスコープ**であり本ゲートでは別途検証しない（tool-less 単独で wake する＝sidecar が full tool を要しないことは確定）。
- `DELIVERED` は sidecar の stdout flush 後の確定。flush→harness 受理→可視の残余 window は **closed ではなく narrowed**（§9.3 の at-least-once + 冪等表示が許容。emit に `msg_id` dedup key を付与）。
- 「能動 poll なし」は**セッション**の性質（sidecar の ~1s claim ループが配送を媒介）。K1 daemon は §9.3 のライフサイクル/reaping/fencing/delivery-scope に限り、flapping/heartbeat health と durability は本体取り込み Issue B スコープ。

## 観測した cosmetic（PASS を覆さない）
tool-less サーバーは TUI の MCP ツールサーバー一覧に出ず `server:org-broker-channel · no MCP server configured with that name` という banner が併記されるが、channel capability は登録され push 注入は正常（tools を持つ claude-peers では当該 banner は出ない）。運用ドキュメントに既知 cosmetic として記載推奨。

## 証跡・再現
- 判定スクリプト（実機・課金）: `spike/run_k1.py`（AC-1/2/4）/ `spike/run_k1_coexist.py`（AC-3）
- committed 証跡（PII 除去・durable）: `spike/k1-evidence/{isolation,coexist}/wake-excerpt.txt` + `result.json`
- 決定的 CI（無課金・15 ケース）: `tests/test_k1_channel.py` + `spike/k1_smoke.py`
- 詳細: `spike/RESULTS.md` Phase K1 節
