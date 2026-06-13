<!-- Issue #22 への投稿用ドラフト。worker は GitHub 書込不可のため窓口が投稿する。 -->

# K1 spike 結果: push 一次配送の批准前 HARD ゲート — **総合 PASS（全 4 AC GO）**

設計 SoT: `broker-native-roles.md` §9.5 / `ja-migration-plan.md` §8 K1。
検証した核心仮定: **tool-less**（ツール宣言ゼロ・`experimental{claude/channel}` のみ）な
channel sidecar を spawn 経路で load し、idle セッションを**能動 poll なしに** push で起こせるか。
prior art（claude-peers-mcp）は tools+channel 同梱で wake 実証済だが、tool-less 単独 channel の先例が無い（これが核心）。

## 環境
WSL2（Linux 6.18）/ tmux 3.4（専用 socket）/ **Claude Code 2.1.177** / Sonnet 4.6（対話 TUI・最小トークン）。
隔離 state-dir（`/tmp/claude/broker-k1-spike/*`）。本番 ja `.state/`・本番 `~/.claude-peers.db` 不可触（AC-3 で mtime 不変を実証）。

## 判定（全 PASS 必須 → 全 PASS）

| AC | 内容 | 判定 | 実機根拠（要点） |
|---|---|---|---|
| 1 | tool-less channel を load + dev-channel 機械承認 | **PASS** | tool-less stdio サーバーを `--dangerously-load-development-channels server:org-broker-channel` で load、idle 到達 5.4s。folder trust + dev-channel 警告の 2 プロンプトを `send-keys` で機械承認 |
| 2 | idle→daemon queue→sidecar claim→push で能動 poll なしに起きる | **PASS** | idle 後 pane へ**一切入力せず** nonce 出力要求を enqueue → 4.0s 後にセッションが自発ターンで `● WOKE-K1-78038792` 出力。**tool-less ゆえ poll 手段が構造的に存在せず**、push 以外ありえない（反証可能に実証） |
| 3 | renga と coexist | **PASS** | 1 セッションに org-broker-channel + **隔離した実 claude-peers**（別 db/port/token）を同居 load → 2 系へ push し両方 wake（source で区別・互いを block せず）。本番 `~/.claude-peers.db` mtime 不変 |
| 4 | 課金中立（対話 TUI・実 argv attestation） | **PASS** | ps 実 argv に `-p`/`--print`/`--headless`/`--output-format` を一切含まず、idle `❯` 対話プロンプト描画を観測 |

## 結論（設計への影響）
- **§9.5 の design fallback（tools+channel 同梱形）は不要**。tool-less channel-only での idle wake が実機で成立したため、delivery-scoped credential / droppable sidecar（§9.4 least-privilege）をそのまま採れる。
- **Issue E（S3 契約批准・P8/P9 prose land）/ Issue G（dogfood + 既定反転）の前提条件を充足**。
- 課金中立 allowlist（`is_interactive_claude_argv`）に `--dangerously-load-development-channels` を追加（§9.5 ceremony が false-reject されないため。headless 系でないので安全）。本体取り込み時に継承。

## 観測した cosmetic（PASS を覆さない）
tool-less サーバーは TUI の MCP ツールサーバー一覧に出ず `server:org-broker-channel · no MCP server configured with that name` という banner が併記されるが、channel capability は登録され push 注入は正常。tools を持つ claude-peers では当該 banner は出ない（registry view の表示差で channel 機能とは独立）。運用ドキュメントに既知 cosmetic として記載推奨。

## 証跡・再現
- 判定スクリプト（実機・課金）: `spike/run_k1.py`（AC-1/2/4）/ `spike/run_k1_coexist.py`（AC-3）
- 決定的 CI（無課金・10 ケース）: `tests/test_k1_channel.py` + `spike/k1_smoke.py`
- 詳細ログ・画面ダンプ・argv attestation: `spike/RESULTS.md` Phase K1 節 / `/tmp/claude/broker-k1-spike/{isolation,coexist}/evidence/`（実機でのみ再生成）
