# 手動 AC テスト — IME × スピナー backend parity（tmux 素 vs WezTerm 素）

> ステータス: **propose-only / 人間実走テンプレ**。ワーカーは日本語 IME をタイプできない
> （変換窓は OS 側オーバーレイで grid scrape から観測不能）ため、本書は**人間が実走して
> GO/NO-GO を埋めるための手順書 + 結果記録テンプレ**である。ワーカーは結論を断定せず、
> ここで停止する。
>
> 機構の根拠: [`mechanism.md`](./mechanism.md)。再描画刺激の生成: [`spinner_harness.py`](./spinner_harness.py)。

タスク: ime-backend-parity-spike（Refs #6 #9）。
設計 SoT: [`docs/design/renga-decoupling.md`](../../docs/design/renga-decoupling.md) §1.2 / §7.1 AC-1。

---

## 0. この実走が答える問い

renga-decoupling §1.2 確定制約 #2「IME 制約により人間入力端末は renga 継続」を、
**新方針（ユーザーが IME overlay の快適さを放棄してでも tmux/WezTerm へ完全移行して組織起動できる）**
の下で再検証する。具体的に埋めるべき問い:

1. **WezTerm 素**で、スピナー再描画中に日本語変換窓は壊れるか（ユーザー実測の再現）。
2. **tmux + ホスト端末（Windows Terminal）**で、同じ刺激で変換窓は壊れるか。
   - 壊れない → **IME 問題は WezTerm 固有**。tmux を唯一の人間入力 backend にできる可能性。
   - 壊れる → **tmux-in-ホスト端末でも起きる**。renga 継続 or 別解が必要。
3. 「変換窓は多少揺れても**確定文字列が正しく入る／未送信テキストが壊れない**なら、
   overlay の見た目を諦めれば移行可」という線引きは引けるか。

> **重要**: これは旧 AC-1（[`../manual-ime-test.md`](../manual-ime-test.md)、broker ナッジ注入の検証）とは
> **別の検証**である。本書の刺激は **Claude 自身のスピナー自己再描画**（外部注入なし）。混同しないこと。

---

## 1. 事前準備（実施者: 日本語 IME を使う人間。所要 15〜20 分）

- 課金なし: 本ハーネスは**実 Claude を起動しない**（純 ANSI 描画のみ）。
- 必要物: Python 3.x、tmux 3.x（POSIX レッグ）、WezTerm（WezTerm レッグ）、日本語 IME。
- ハーネスの自己診断（任意・TTY 不要）で壊れていないか先に確認:

  ```bash
  cd <repo>/spike/ime-parity
  python3 spinner_harness.py --selftest      # 全 PASS を確認
  ```

### ハーネスの操作（共通）

```bash
python3 spinner_harness.py --state <idle|long-input|streaming|ime> [--cursor-mode <save|cup>] [--hz 10]
```

- 下部の `❯ ` 入力欄に日本語を IME 変換しながらタイプする。
- `Ctrl+U` で入力欄クリア、`Ctrl+C` で終了。
- `--cursor-mode save`（既定）= DECSC/DECRC 往復、`--cursor-mode cup` = 絶対 CUP 往復。
  **両方を必ず試す**（mechanism.md §4 留保: どちらの様式が IME を崩すか切り分けるため）。

---

## 2. backend 別の起動手順

### レッグ A: tmux（ホスト端末 = Windows Terminal）

「tmux を唯一の人間入力端末にする」想定。**renga も WezTerm も使わない**。

1. **Windows Terminal** を開き、WSL2（Ubuntu-24.04）を起動する。
2. ```bash
   cd <repo>/spike/ime-parity
   tmux new -s ime-parity
   ```
   （既存 tmux サーバーと混ざらないよう新規セッション推奨。`tmux kill-session -t ime-parity` で後片付け）
3. tmux の中で:
   ```bash
   python3 spinner_harness.py --state ime
   ```
4. Windows Terminal をフォーカスし、日本語 IME（Microsoft IME 等）を有効化して §3 の各ケースを実走。
5. `--cursor-mode cup` でも繰り返す。

> 記録すべき前提: ホスト端末が **Windows Terminal** であること（`echo $WT_SESSION` が非空）。
> もし VSCode 統合端末や別ホスト端末で実走する場合はその旨を §5 環境記録に明記する
> （IME 実装が変わるため結果の一般化が変わる — mechanism.md §3 補足の VSCode #282621 参照）。

### レッグ B: WezTerm 素（WezTerm 自身がホスト端末）

「WezTerm を唯一の人間入力端末にする」想定。**tmux で包まない**（WezTerm 素の native IME を見るため）。

1. **IME を確認**（`use_ime` の扱いは構成で異なる — WezTerm 公式 doc）:
   - **Windows 側 `wezterm.exe`（第一想定）**: Windows では IME は**常時有効**で、`use_ime` 設定は
     **効果がない**（無効化もできない）。追加設定は不要。そのまま日本語入力できる。
   - **Linux ビルド（X11/Wayland）**: `use_ime` は現行**全プラットフォーム既定 `true`**（20220319 以降）。
     日本語入力には fcitx5/ibus 等の IME 環境設定（`GTK_IM_MODULE` / `XMODIFIERS` 等）が前提。
     `~/.wezterm.lua` で明示するなら `return { use_ime = true }`。変更後は WezTerm を**再起動**。
   - 任意の記録対象: `ime_preedit_rendering = 'builtin' | 'system'`（preedit 描画様式。save/cup や
     backend で差が出たら §5 に記録）。
2. WezTerm を**ホスト端末として**起動し、WSL シェルを開く:
   - Windows 側 `wezterm.exe`（GUI）で WSL を起動する構成を第一に想定。例:
     `wezterm.exe start -- wsl.exe ~` あるいは WezTerm の起動ドメインを WSL に設定。
   - （Linux ビルドの wezterm を X/Wayland で動かす構成なら IME は fcitx5/ibus。
     その旨を §5 に明記し、別スタックの結果として扱う。）
3. ```bash
   cd <repo>/spike/ime-parity
   python3 spinner_harness.py --state ime
   ```
4. WezTerm をフォーカスし、日本語 IME を有効化して §3 の各ケースを実走。
5. `--cursor-mode cup` でも繰り返す。

> 記録すべき前提: WezTerm のバージョン（`wezterm --version`）、`use_ime` 値、
> Windows 側 wezterm.exe か Linux ビルドか、IME の種類。

---

## 3. テストする 4 状態（renga-decoupling §7.1 AC-1）

各状態を **両 backend × 両 cursor-mode** で実走する。状態と `--state` の対応:

| # | 状態 | `--state` | スピナー | 人間の操作 | 観察ポイント |
|---|---|---|---|---|---|
| 1 | **idle**（対照群） | `idle` | 停止 | 入力欄に日本語を 1 文変換・確定 | スピナー無しで変換窓が正常に入力欄カーソルに錨を打つか（ベースライン） |
| 2 | **長文入力中**（対照群） | `long-input` | 停止 | 複数行の長い日本語を変換しながら入力（送信しない） | スピナー無しで長文 IME 入力が壊れないか。確定文字列の欠落・重複が無いか |
| 3 | **streaming** | `streaming` | **稼働** | タイプせずスピナー描画と擬似ストリーミングを観察 | スピナーが同位置再描画され、上部に出力が流れる。描画自体の乱れが無いか |
| 4 | **IME 変換中**（中心セル） | `ime` | **稼働** | **スピナー稼働中に**日本語を変換しながら入力 | ★本問題の核心。下記を必ず記録 |

### 状態 4（IME 変換中 × スピナー稼働）の観察ポイント（最重要）

スピナーが ~10Hz で再描画し続ける中で日本語を変換しながら入力し、以下を 1 つずつ確認:

- **(a) 変換窓・候補リストの位置**: スピナー再描画のたびに変換窓が**揺れる/飛ぶ/消える**か。
  入力欄カーソルに座り続けるか。
- **(b) 未確定文字列（下線付き）**: スピナー上書きで**破壊・消失・確定**されないか。
- **(c) 確定後の文字列**: 変換確定（Enter）した日本語が入力欄に**正しく・欠落/重複なく**入るか。
- **(d) スピナー文字列の混入**: スピナーの「✻ Cogitating…」が入力欄や未確定文字列に**混入**しないか。
- **(e) cursor-mode 差**: `save`（DECSC/DECRC）と `cup`（絶対 CUP）で (a)〜(d) に差が出るか。

> 判定の主眼（新方針）: 「**変換窓が見た目揺れても、確定文字列が正しく入り、未送信テキストが
> 壊れない**」なら overlay を諦めれば移行可。「**確定文字列が壊れる/勝手に送信される/入力不能**」なら移行不可。
> この線引きで GO/NO-GO を付ける（§4）。

---

## 4. 結果記録テンプレ（人間が実走後に埋める）

> 各セルに **GO / NO-GO / N/A** と 1 行根拠。GO = その backend を人間入力端末として使える
> （overlay 揺れは許容、確定文字列が壊れない）。NO-GO = 確定文字列破壊・勝手送信・入力不能。

### 実走サマリ（最終判定）

| 状態 \ backend×mode | tmux+WT save | tmux+WT cup | WezTerm save | WezTerm cup |
|---|---|---|---|---|
| 1 idle（対照） |  |  |  |  |
| 2 長文入力（対照） |  |  |  |  |
| 3 streaming |  |  |  |  |
| 4 **IME 変換中** ★ |  |  |  |  |

### 状態 4 の詳細記録（backend×mode ごとに記述）

各 backend×mode について (a)〜(d) を記録（壊れた場合はスクリーンショット推奨）:

```
[tmux + Windows Terminal / cursor-mode=save]
 (a) 変換窓の位置:
 (b) 未確定文字列:
 (c) 確定後文字列:
 (d) スピナー混入:
 判定: GO / NO-GO  根拠:

[tmux + Windows Terminal / cursor-mode=cup]
 (a) ... (b) ... (c) ... (d) ...  判定:

[WezTerm 素 / cursor-mode=save]
 (a) ... (b) ... (c) ... (d) ...  判定:

[WezTerm 素 / cursor-mode=cup]
 (a) ... (b) ... (c) ... (d) ...  判定:
```

### parity 結論（問い 1・2 への回答）

```
- WezTerm 素は壊れたか:           はい / いいえ  （根拠:                       ）
- tmux+WT は壊れたか:             はい / いいえ  （根拠:                       ）
- ⇒ IME 問題は「WezTerm 固有」か「tmux でも起きる」か:
      [ ] WezTerm 固有（tmux は GO） → tmux を唯一の人間入力 backend にできる
      [ ] tmux でも起きる            → renga 継続 or 別解が必要（制約 #2 維持）
      [ ] cursor-mode 依存（save と cup で割れる） → 詳細:
- overlay を諦めれば移行可、の線引きは引けたか:  はい / いいえ  （内容:        ）
```

---

## 5. 環境記録（実走時に必ず埋める）

```
- 実施日時 / 実施者:
- OS / WSL ディストロ:                       （例: Windows 11 / WSL2 Ubuntu-24.04）
- IME の種類とバージョン:                     （例: Microsoft IME / Google 日本語入力）
- [tmux レッグ] ホスト端末と版:               （例: Windows Terminal 1.x、$WT_SESSION=...）
                tmux バージョン:
- [WezTerm レッグ] wezterm --version:
                  Windows wezterm.exe か Linux ビルドか:
                  use_ime 値:                 （Windows は常時有効・設定無効。Linux は既定 true）
                  ime_preedit_rendering（設定していれば）:
- spinner_harness.py の git revision:
- 備考（既知の癖・スクリーンショットの所在 等）:
```

---

## 6. 判定後の扱い（ワーカーはここまで用意して停止）

- 本テンプレを埋めた結果は **設計再導出（Epic #6 ja 完全移行）の判断材料**となる。
  断定はこの実走結果が出てから窓口・ユーザーが行う。ワーカーは結論を書かない。
- NO-GO が出ても**勝手に AC を緩めない**。§3 の判定主眼（確定文字列が壊れるか）に従い、
  揺れの許容範囲を独断で広げない。緩和策の検討が要る場合は窓口にエスカレーションする。
- 結果は本ファイルの §4/§5 に追記するか、`spike/RESULTS.md` に「IME backend parity（手動）」節を
  設けて転記する（GO/NO-GO 列を保ったまま）。
