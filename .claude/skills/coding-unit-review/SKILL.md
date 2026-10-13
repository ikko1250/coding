---
name: coding-unit-review
description: インタビュー書き起こし（input/*.md）から、Haiku サブエージェントでコーディング単位に分割し、人が印を付けて確定させる作業用 CSV（output/<name>_review.csv）を作る。「review を作って」「コーディング単位に分けて」「同じ手順で新しいトランスクリプトを処理して」と頼まれたとき、または /coding-unit-review <name> で使う。
---

# コーディング単位の作業用 CSV（review）を作る

`input/<name>.md` の書き起こしを、1文1行 → Haiku による単位分割（長いターンは3回多数決）→ 作業用 CSV の順に処理する。
作業ディレクトリはこのプロジェクトのルート（`md_to_csv.py` などがある場所）。Python は `.venv/bin/python` を使う。

`<name>` は `input/<name>.md` の拡張子なしのファイル名。指定がなければ `input/` を見て、まだ `output/<name>_review.csv` がないものを対象にする（複数あれば確認する）。

## 前提（変えないこと）

- 単位はターンをまたがない。ターン = 相づちを飛ばしたうえで同じ話者が続けて話した文のまとまり（`transcript.py`）。
- 相づち = 同じ話者に挟まれた他の話者の短い発言（`BACKCHANNEL_MAX` 字以下）。単位に含めない。
- 長いターン（`LONG_TURN_MIN` 文以上、既定 6）は 1ターン1エージェントで3回分割し、境目ごとに過半数で決める。
- Haiku の出力は必ず `merge` の形式チェックを通す。不合格のものは手で直さず、`rejected/` に移してやり直させる。

## 手順

### 1. 1文1行の CSV にする

```bash
.venv/bin/python md_to_csv.py
```

- `input/` の全 `.md` を処理する（既存ファイルの出力は変わらない）。`output/<name>.csv` ができたことと文数を確認する。
- 「解析できませんでした」の警告が出たら、書き起こしの形式を確認してユーザーに報告する。
- 新しい形式で読めない場合は `md_to_csv.py` の正規表現を拡張する（既存ファイルの出力が変わらないことを `cmp` で確認）。

### 2. 相づちとターンを確認する

```bash
.venv/bin/python - <<'EOF'
import csv, collections
from transcript import mark_turns
S = list(csv.DictReader(open("output/<name>.csv", encoding="utf-8-sig")))
mark_turns(S)
bc = [r for r in S if r["相づち"]]
print("文", len(S), "相づち", len(bc), collections.Counter(r["発話者"] for r in bc))
print("話し手の相づち例:", [r["文"] for r in bc][:20])
EOF
```

相づちの数や中身が不自然（内容のある質問が大量に相づち扱い、など）なら、進める前にユーザーに確認する。

### 3. Haiku に渡すファイルを作る

```bash
.venv/bin/python haiku_turns.py prepare <name>        # work/haiku/<name>/batch_N.md
.venv/bin/python haiku_turns.py prepare-long <name>   # work/haiku/<name>/long/turn_T.md
mkdir -p work/haiku/<name>/long/run1 work/haiku/<name>/long/run2 work/haiku/<name>/long/run3
```

バッチ数と長いターンの数を数え、必要なエージェント数（バッチ数 + 長いターン数 × 3）をユーザーに伝える。

### 4. Haiku サブエージェントで分割する

- Agent ツールで `model: "haiku"`, `subagent_type: "general-purpose"`, `run_in_background: true` で起動する。
- 指示文はテンプレートの `{NAME}` `{N}` `{T}` `{RUN}` を置き換えて使う（文言は変えない）。
  - バッチ: [prompts/batch.md](prompts/batch.md)（`batch_{N}.md` → `result_{N}.csv`）
  - 長いターン: [prompts/long.md](prompts/long.md)（`long/turn_{T}.md` → `long/run{RUN}/result_{T}.csv`）
- 同時に動かせるサブエージェントは 20 まで。終了通知が来て空いた分だけ次を起動する（上限エラーが出ても同じ呼び出しを繰り返さない）。
- 全部そろうまで `merge` は待つ。

### 5. 検証と多数決

```bash
.venv/bin/python haiku_turns.py merge <name>
```

- `NG` が出たファイルは `work/haiku/<name>/rejected/` に移し（例: `run1_140.csv`）、同じテンプレートで同じファイルをもう一度作らせる。
  2回目も同じ理由で落ちる場合は、テンプレートの出力条件に「最初の開始文ID・最後の終了文ID」や「文を飛ばさない」を具体的な文IDで書き足して再実行してよい。
- 全部 OK になると `output/<name>_units_haiku.csv` ができる。

### 6. 作業用 CSV を作る

```bash
.venv/bin/python review.py prepare <name>
.venv/bin/python review.py apply <name>    # 印なしで units_haiku と同じ結果になることの確認
```

印なしの `apply` 結果（`output/<name>_units_final.csv`）の開始・終了文IDが `_units_haiku.csv` と一致することを確認する。

### 7. 報告

次をユーザーに伝える。

- 文数、相づち扱いの文数、ターン数、単位数（話者別）、単位あたり文数の分布と最大
- Haiku の実行回数と、やり直した件数・理由
- 要確認の注記の件数（多数決で票が割れた境目、長い単位、単位末尾の短い文）
- 作業用 CSV の使い方（下記）

## 作業用 CSV の使い方（ユーザー向けの説明）

`output/<name>_review.csv`（1文1行）の列: `文ID, 単位ID, 先頭, 相づち, 区切る, つなぐ, 発話者, コード案, 文, 要確認`

- 区切る: 新しい単位の1文目になる行に `1`
- つなぐ: 前の単位とまとめたい単位の先頭（`●`）の行に `1`
- 相づち: `1` を消すと通常の発言、`1` を付けると相づち（単位に入らない）
- 文・発話者: 直接書き換えれば修正になる。`文` に `/` を書くとそこで文を分ける（`S0123-1`, `S0123-2`）。分けた位置で区切るには `prepare` を再実行して、分けた行に 区切る を付ける
- 行の追加・削除・並べ替えはしない
- 反映: `.venv/bin/python review.py apply <name>` → `output/<name>_units_final.csv`（単位）と `output/<name>_sentences_final.csv`（修正履歴付きの全文）
- `prepare` を再実行しても、付けた印と修正は引き継がれる
