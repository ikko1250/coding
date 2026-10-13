"""話者ターン単位で Haiku にコーディング単位へ分割させるための、準備と結果の統合。

ターン = 同じ話者が連続して話した文のまとまり。単位はターンをまたがない。
同じ話者に挟まれた他の話者の短い相づちは飛ばしてターンをつなげ、単位には含めない（transcript.py）。

使い方（<name> は output/<name>.csv の名前。例: interview_taharasan_2）:
  .venv/bin/python haiku_turns.py prepare <name>       # work/haiku/<name>/batch_N.md を作る
  （Haiku サブエージェントが work/haiku/<name>/result_N.csv を書く）
  .venv/bin/python haiku_turns.py prepare-long <name>  # 長いターンを1ターン1ファイルにする（long/）
  .venv/bin/python haiku_turns.py merge <name>         # 検証して output/<name>_units_haiku.csv を作る
                                            # long/run*/result_T.csv があれば、区切り位置を多数決で決めて差し替える
"""

import csv
import re
import sys
from pathlib import Path

from transcript import mark_turns

BASE = Path(__file__).resolve().parent
LONG_TURN_MIN = 6  # この文数以上のターンは1ターンずつやり直す
SOURCE = WORK = LONG = OUTPUT = Path()  # configure() で設定する


def configure(name: str) -> None:
    global SOURCE, WORK, LONG, OUTPUT
    SOURCE = BASE / "output" / f"{name}.csv"
    if not SOURCE.exists():
        sys.exit(f"{SOURCE.relative_to(BASE)} がありません")
    WORK = BASE / "work" / "haiku" / name
    LONG = WORK / "long"
    OUTPUT = BASE / "output" / f"{name}_units_haiku.csv"


SENTENCES_PER_BATCH = 45
CONTEXT_BEFORE = 4
CONTEXT_AFTER = 2
RESULT_HEADER = ["ターン番号", "単位番号", "開始文ID", "終了文ID", "要約", "コード案"]
SUMMARY_MAX = 40
CODE_MAX = 15


def load() -> tuple[list[dict], list[list[int]]]:
    with SOURCE.open(encoding="utf-8-sig", newline="") as f:
        sentences = list(csv.DictReader(f))
    for i, row in enumerate(sentences, 1):
        row["sid"] = f"S{i:04d}"
    mark_turns(sentences)
    turns = {}
    for i, s in enumerate(sentences):
        if not s["相づち"]:
            turns.setdefault(s["ターン"], []).append(i)
    return sentences, [turns[t] for t in sorted(turns)]


def batch_turns(turns: list[list[int]]) -> list[list[int]]:
    """ターン番号（0 始まり）を、文数が SENTENCES_PER_BATCH 前後になるようにまとめる。"""
    batches, current, size = [], [], 0
    for t, turn in enumerate(turns):
        if current and size + len(turn) > SENTENCES_PER_BATCH:
            batches.append(current)
            current, size = [], 0
        current.append(t)
        size += len(turn)
    batches.append(current)
    return batches


def turn_lines(t: int, turns: list[list[int]], sentences: list[dict]) -> list[str]:
    turn = turns[t]
    a, b = turn[0], turn[-1]
    lines = [f"## ターン {t + 1}（話者: {sentences[a]['発話者']}、{len(turn)} 文）"]
    lines.append("### 直前の文脈（分割の対象外）")
    lines += [f"{s['sid']} {s['発話者']}：{s['文']}" for s in sentences[max(0, a - CONTEXT_BEFORE) : a]] or ["（なし）"]
    lines.append("### 分割の対象（文IDのない（相づち）の行は他の話者の相づちで、対象外）")
    for i in range(a, b + 1):
        s = sentences[i]
        lines.append(f"（相づち）{s['発話者']}：{s['文']}" if s["相づち"] else f"{s['sid']}：{s['文']}")
    lines.append("### 直後の文脈（分割の対象外）")
    lines += [f"{s['sid']} {s['発話者']}：{s['文']}" for s in sentences[b + 1 : b + 1 + CONTEXT_AFTER]] or ["（なし）"]
    lines.append("")
    return lines


def prepare() -> None:
    sentences, turns = load()
    WORK.mkdir(parents=True, exist_ok=True)
    for old in WORK.glob("*.*"):
        old.unlink()
    batches = batch_turns(turns)
    for n, batch in enumerate(batches, 1):
        lines = [f"# バッチ {n}（ターン {len(batch)} 個）", ""]
        for t in batch:
            lines += turn_lines(t, turns, sentences)
        (WORK / f"batch_{n}.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"batch_{n}.md: ターン {batch[0] + 1}〜{batch[-1] + 1}（{sum(len(turns[t]) for t in batch)} 文）")


def prepare_long() -> None:
    sentences, turns = load()
    LONG.mkdir(parents=True, exist_ok=True)
    for t, turn in enumerate(turns):
        if len(turn) >= LONG_TURN_MIN:
            (LONG / f"turn_{t + 1}.md").write_text("\n".join(turn_lines(t, turns, sentences)), encoding="utf-8")
            print(f"turn_{t + 1}.md（{len(turn)} 文）")


def validate(path: Path, batch: list[int], turns: list[list[int]], sentences: list[dict]) -> tuple[list[list[str]], list[str]]:
    """結果 CSV を検証し、(行, 問題点のリスト) を返す。batch は対象のターン番号（0 始まり）。"""
    if not path.exists():
        return [], [f"{path.name} がありません"]
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    problems = []
    if not rows or rows[0] != RESULT_HEADER:
        problems.append(f"ヘッダーが不正: {rows[0] if rows else '空'}")
        return [], problems
    rows = [r for r in rows[1:] if any(c.strip() for c in r)]
    for r in rows:
        if len(r) != len(RESULT_HEADER):
            problems.append(f"列数 {len(r)}: {r}")
    if problems:
        return [], problems

    for t in batch:
        ids = [sentences[i]["sid"] for i in turns[t]]
        units = [r for r in rows if r[0] == str(t + 1)]
        if not units:
            problems.append(f"ターン {t + 1}: 単位がありません")
            continue
        covered = []
        for k, r in enumerate(units, 1):
            if r[1] != str(k):
                problems.append(f"ターン {t + 1}: 単位番号が連番でない（{r[1]}）")
            if r[2] not in ids or r[3] not in ids:
                problems.append(f"ターン {t + 1}: ターン外の文ID {r[2]}〜{r[3]}")
                continue
            a, b = ids.index(r[2]), ids.index(r[3])
            if a > b:
                problems.append(f"ターン {t + 1}: 開始と終了が逆 {r[2]}〜{r[3]}")
            covered += ids[a : b + 1]
            if not r[4].strip() or len(r[4]) > SUMMARY_MAX:
                problems.append(f"ターン {t + 1} 単位 {r[1]}: 要約が空か {SUMMARY_MAX} 字超（{len(r[4])} 字）")
            if not r[5].strip() or len(r[5]) > CODE_MAX:
                problems.append(f"ターン {t + 1} 単位 {r[1]}: コード案が空か {CODE_MAX} 字超（{len(r[5])} 字）")
        if covered != ids:
            problems.append(f"ターン {t + 1}: 文の漏れ・重複・順序違い")
    extra = {r[0] for r in rows} - {str(t + 1) for t in batch}
    if extra:
        problems.append(f"バッチ外のターン番号: {sorted(extra)}")
    return rows, problems


def vote(t: int, runs: list[list[list[str]]], turns: list[list[int]], sentences: list[dict]) -> tuple[list[list[str]], dict]:
    """複数回の分割結果から、各境目を過半数で区切るかどうか決めて単位を作り直す。

    要約・コード案は、同じ範囲の単位を出した回があればそこから取り、なければ開始文を含む単位から流用する。
    """
    ids = [sentences[i]["sid"] for i in turns[t]]
    n = len(runs)
    starts = [{r[2] for r in rows} for rows in runs]
    votes = {sid: sum(sid in st for st in starts) for sid in ids[1:]}  # その文の前で区切った回数
    cut = [ids[0]] + [sid for sid in ids[1:] if votes[sid] * 2 > n]

    rows, notes = [], {}
    for k, start in enumerate(cut):
        end = ids[ids.index(cut[k + 1]) - 1] if k + 1 < len(cut) else ids[-1]
        inner = ids[ids.index(start) + 1 : ids.index(end) + 1]
        exact = [r for run in runs for r in run if r[2] == start and r[3] == end]
        source = exact[0] if exact else next(
            r for run in runs for r in run if ids.index(r[2]) <= ids.index(start) <= ids.index(r[3])
        )
        reasons = []
        if k > 0 and votes[start] < n:
            reasons.append(f"開始の区切りが {votes[start]}/{n} 票")
        minority = [sid for sid in inner if votes[sid] > 0]
        if minority:
            reasons.append("内部に少数意見の区切り: " + " ".join(f"{sid}前({votes[sid]}/{n})" for sid in minority))
        if not exact:
            reasons.append("要約・コード案は近い単位から流用")
        notes[(str(t + 1), start)] = (f"{votes[start]}/{n}" if k > 0 else "", "／".join(reasons))
        rows.append([str(t + 1), str(k + 1), start, end, source[4], source[5]])
    return rows, notes


def merge() -> None:
    sentences, turns = load()
    batches = batch_turns(turns)
    all_rows, failed = [], []

    def check(path: Path, batch: list[int]) -> list[list[str]] | None:
        rows, problems = validate(path, batch, turns, sentences)
        if problems:
            failed.append(path.name)
            print(f"{path.relative_to(WORK)}: NG")
            for p in problems[:10]:
                print(f"  - {p}")
            return None
        print(f"{path.relative_to(WORK)}: OK（{len(rows)} 単位）")
        return rows

    for n, batch in enumerate(batches, 1):
        all_rows += check(WORK / f"result_{n}.csv", batch) or []

    # 長いターンは、1ターンずつ複数回分割させた結果の多数決で差し替える
    notes = {}  # (ターン番号, 開始文ID) -> (区切り票, 要確認の理由)
    long_turns = sorted({int(p.stem.split("_")[1]) - 1 for p in LONG.glob("run*/result_*.csv")})
    for t in long_turns:
        runs = [rows for path in sorted(LONG.glob(f"run*/result_{t + 1}.csv")) if (rows := check(path, [t]))]
        if not runs:
            continue
        rows, turn_notes = vote(t, runs, turns, sentences)
        all_rows = [r for r in all_rows if r[0] != str(t + 1)] + rows
        notes.update(turn_notes)
    if failed:
        sys.exit(f"不合格: {failed}（やり直してから再実行してください）")
    all_rows.sort(key=lambda r: (int(r[0]), int(r[1])))

    index = {s["sid"]: i for i, s in enumerate(sentences)}
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["単位ID", "発話者", "開始文ID", "終了文ID", "文数", "発話番号", "ターン番号", "コード案", "要約",
                         "区切り票", "要確認", "本文"])
        for u, r in enumerate(all_rows, 1):
            a, b = index[r[2]], index[r[3]]
            span = [s for s in sentences[a : b + 1] if not s["相づち"]]
            utterances = sorted({int(s["発話番号"]) for s in span})
            writer.writerow([
                f"U{u:04d}", span[0]["発話者"], r[2], r[3], len(span), ",".join(map(str, utterances)),
                r[0], r[5], r[4], *notes.get((r[0], r[2]), ("", "")), "".join(s["文"] for s in span),
            ])
    print(f"-> {OUTPUT.relative_to(BASE)}（{len(all_rows)} 単位）")


if __name__ == "__main__":
    commands = {"prepare": prepare, "prepare-long": prepare_long, "merge": merge}
    if len(sys.argv) != 3 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    configure(sys.argv[2])
    commands[sys.argv[1]]()
