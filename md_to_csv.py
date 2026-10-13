"""input/ 内の Markdown 発話ログを、1文1行の CSV に変換する。

入力行の形式: ⚬ 発話者：「発話内容」 または **発話者**：発話内容
見出し・凡例・区切り線と、（周囲の雑音）のような行全体が括弧書きの注記は読み飛ばす。
出力列: 発話番号, 文番号, 発話者, 文
"""

import csv
import re
import sys
from pathlib import Path

LINE_RE = re.compile(r"^\s*[⚬・\-*]?\s*(?P<speaker>[^：:]+)[：:]\s*「(?P<text>.*?)」?\s*$")
BOLD_LINE_RE = re.compile(r"^\*\*(?P<speaker>[^*]+)\*\*[：:]\s*(?P<text>.+?)\s*$")
SKIP_RE = re.compile(r"^(#|- \*\*|---\s*$)")
NOTE_RE = re.compile(r"^（[^）]*）$")
# 文末記号（。！？!?）の直後で区切る。連続する記号と、直後の（笑）のような括弧書きはまとめて前の文に含める
SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]*(?:（[^）]*）)*")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_RE.findall(text) if s.strip()]


def convert(md_path: Path, csv_path: Path) -> int:
    rows = []
    utterance_no = 0
    for lineno, line in enumerate(md_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or SKIP_RE.match(line):
            continue
        if NOTE_RE.match(line.strip()):
            print(f"注記として読み飛ばし: {md_path.name}:{lineno} {line.strip()}", file=sys.stderr)
            continue
        m = BOLD_LINE_RE.match(line) or LINE_RE.match(line)
        if not m:
            print(f"警告: {md_path.name}:{lineno} を解析できませんでした: {line[:40]}", file=sys.stderr)
            continue
        utterance_no += 1
        speaker = m["speaker"].strip()
        for sentence_no, sentence in enumerate(split_sentences(m["text"]), 1):
            rows.append([utterance_no, sentence_no, speaker, sentence])

    # Excel でも文字化けしないよう BOM 付き UTF-8 で出力
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["発話番号", "文番号", "発話者", "文"])
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    base = Path(__file__).resolve().parent
    input_dir = base / "input"
    output_dir = base / "output"
    output_dir.mkdir(exist_ok=True)
    for md_path in sorted(input_dir.glob("*.md")):
        csv_path = output_dir / f"{md_path.stem}.csv"
        n = convert(md_path, csv_path)
        print(f"{md_path.name} -> {csv_path.relative_to(base)} ({n} 文)")


if __name__ == "__main__":
    main()
