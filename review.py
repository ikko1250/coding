"""Haiku が作ったコーディング単位を、人の印と文字起こしの修正で確定させる。

使い方（<name> は output/<name>.csv の名前。例: interview_taharasan_2）:
  .venv/bin/python review.py prepare <name>   # output/<name>_review.csv（1文1行の作業用 CSV）を作る
  （作業用 CSV に印を付けたり、文字起こしを直したりする）
  .venv/bin/python review.py apply <name>     # 反映して output/<name>_units_final.csv などを作る

印の意味（区切る 列・つなぐ 列。空欄でなければ何でもよい。おすすめは 1）:
  区切る … この文から新しい単位を始める
  つなぐ … この文から始まる単位を、1つ前の単位とまとめる（単位の先頭 ● の行に付ける）
話者が変わる位置は、印に関係なく必ず区切る（話者を修正した場合は修正後の話者で判定する）。
同じ話者に挟まれた他の話者の短い相づちは、単位ID が（相づち）になり、どの単位にも入らない（transcript.py）。
相づちを飛ばした前後の発言は同じターンとして扱うので、つなぐ で1つの単位にできる。

相づちの指定:
  相づち 列に 1 が付いた行が相づち。最初の prepare で自動判定の結果が入る。
  1 を消すと通常の発言に、1 を付ける（空欄でなければ何でもよい）と相づちになる。
  一度作った作業用 CSV では、この列の内容が自動判定より優先される。

文字起こしの修正:
  文 列・発話者 列を直接書き換えると、最終版に反映する。
  文 列に / を書くと、そこで文を分ける（S0123 → S0123-1, S0123-2）。分けた文は同じ単位に入る。
  分けた位置で単位も区切りたいときは、prepare を再実行して分けた文の行を作り、その行に 区切る を付ける。
  行の追加・削除・並べ替えはしない。
  修正後の全文は output/<name>_sentences_final.csv に、元の文との対応付きで出力する。
prepare を再実行しても、すでに付けた印と文字起こしの修正は引き継ぐ。
"""

import csv
import re
import sys
import unicodedata
from pathlib import Path

from transcript import mark_turns

BASE = Path(__file__).resolve().parent
SOURCE = UNITS = REVIEW = FINAL = SENTENCES_FINAL = Path()  # configure() で設定する
BACKCHANNEL = "（相づち）"

LONG_UNIT_MIN = 10  # この文数以上の単位は、内部の文すべてに要確認の注記を付ける
SHORT_TAIL_MAX = 10  # 単位の末尾がこの字数以下の文なら、相づち・断片の付け先として注記する
SPLIT, JOIN = "区切る", "つなぐ"
SENTENCE_SPLITTER = re.compile(r"[/／]")
BACKCHANNEL_COL = "相づち"
REVIEW_HEADER = ["文ID", "単位ID", "先頭", BACKCHANNEL_COL, SPLIT, JOIN, "発話者", "コード案", "文", "要確認"]


def configure(name: str) -> None:
    global SOURCE, UNITS, REVIEW, FINAL, SENTENCES_FINAL
    SOURCE = BASE / "output" / f"{name}.csv"
    UNITS = BASE / "output" / f"{name}_units_haiku.csv"
    REVIEW = BASE / "output" / f"{name}_review.csv"
    FINAL = BASE / "output" / f"{name}_units_final.csv"
    SENTENCES_FINAL = BASE / "output" / f"{name}_sentences_final.csv"
    for path in (SOURCE, UNITS):
        if not path.exists():
            sys.exit(f"{path.relative_to(BASE)} がありません")


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load() -> tuple[list[dict], dict[str, int], list[dict], dict[str, dict]]:
    """元データの (文, 文ID->位置, 単位, 文ID->その文を含む単位)。相づちの文は unit_of に入らない。"""
    sentences = read_csv(SOURCE)
    for i, row in enumerate(sentences, 1):
        row["sid"] = f"S{i:04d}"
    mark_turns(sentences)
    index = {s["sid"]: i for i, s in enumerate(sentences)}
    units = read_csv(UNITS)
    unit_of = {}
    for u in units:
        for i in range(index[u["開始文ID"]], index[u["終了文ID"]] + 1):
            if not sentences[i]["相づち"]:
                unit_of[sentences[i]["sid"]] = u
    return sentences, index, units, unit_of


def is_marked(value: str | None) -> bool:
    return bool(unicodedata.normalize("NFKC", value or "").strip())


def current_rows(sentences: list[dict]) -> list[dict]:
    """作業用 CSV の内容（修正・分割・印）を反映した、今の1文1行のリスト。

    各要素: sid, base（元の文ID）, 発話番号, 発話者, 文, 区切る, つなぐ（印の文字列）,
            相づち指定（作業用 CSV に 相づち 列があれば True / False、なければ None = 自動判定）
    """
    if not REVIEW.exists():
        return [{"sid": s["sid"], "base": s["sid"], "発話番号": s["発話番号"], "発話者": s["発話者"],
                 "文": s["文"], SPLIT: "", JOIN: ""} for s in sentences]

    review = read_csv(REVIEW)
    has_backchannel_col = bool(review) and BACKCHANNEL_COL in review[0]
    bases = [r["文ID"].split("-")[0] for r in review]
    collapsed = [b for k, b in enumerate(bases) if k == 0 or bases[k - 1] != b]
    if collapsed != [s["sid"] for s in sentences]:
        sys.exit(f"{REVIEW.name} の文ID の並びが元データと一致しません（行の追加・削除・並べ替えをしていませんか）")

    by_sid = {s["sid"]: s for s in sentences}
    pieces = []  # 分割前の ID 付けをしていない状態
    for r, base in zip(review, bases):
        parts = [p.strip() for p in SENTENCE_SPLITTER.split(r.get("文") or "")]
        parts = [p for p in parts if p] or [""]  # 全部空なら空のまま残し、apply で検出する
        for k, text in enumerate(parts):
            pieces.append({
                "base": base, "発話番号": by_sid[base]["発話番号"], "発話者": (r.get("発話者") or "").strip(), "文": text,
                # 印は、行の最初の断片（その行の文の頭）の前の境目に対するもの
                SPLIT: r.get(SPLIT, "") if k == 0 else "", JOIN: r.get(JOIN, "") if k == 0 else "",
                "相づち指定": is_marked(r.get(BACKCHANNEL_COL)) if has_backchannel_col else None,
            })
    count = {}
    for p in pieces:
        count[p["base"]] = count.get(p["base"], 0) + 1
    seen = {}
    for p in pieces:
        seen[p["base"]] = seen.get(p["base"], 0) + 1
        p["sid"] = p["base"] if count[p["base"]] == 1 else f"{p['base']}-{seen[p['base']]}"
    return pieces


def position_of(rows: list[dict]) -> dict[str, int]:
    return {r["sid"]: k for k, r in enumerate(rows)}


def base_starts(units: list[dict]) -> set[str]:
    return {u["開始文ID"] for u in units}


def notes_for(sentences: list[dict], index: dict[str, int], units: list[dict]) -> dict[str, list[str]]:
    """元の文ID -> 要確認の注記（その文の直前の境目について）"""
    found: dict[str, list[str]] = {}

    def add(sid: str, note: str) -> None:
        found.setdefault(sid, []).append(note)

    for k, u in enumerate(units):
        # 多数決で判断が割れた境目
        if u["区切り票"] and "開始の区切りが" in u["要確認"]:
            add(u["開始文ID"], f"この前の区切りは {u['区切り票']} 票で採用")
        for sid, v in re.findall(r"(S\d{4})前\((\d+/\d+)\)", u["要確認"]):
            add(sid, f"この前で区切る案が {v} 票（不採用）")
        a, b = index[u["開始文ID"]], index[u["終了文ID"]]
        if int(u["文数"]) >= LONG_UNIT_MIN:
            for i in range(a + 1, b + 1):
                if not sentences[i]["相づち"]:
                    add(sentences[i]["sid"], f"{u['文数']} 文の長い単位の途中")
        nxt = units[k + 1] if k + 1 < len(units) else None
        if b > a and nxt and nxt["ターン番号"] == u["ターン番号"] and len(sentences[b]["文"]) <= SHORT_TAIL_MAX:
            add(sentences[b]["sid"], "単位末尾の短い文（次の単位に付けるなら この行を区切る＋次の単位の先頭をつなぐ）")
    return found


def prepare() -> None:
    sentences, index, units, unit_of = load()
    notes = notes_for(sentences, index, units)
    starts = base_starts(units)
    original = {s["sid"]: s for s in sentences}
    rows = current_rows(sentences)
    mark_turns(rows)

    out, kept, kept_edits = [], 0, 0
    for k, r in enumerate(rows):
        u = None if r["相づち"] else unit_of.get(r["base"])
        first_piece = k == 0 or rows[k - 1]["base"] != r["base"]
        head = bool(u) and first_piece and r["base"] in starts
        if is_marked(r[SPLIT]) or is_marked(r[JOIN]):
            kept += 1
        if r["sid"] != r["base"] or (r["発話者"], r["文"]) != (original[r["base"]]["発話者"], original[r["base"]]["文"]):
            kept_edits += 1
        out.append({
            "文ID": r["sid"],
            "単位ID": u["単位ID"] if u else (BACKCHANNEL if r["相づち"] else "（未割り当て）"),
            "先頭": "●" if head else "",
            BACKCHANNEL_COL: "1" if r["相づち"] else "",
            SPLIT: r[SPLIT],
            JOIN: r[JOIN],
            "発話者": r["発話者"],
            "コード案": u["コード案"] if head else "",
            "文": r["文"],
            "要確認": "／".join(notes.get(r["base"], [])) if first_piece else "",
        })
    with REVIEW.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REVIEW_HEADER)
        writer.writeheader()
        writer.writerows(out)
    n_notes = sum(1 for r in out if r["要確認"])
    print(f"-> {REVIEW.relative_to(BASE)}（{len(out)} 文、要確認の注記 {n_notes} 件、"
          f"引き継いだ印 {kept} 件、文字起こしの修正 {kept_edits} 件）")


def apply() -> None:
    sentences, index, units, unit_of = load()
    if not REVIEW.exists():
        sys.exit(f"{REVIEW.name} がありません。先に prepare を実行してください。")
    rows = current_rows(sentences)
    original = {s["sid"]: s for s in sentences}
    pieces_of = {}
    for r in rows:
        pieces_of.setdefault(r["base"], []).append(r["sid"])

    errors, warnings = [], []
    # 文字起こしの修正・分割
    known_speakers = {s["発話者"] for s in sentences}
    fixes = {}  # 文ID -> 修正内容
    for r in rows:
        orig = original[r["base"]]
        if not r["発話者"] or not r["文"]:
            errors.append(f"{r['sid']}: 発話者 と 文 は空にできません（文を消したい場合は相談してください）")
            continue
        if r["発話者"] not in known_speakers:
            warnings.append(f"{r['sid']}: 新しい発話者名「{r['発話者']}」です（表記ゆれでないか確認してください）")
        notes = []
        if r["発話者"] != orig["発話者"]:
            notes.append(f"発話者: {orig['発話者']} → {r['発話者']}")
        if len(pieces_of[r["base"]]) > 1:
            notes.append(f"分割（元の文: {orig['文']}）")
        elif r["文"] != orig["文"]:
            notes.append(f"文: {orig['文']}")
        if notes:
            fixes[r["sid"]] = "／".join(notes)
    if errors:
        sys.exit("\n".join(errors))
    # 相づちとターンは、修正後の話者・文と 相づち 列の指定で判定し直す。turn_start はターン（相づちを除く）の最初の文
    auto = [dict(r, 相づち指定=None) for r in rows]
    mark_turns(auto)
    mark_turns(rows)
    for r, a in zip(rows, auto):
        if r["相づち"] != a["相づち"]:
            note = "相づち: 手動で指定" if r["相づち"] else "相づち: 手動で解除"
            fixes[r["sid"]] = "／".join(filter(None, [fixes.get(r["sid"]), note]))
    turn_start, prev_turn = [], None
    for r in rows:
        turn_start.append(not r["相づち"] and r["ターン"] != prev_turn)
        if not r["相づち"]:
            prev_turn = r["ターン"]

    # 区切る・つなぐの印
    starts = {pieces_of[b][0] for b in base_starts(units)}
    changed = 0
    for k, r in enumerate(rows):
        sid, split, join = r["sid"], is_marked(r[SPLIT]), is_marked(r[JOIN])
        if split and join:
            errors.append(f"{sid}: {SPLIT} と {JOIN} の両方に印があります")
        elif (split or join) and r["相づち"]:
            warnings.append(f"{sid}: 相づちの行なので印は無視しました")
        elif split:
            if sid in starts:
                warnings.append(f"{sid}: すでに単位の先頭なので {SPLIT} の印は無視しました")
            else:
                starts.add(sid)
                changed += 1
        elif join:
            if turn_start[k]:
                warnings.append(f"{sid}: 話者が変わる位置なので {JOIN} の印は無視しました")
            elif sid not in starts:
                warnings.append(f"{sid}: 単位の先頭ではないので {JOIN} の印は無視しました（先頭 ● の行に付けてください）")
            else:
                starts.discard(sid)
                changed += 1
    if errors:
        sys.exit("\n".join(errors))
    for w in warnings:
        print(f"警告: {w}")
    # 話者が変わる位置（ターンの先頭）は必ず単位の先頭。相づちの行は単位の先頭にならない
    starts |= {r["sid"] for k, r in enumerate(rows) if turn_start[k]}
    starts = {sid for sid in starts if not rows[position_of(rows)[sid]]["相づち"]}

    # 元の単位と範囲が同じかどうか（分割した文は、元の文の最初の断片〜最後の断片で比べる）
    by_range = {(pieces_of[u["開始文ID"]][0], pieces_of[u["終了文ID"]][-1]): u for u in units}
    position = position_of(rows)
    ordered = sorted(starts, key=position.get)
    unit_id_of = {}
    with FINAL.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["単位ID", "発話者", "開始文ID", "終了文ID", "文数", "発話番号", "ターン番号", "コード案", "要約", "変更", "本文"])
        for n, start in enumerate(ordered, 1):
            a = position[start]
            b = position[ordered[n]] - 1 if n < len(ordered) else len(rows) - 1
            span = [r for r in rows[a : b + 1] if not r["相づち"]]  # 間に挟まる相づちは単位に含めない
            same = by_range.get((start, span[-1]["sid"]))
            notes = []
            if same:
                code, summary = same["コード案"], same["要約"]
            else:
                # 元の単位と範囲が変わった：重なる元の単位のコード案を並べ、要約は空にする
                code = " / ".join(dict.fromkeys(unit_of[r["base"]]["コード案"] for r in span if r["base"] in unit_of))
                summary = ""
                notes.append("範囲を変更（コード案・要約の見直しが必要）")
            if any(r["sid"] in fixes for r in span):
                notes.append("文字起こしを修正" + ("（要約の確認を推奨）" if same else ""))
            utterances = sorted({int(r["発話番号"]) for r in span})
            unit_id = f"U{n:04d}"
            for r in span:
                unit_id_of[r["sid"]] = unit_id
            writer.writerow([
                unit_id, span[0]["発話者"], start, span[-1]["sid"], len(span), ",".join(map(str, utterances)),
                rows[a]["ターン"], code, summary, "／".join(notes), "".join(r["文"] for r in span),
            ])

    with SENTENCES_FINAL.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["文ID", "発話番号", "発話者", "文", "単位ID", "修正内容（元の値）"])
        for r in rows:
            unit_id = BACKCHANNEL if r["相づち"] else unit_id_of[r["sid"]]
            writer.writerow([r["sid"], r["発話番号"], r["発話者"], r["文"], unit_id, fixes.get(r["sid"], "")])

    n_split = sum(1 for p in pieces_of.values() if len(p) > 1)
    print(f"印を反映した箇所 {changed} 件、文字起こしの修正 {len(fixes)} 件（うち文の分割 {n_split} 文）")
    print(f"-> {FINAL.relative_to(BASE)}（{len(ordered)} 単位）")
    print(f"-> {SENTENCES_FINAL.relative_to(BASE)}（{len(rows)} 文）")


if __name__ == "__main__":
    commands = {"prepare": prepare, "apply": apply}
    if len(sys.argv) != 3 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    configure(sys.argv[2])
    commands[sys.argv[1]]()
