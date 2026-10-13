"""1文1行の CSV を、jev（TypeSafe System One）でコーディング単位にまとめ直す。

隣り合う文の「境目」ごとに、同じコーディング単位の続きかどうかを jev に判定させ、
その確率を組み合わせて区切る／つなぐを決める。話者が変わる境目は必ず区切る。
前の文が相づちだけ（jev で判定）の場合は、同じ話者の次の文に必ずつなぐ。

入力:  output/<name>.csv          （md_to_csv.py の出力）
出力:  output/<name>_boundaries.csv  境目ごとの判定結果（manual 列で手動修正できる）
       output/<name>_units.csv       コーディング単位ごとの CSV
       output/<name>_jev_cache.jsonl API 応答のキャッシュ（再実行時に API を呼ばない）

使い方:
  .venv/bin/python segment_units.py                 # output/ の全 CSV を処理
  .venv/bin/python segment_units.py --limit 20      # 最初の 20 境目だけ試す
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys
from pathlib import Path

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, TypeSafeError

BASE = Path(__file__).resolve().parent
OUTPUT_DIR = BASE / "output"
DERIVED_SUFFIXES = ("_boundaries", "_units")

DEFAULT_THEME = "大学生へのインタビュー：仕事（働くこと）とは何か"
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 2

# join_score = Σ 重み × 「つなぐ」方向の確率
WEIGHTS = {"same_unit": 0.5, "no_topic_shift": 0.3, "related": 0.2}
DEPENDENT_FORCE_JOIN = 0.8  # 前の文がないと意味が通じない確率がこれ以上なら必ずつなぐ
BACKCHANNEL_FORCE_JOIN = 0.7  # 前の文が相づちだけの確率がこれ以上なら、次の文に必ずつなぐ
JOIN_CUT = 0.5  # join_score がこれ以上ならつなぐ（--cut で変更可）
REVIEW_MARGIN = 0.15  # JOIN_CUT からこの幅以内の境目は要確認にする
JOINING_RELATIONS = ("elaboration", "continuation", "self_correction")

UNIT_DEFINITION = (
    "A coding unit is one meaning unit in qualitative interview analysis: a single topic, "
    "claim, reason, or episode expressed by one speaker. Consecutive sentences that develop "
    "the same point (examples, reasons, restatements, hesitations, the rest of the same story) "
    "belong to the same unit. A new unit starts when the speaker moves on to a different "
    "topic, claim, or episode. The transcript is spoken Japanese, so fillers and fragmented "
    "sentences are common."
)

# jev は英語が主な学習言語なので、問いは英語で書き、本文は日本語のまま渡す
QUESTIONS = {
    "same_unit": Noul(
        instructions=(
            "Should `b.text` be coded together with `a.text` as part of the same coding unit, "
            "as defined in `definition`?"
        ),
        criteria={
            "true": "`b.text` continues the same topic, claim, or episode as `a.text`",
            "false": "`b.text` starts a different topic, claim, or episode",
        },
    ),
    "topic_shift": Noul(
        instructions="Does `b.text` move on to a new topic or claim that differs from `a.text`?",
    ),
    "dependent": Noul(
        instructions=(
            "Does `b.text` depend on `a.text` to be understood, for example through a "
            "demonstrative, a conjunction, or an omitted subject or object that refers back "
            "to `a.text`?"
        ),
    ),
    "relation": Choice(
        instructions="How does `b.text` relate to `a.text`?",
        criteria={
            "elaboration": "Gives an example, detail, reason, or restatement of `a.text`",
            "continuation": "Continues the same story or line of thought as `a.text`",
            "self_correction": "Corrects, restarts, or finishes a hesitant `a.text`",
            "new_topic": "Moves on to a different topic or claim",
            "other": "None of the above",
        },
    ),
}


# コーディングの観点で直接聞く問い（境目ごとに QUESTIONS とは別リクエスト）
CODING_QUESTIONS = {
    "different_code": Noul(
        instructions=(
            "A qualitative researcher is coding this interview (see `theme`) by assigning a "
            "short code, i.e. a category label, to each passage. Would they assign a different "
            "code to `b.text` than to `a.text`?"
        ),
        criteria={
            "true": "`b.text` expresses a different idea and would get its own code",
            "false": "`b.text` would be coded with the same code as `a.text`",
        },
    ),
    "separate_quote": Noul(
        instructions=(
            "Could `b.text` be quoted on its own in a research report as evidence for a "
            "point that is different from the point made by `a.text`?"
        ),
    ),
}

# 相づち判定は文ごとに別リクエストで聞く（境目の問いのキャッシュを無効にしないため）
BACKCHANNEL_QUESTIONS = {
    "backchannel": Noul(
        instructions=(
            "Is `sentence.text` only a backchannel, filler, or bare acknowledgment "
            "(for example 「あーそうですね。」「いや全然。」「あーなるほど。」「はい。」) "
            "with no substantive content of its own, so that it should be attached to the "
            "same speaker's following sentence?"
        ),
        criteria={
            "true": "Only an acknowledgment or filler; no topic, claim, or information of its own",
            "false": "Contains its own content, such as an opinion, fact, question, or answer",
        },
    ),
}


def load_dotenv(path: Path) -> None:
    """.env の KEY=VALUE を環境変数に読み込む（既に設定済みの値は上書きしない）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)


def read_sentences(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, 1):
        row["sid"] = f"S{i:04d}"
    return rows


def build_state(sentences: list[dict], i: int, theme: str) -> dict:
    """文 i と i+1 の境目を判定するための state。"""

    def item(row: dict) -> dict:
        return {"speaker": row["発話者"], "text": row["文"]}

    return {
        "task": "Segmenting an interview transcript into coding units for qualitative analysis",
        "theme": theme,
        "definition": UNIT_DEFINITION,
        "context_before": [item(r) for r in sentences[max(0, i - CONTEXT_BEFORE) : i]],
        "a": item(sentences[i]),
        "b": item(sentences[i + 1]),
        "context_after": [item(r) for r in sentences[i + 2 : i + 2 + CONTEXT_AFTER]],
    }


def build_sentence_state(sentences: list[dict], i: int, theme: str) -> dict:
    """文 i 単体（相づちかどうか）を判定するための state。"""

    def item(row: dict) -> dict:
        return {"speaker": row["発話者"], "text": row["文"]}

    return {
        "task": "Checking whether a sentence in an interview transcript is only a backchannel",
        "theme": theme,
        "context_before": [item(r) for r in sentences[max(0, i - CONTEXT_BEFORE) : i]],
        "sentence": item(sentences[i]),
        "context_after": [item(r) for r in sentences[i + 1 : i + 1 + CONTEXT_AFTER]],
    }


def cache_key(state: dict, questions: dict = QUESTIONS) -> str:
    dumped = {k: q.model_dump() if hasattr(q, "model_dump") else repr(q) for k, q in questions.items()}
    payload = json.dumps([state, dumped], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cache(path: Path) -> dict[str, dict]:
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                cache[entry["key"]] = entry["answers"]
    return cache


async def ask_jev(client, state: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        res = await client.system_one(state=state, questions=QUESTIONS)
    relation = res.choices["relation"]
    return {
        "same_unit": res.nouls["same_unit"].noul,
        "topic_shift": res.nouls["topic_shift"].noul,
        "dependent": res.nouls["dependent"].noul,
        "relation": relation.choice,
        "relation_confidence": relation.confidence,
        "relation_probs": dict(relation.probabilities),
    }


async def ask_coding(client, state: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        res = await client.system_one(state=state, questions=CODING_QUESTIONS)
    return {k: res.nouls[k].noul for k in CODING_QUESTIONS}


async def ask_backchannel(client, state: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        res = await client.system_one(state=state, questions=BACKCHANNEL_QUESTIONS)
    return {"backchannel": res.nouls["backchannel"].noul}


def decide(ans: dict, cut: float = JOIN_CUT) -> tuple[float, str, bool]:
    """(join_score, 'join' | 'split', 要確認か) を返す。"""
    related = sum(ans["relation_probs"].get(k, 0.0) for k in JOINING_RELATIONS)
    score = (
        WEIGHTS["same_unit"] * ans["same_unit"]
        + WEIGHTS["no_topic_shift"] * (1 - ans["topic_shift"])
        + WEIGHTS["related"] * related
    )
    if ans["dependent"] >= DEPENDENT_FORCE_JOIN:
        return score, "join", False
    return score, "join" if score >= cut else "split", abs(score - cut) < REVIEW_MARGIN


def read_manual(path: Path) -> dict[str, str]:
    """前回の boundaries.csv で人が記入した manual 列（join / split）を引き継ぐ。"""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {
            r["境目ID"]: r["manual"].strip()
            for r in csv.DictReader(f)
            if r.get("manual", "").strip() in ("join", "split")
        }


async def process(csv_path: Path, theme: str, limit: int | None, concurrency: int, cut: float = JOIN_CUT) -> None:
    stem = csv_path.stem
    sentences = read_sentences(csv_path)
    boundaries_path = OUTPUT_DIR / f"{stem}_boundaries.csv"
    units_path = OUTPUT_DIR / f"{stem}_units.csv"
    cache_path = OUTPUT_DIR / f"{stem}_jev_cache.jsonl"

    manual = read_manual(boundaries_path)
    cache = load_cache(cache_path)

    # 判定が必要な境目（同じ話者が続く箇所）を集める。各境目で「境目の問い」と「前の文の相づち判定」を聞く
    targets = []
    for i in range(len(sentences) - 1):
        if sentences[i]["発話者"] != sentences[i + 1]["発話者"]:
            continue
        state = build_state(sentences, i, theme)
        bc_state = build_sentence_state(sentences, i, theme)
        targets.append((i, state, cache_key(state), bc_state, cache_key(bc_state, BACKCHANNEL_QUESTIONS),
                        cache_key(state, CODING_QUESTIONS)))
    if limit is not None:
        targets = targets[:limit]

    todo = [(i, ask_jev, s, k) for i, s, k, _, _, _ in targets if k not in cache]
    todo += [(i, ask_backchannel, s, k) for i, _, _, s, k, _ in targets if k not in cache]
    todo += [(i, ask_coding, s, k) for i, s, _, _, _, k in targets if k not in cache]
    print(f"{csv_path.name}: 文 {len(sentences)}、判定する境目 {len(targets)}（API 呼び出し {len(todo)}）")

    if todo:
        sem = asyncio.Semaphore(concurrency)
        async with AsyncTypeSafeClient() as client:
            done = 0

            async def run(i, ask, state, key):
                nonlocal done
                ans = await ask(client, state, sem)
                cache[key] = ans
                with cache_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": key, "sid": sentences[i]["sid"], "answers": ans}, ensure_ascii=False) + "\n")
                done += 1
                print(f"\r  {done}/{len(todo)}", end="", file=sys.stderr, flush=True)

            results = await asyncio.gather(*(run(*t) for t in todo), return_exceptions=True)
            print(file=sys.stderr)
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                print(f"  警告: {len(errors)} 件の境目で API 呼び出しに失敗しました（再実行すると失敗分だけ再試行します）: {errors[0]!r}", file=sys.stderr)

    # 境目ごとの判定を確定する
    target_keys = {i: (k, bk, ck) for i, _, k, _, bk, ck in targets}
    boundary_rows = []
    join_after = {}  # 文 i の後ろでつなぐか
    for i in range(len(sentences) - 1):
        a, b = sentences[i], sentences[i + 1]
        bid = f"{a['sid']}-{b['sid']}"
        row = {"境目ID": bid, "前の文ID": a["sid"], "後の文ID": b["sid"], "発話者": a["発話者"]}
        if a["発話者"] != b["発話者"]:
            row.update(判定="split", 根拠="話者交代", 要確認="")
            join_after[i] = False
        elif i in target_keys and all(k in cache for k in target_keys[i]):
            ans = {**cache[target_keys[i][0]], **cache[target_keys[i][2]]}
            backchannel = cache[target_keys[i][1]]["backchannel"]
            score, decision, review = decide(ans, cut)
            basis = "jev"
            if backchannel >= BACKCHANNEL_FORCE_JOIN:
                decision, review, basis = "join", False, "相づち"
            row.update(
                same_unit=f"{ans['same_unit']:.3f}",
                topic_shift=f"{ans['topic_shift']:.3f}",
                dependent=f"{ans['dependent']:.3f}",
                different_code=f"{ans['different_code']:.3f}",
                separate_quote=f"{ans['separate_quote']:.3f}",
                relation=ans["relation"],
                relation_confidence=f"{ans['relation_confidence']:.3f}",
                join_score=f"{score:.3f}",
                backchannel=f"{backchannel:.3f}",
                判定=decision,
                根拠=basis,
                要確認="1" if review else "",
            )
            join_after[i] = decision == "join"
        else:
            # --limit の対象外か API 失敗。安全側（区切る）に倒して要確認にする
            row.update(判定="split", 根拠="未判定", 要確認="1")
            join_after[i] = False
        if bid in manual:
            row["manual"] = manual[bid]
            join_after[i] = manual[bid] == "join"
        row["前の文"] = a["文"]
        row["後の文"] = b["文"]
        boundary_rows.append(row)

    boundary_fields = [
        "境目ID", "前の文ID", "後の文ID", "発話者", "same_unit", "topic_shift", "dependent",
        "different_code", "separate_quote", "relation", "relation_confidence", "join_score", "backchannel", "判定", "根拠", "要確認", "manual",
        "前の文", "後の文",
    ]
    with boundaries_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=boundary_fields)
        writer.writeheader()
        writer.writerows(boundary_rows)

    # 単位にまとめる
    units = []
    current = [sentences[0]]
    review_in_unit = False
    for i in range(len(sentences) - 1):
        brow = boundary_rows[i]
        if join_after[i]:
            current.append(sentences[i + 1])
            review_in_unit |= brow["要確認"] == "1" and "manual" not in brow
        else:
            units.append((current, review_in_unit))
            current, review_in_unit = [sentences[i + 1]], False
    units.append((current, review_in_unit))

    with units_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["単位ID", "発話者", "開始文ID", "終了文ID", "文数", "発話番号", "本文", "内部に要確認の境目"])
        for n, (rows, review) in enumerate(units, 1):
            utterances = sorted({int(r["発話番号"]) for r in rows})
            writer.writerow([
                f"U{n:04d}", rows[0]["発話者"], rows[0]["sid"], rows[-1]["sid"], len(rows),
                ",".join(map(str, utterances)), "".join(r["文"] for r in rows), "1" if review else "",
            ])

    n_review = sum(1 for r in boundary_rows if r["要確認"] == "1" and "manual" not in r)
    print(f"  -> {units_path.relative_to(BASE)}（{len(units)} 単位）")
    print(f"  -> {boundaries_path.relative_to(BASE)}（要確認の境目 {n_review}）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="*", type=Path, help="対象の CSV（省略時は output/ の全文 CSV）")
    parser.add_argument("--theme", default=DEFAULT_THEME, help="インタビューのテーマ（state に入れる）")
    parser.add_argument("--limit", type=int, help="判定する境目の数を先頭から制限する（試し実行用）")
    parser.add_argument("--cut", type=float, default=JOIN_CUT, help="join_score がこの値以上ならつなぐ（上げるほど細かく区切る）")
    parser.add_argument("--concurrency", type=int, default=8, help="同時に送るリクエスト数")
    args = parser.parse_args()

    load_dotenv(BASE / ".env")
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit(".env の TYPESAFE_API_KEY が空です。API キーを記入してください。")

    paths = args.csv or [
        p for p in sorted(OUTPUT_DIR.glob("*.csv")) if not p.stem.endswith(DERIVED_SUFFIXES)
    ]
    try:
        for p in paths:
            asyncio.run(process(p, args.theme, args.limit, args.concurrency, args.cut))
    except TypeSafeError as e:
        sys.exit(f"TypeSafe API エラー: {e}")


if __name__ == "__main__":
    main()
