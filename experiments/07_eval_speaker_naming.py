from __future__ import annotations

import bisect
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.artifacts import captions_to_utterances, read_json, read_jsonl, write_jsonl
from pipeline.models import Meeting
from pipeline.stages import name_speakers

BENCHMARK = ROOT / "data" / "benchmark" / "2025-04-23-transportation"
OUTPUT = BENCHMARK / "speaker-naming-eval.md"
MATCH_TOLERANCE_SEC = 8.0
NICKNAME_CANONICAL_FIRST_NAMES = {
    "alex": "Alexandra",
    "alexandra": "Alexandra",
    "ben": "Benjamin",
    "benjamin": "Benjamin",
    "beth": "Elizabeth",
    "betty": "Elizabeth",
    "bill": "William",
    "billy": "William",
    "bob": "Robert",
    "bobby": "Robert",
    "catherine": "Catherine",
    "cathy": "Catherine",
    "charles": "Charles",
    "charlie": "Charles",
    "chris": "Christopher",
    "christopher": "Christopher",
    "chuck": "Charles",
    "dan": "Daniel",
    "daniel": "Daniel",
    "dave": "David",
    "david": "David",
    "ed": "Edward",
    "eddie": "Edward",
    "edward": "Edward",
    "eliza": "Elizabeth",
    "elizabeth": "Elizabeth",
    "jack": "John",
    "james": "James",
    "jen": "Jennifer",
    "jennifer": "Jennifer",
    "jim": "James",
    "jimmy": "James",
    "joe": "Joseph",
    "john": "John",
    "joseph": "Joseph",
    "kate": "Katherine",
    "katherine": "Katherine",
    "kathy": "Katherine",
    "katie": "Katherine",
    "liz": "Elizabeth",
    "maggie": "Margaret",
    "margaret": "Margaret",
    "matt": "Matthew",
    "matthew": "Matthew",
    "meg": "Margaret",
    "michael": "Michael",
    "mike": "Michael",
    "nick": "Nicholas",
    "nicholas": "Nicholas",
    "pat": "Patricia",
    "patricia": "Patricia",
    "patty": "Patricia",
    "peggy": "Margaret",
    "rebecca": "Rebecca",
    "rich": "Richard",
    "richard": "Richard",
    "rick": "Richard",
    "rob": "Robert",
    "robbie": "Robert",
    "robert": "Robert",
    "sam": "Samuel",
    "samuel": "Samuel",
    "sue": "Susan",
    "susan": "Susan",
    "susie": "Susan",
    "ted": "Edward",
    "thomas": "Thomas",
    "tim": "Timothy",
    "timothy": "Timothy",
    "tom": "Thomas",
    "tony": "Anthony",
    "will": "William",
    "william": "William",
}


@dataclass(frozen=True)
class ReferenceUtterance:
    seek: float
    speaker: str
    text: str
    source: str


def main() -> int:
    force = "--force" in sys.argv
    meeting = _meeting()
    _prepare_pseudo_utterances(meeting)
    named_path = meeting.meeting_dir / "utterances-named.jsonl"
    if force or not named_path.exists():
        named_path = name_speakers(meeting)
    named = read_jsonl(named_path)
    references = _read_citymeetings_references(BENCHMARK)
    meta_path = meeting.meeting_dir / "name-speakers-meta.json"
    meta = read_json(meta_path) if meta_path.exists() else {}
    report = _score(named, references, meta)
    OUTPUT.write_text(report)
    print(report)
    return 0


def _meeting() -> Meeting:
    payload = read_json(BENCHMARK / "meeting.json")
    return Meeting(
        meeting_key="2025-04-23-transportation",
        meeting_dir=BENCHMARK,
        legistar_event_id=payload.get("legistar_event_id"),
        legistar_event_guid=payload.get("legistar_event_guid"),
        viebit_filename=payload.get("viebit_file"),
        viebit_hash=payload.get("viebit_hash"),
        body_name=payload.get("body"),
        event_date=payload.get("date"),
        event_time=payload.get("time"),
        duration_seconds=payload.get("duration_sec"),
    )


def _prepare_pseudo_utterances(meeting: Meeting) -> None:
    captions = read_jsonl(meeting.meeting_dir / "captions-clean.jsonl")
    utterances = captions_to_utterances(captions)
    write_jsonl(meeting.meeting_dir / "utterances.jsonl", utterances)


def _read_citymeetings_references(root: Path) -> list[ReferenceUtterance]:
    refs: list[ReferenceUtterance] = []
    for path in sorted(root.glob("citymeetings-chapter-*.html")):
        soup = BeautifulSoup(path.read_text(errors="replace"), "html.parser")
        transcript = soup.find(attrs={"x-show": "toShow == 'transcript'"})
        if transcript is None:
            continue
        for block in transcript.find_all("div", class_="flex flex-col gap-y-2", recursive=False):
            speaker_node = block.find("div", class_="font-semibold")
            if speaker_node is None:
                continue
            speaker = _normalize_speaker(speaker_node.get_text(" ", strip=True))
            if not speaker:
                continue
            for row in block.find_all("div", class_="flex gap-x-4 items-center"):
                click_node = row.find(attrs={"@click": re.compile(r"player\.seekTo")})
                sentence = row.find("div", class_="sentence")
                if click_node is None or sentence is None:
                    continue
                match = re.search(r"seekTo\(([\d.]+)\)", str(click_node.get("@click")))
                if not match:
                    continue
                refs.append(
                    ReferenceUtterance(
                        seek=float(match.group(1)),
                        speaker=speaker,
                        text=sentence.get_text(" ", strip=True),
                        source=path.name,
                    )
                )
    if not refs:
        raise RuntimeError(f"no citymeetings reference utterances parsed from {root}")
    return refs


def _score(named: list[dict[str, Any]], references: list[ReferenceUtterance], meta: dict[str, Any]) -> str:
    starts = [float(row["t0"]) for row in named]
    matched = 0
    correct = 0
    misses = []
    mismatches = []
    confusion: Counter[tuple[str, str]] = Counter()

    for ref in references:
        idx = _nearest_index(starts, ref.seek)
        if idx is None or abs(starts[idx] - ref.seek) > MATCH_TOLERANCE_SEC:
            misses.append(ref)
            continue
        matched += 1
        predicted = _normalize_speaker(str(named[idx].get("speaker") or "UNKNOWN"))
        expected = _normalize_speaker(ref.speaker)
        if predicted == expected:
            correct += 1
        else:
            confusion[(expected, predicted)] += 1
            mismatches.append(
                {
                    "seek": ref.seek,
                    "expected": expected,
                    "predicted": predicted,
                    "reference_text": ref.text,
                    "matched_text": str(named[idx].get("text") or ""),
                    "source": ref.source,
                }
            )

    accuracy = correct / matched if matched else 0
    usage = meta.get("usage", {}) if isinstance(meta.get("usage"), dict) else {}
    lines = [
        "# Speaker Naming Eval - 2025-04-23 Transportation",
        "",
        f"- References parsed: {len(references)}",
        f"- Matched by time (+/- {MATCH_TOLERANCE_SEC:.0f}s): {matched}",
        f"- Correct: {correct}",
        f"- Accuracy: {accuracy:.1%}",
        f"- Unmatched references: {len(misses)}",
        f"- Naming mode: {meta.get('mode', 'unknown')}",
        f"- Chunks: {meta.get('chunks', 'unknown')}",
        f"- Gemini tokens: prompt={usage.get('promptTokenCount', 'n/a')}, output={usage.get('candidatesTokenCount', 'n/a')}, thoughts={usage.get('thoughtsTokenCount', 'n/a')}, total={usage.get('totalTokenCount', 'n/a')}",
        f"- Estimated Gemini cost: ${float(meta.get('estimated_cost_usd') or 0):.4f}",
        "",
        "## Confusion List",
        "",
    ]
    if confusion:
        lines.append("| expected | predicted | count |")
        lines.append("|---|---|---:|")
        for (expected, predicted), count in confusion.most_common(30):
            lines.append(f"| {expected} | {predicted} | {count} |")
    else:
        lines.append("No speaker confusions among matched references.")

    if mismatches:
        lines.extend(["", "## Mismatch Sample", ""])
        lines.append("| seek | expected | predicted | reference text | matched text |")
        lines.append("|---:|---|---|---|---|")
        for item in mismatches[:20]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{item['seek']:.3f}",
                        _md(item["expected"]),
                        _md(item["predicted"]),
                        _md(str(item["reference_text"])[:100]),
                        _md(str(item["matched_text"])[:100]),
                    ]
                )
                + " |"
            )

    if misses:
        lines.extend(["", "## Unmatched Reference Sample", ""])
        for ref in misses[:10]:
            lines.append(f"- {ref.source} @ {ref.seek:.3f}s: {ref.speaker} - {ref.text[:120]}")
    lines.append("")
    return "\n".join(lines)


def _nearest_index(starts: list[float], seek: float) -> int | None:
    if not starts:
        return None
    right = bisect.bisect_left(starts, seek)
    candidates = []
    if right < len(starts):
        candidates.append(right)
    if right:
        candidates.append(right - 1)
    return min(candidates, key=lambda idx: abs(starts[idx] - seek)) if candidates else None


def _normalize_speaker(value: str) -> str:
    value = re.sub(r"^Council\s+Member\s+", "", value.strip(), flags=re.I)
    value = re.sub(r"^Member\s+of\s+the\s+Public\s*-\s*", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value)
    parts = value.split()
    if len(parts) >= 2:
        first_key = re.sub(r"[^a-z]", "", parts[0].lower())
        canonical = NICKNAME_CANONICAL_FIRST_NAMES.get(first_key)
        if canonical:
            parts[0] = canonical
            value = " ".join(parts)
    return value


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
