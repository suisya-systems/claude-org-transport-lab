# IME × スピナー backend parity スパイク（tmux 素 vs WezTerm 素）

タスク: ime-backend-parity-spike（Refs #6 #9）。**propose-only**。
renga-decoupling §1.2 確定制約 #2「IME 制約により人間入力端末は renga 継続」を、
**新方針（IME overlay を放棄してでも tmux/WezTerm へ完全移行して組織起動できる）**の下で再検証する。

最後の IME 目視判定は人間が実施するため、ワーカーは手順とテンプレまでを用意して停止する。
結論は断定しない（人間実走で埋める）。

## 成果物

| ファイル | 役割 |
|---|---|
| [`mechanism.md`](./mechanism.md) | **機構解明**。なぜ WezTerm 素は IME 変換窓のアンカーを奪い、tmux はホスト端末（Windows Terminal）に IME 描画を委譲するので奪わない可能性があるかを、WezTerm/tmux/IME(TSF/IMM) の一次情報で根拠付け。renga の hardware-cursor caret 解との統一視点。断定せず仮説として提示 |
| [`spinner_harness.py`](./spinner_harness.py) | **スピナー再現ハーネス**。実 Claude を待たず、同位置連続再描画（DECSC/DECRC・絶対 CUP・EL）を生成。入力欄に日本語を変換しながらタイプして IME 共存を目視判定する。stdlib のみ・無課金。`--selftest` で TTY 不要の自己診断 |
| [`manual-ac-ime-parity.md`](./manual-ac-ime-parity.md) | **手動 AC テスト手順 + 結果記録テンプレ**。4 状態（idle / 長文入力中 / streaming / IME 変換中）を tmux 素 / WezTerm 素 × cursor-mode（save/cup）で実走する手順と GO/NO-GO テンプレ |

## クイックスタート（人間）

```bash
cd <repo>/spike/ime-parity
python3 spinner_harness.py --selftest          # 壊れていないか確認（全 PASS）

# tmux レッグ（ホスト端末 = Windows Terminal）
tmux new -s ime-parity
python3 spinner_harness.py --state ime         # 変換しながら ❯ にタイプ
python3 spinner_harness.py --state ime --cursor-mode cup

# WezTerm 素レッグ（use_ime=true 必須・再起動要）
python3 spinner_harness.py --state ime
python3 spinner_harness.py --state ime --cursor-mode cup
```

詳細は [`manual-ac-ime-parity.md`](./manual-ac-ime-parity.md) を参照。

## 旧 AC-1（broker ナッジ）との違い

[`../manual-ime-test.md`](../manual-ime-test.md) は **broker のナッジ打鍵注入**が IME 変換を壊すかの輸送層検証。
本スパイクは **Claude 自身のスピナー自己再描画**が backend を跨いで IME と共存するかの描画層検証で、別物。
</content>
