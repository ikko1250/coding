"""1文1行の書き起こしから、相づちとターンを判定する共通処理。

相づち: 同じ話者の発言に挟まれた、別の話者の短い発言（ターン全体で BACKCHANNEL_MAX 字以下）。
        例）話者A「…」→ 質問者1「うん。」→ 話者A「…」 の「うん。」
        相づちはコーディング単位に含めず、前後の話者Aの発言は1つのターンとしてつなげて扱う。
ターン: 相づちを飛ばしたうえで、同じ話者が続けて話した文のまとまり。単位はターンをまたがない。
"""

import unicodedata

BACKCHANNEL_MAX = 10


def mark_turns(rows: list[dict]) -> None:
    """rows（発話者・文 を持つ dict のリスト）に、相づち（bool）とターン（1 始まり、相づちは None）を書き込む。"""
    # まず話者が変わるごとの素のターンに分ける
    raw = []
    for k, r in enumerate(rows):
        if raw and rows[raw[-1][-1]]["発話者"] == r["発話者"]:
            raw[-1].append(k)
        else:
            raw.append([k])
    # 前から順に、直前の相づちでないターンの話者（今話している人）を基準に判定する。
    # 話者A「…」→ 質問者1「うん。」→ 話者A「バーテンダーと。」→ 質問者1「バーテンダーと。」→ 話者A「…」
    # のように短い発言が交互に続いても、話者A の発言は相づちにならない。
    backchannel = set()
    main = rows[raw[0][0]]["発話者"]
    for t in range(1, len(raw)):
        here = rows[raw[t][0]]["発話者"]
        after = rows[raw[t + 1][0]]["発話者"] if t + 1 < len(raw) else None
        text = "".join(rows[k]["文"] for k in raw[t])
        if here != main and after == main and len(unicodedata.normalize("NFKC", text)) <= BACKCHANNEL_MAX:
            backchannel.update(raw[t])
        else:
            main = here

    turn, speaker = 0, None
    for k, r in enumerate(rows):
        # 相づち指定（True / False）があれば自動判定より優先する。None は自動判定のまま
        override = r.get("相づち指定")
        r["相づち"] = override if override is not None else k in backchannel
        if r["相づち"]:
            r["ターン"] = None
            continue
        if r["発話者"] != speaker:
            turn += 1
            speaker = r["発話者"]
        r["ターン"] = turn
