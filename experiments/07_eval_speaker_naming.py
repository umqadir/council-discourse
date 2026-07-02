from __future__ import annotations

import bisect
import re
import sys
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
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


@dataclass(frozen=True)
class MatchedUtterance:
    ref: ReferenceUtterance
    index: int
    expected: str
    predicted: str
    matched_text: str


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
            speaker = _display_speaker(speaker_node.get_text(" ", strip=True))
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
    misses = []
    raw_matches: list[MatchedUtterance] = []
    confusion: Counter[tuple[str, str]] = Counter()

    for ref in references:
        idx = _nearest_index(starts, ref.seek)
        if idx is None or abs(starts[idx] - ref.seek) > MATCH_TOLERANCE_SEC:
            misses.append(ref)
            continue
        raw_matches.append(
            MatchedUtterance(
                ref=ref,
                index=idx,
                expected=_display_speaker(ref.speaker),
                predicted=_display_speaker(str(named[idx].get("speaker") or "UNKNOWN")),
                matched_text=str(named[idx].get("text") or ""),
            )
        )

    scored = _deskew_matches(raw_matches)
    matched = len(scored)
    strict_correct = 0
    same_person_correct = 0
    strict_only_mismatches: list[MatchedUtterance] = []
    same_person_mismatches: list[MatchedUtterance] = []
    for item in scored:
        strict_ok = _strict_key(item.predicted) == _strict_key(item.expected)
        same_ok = _same_person(item.predicted, item.expected)
        if strict_ok:
            strict_correct += 1
        if same_ok:
            same_person_correct += 1
        if not strict_ok and same_ok:
            strict_only_mismatches.append(item)
        if not same_ok:
            confusion[(item.expected, item.predicted)] += 1
            same_person_mismatches.append(item)

    strict_accuracy = strict_correct / matched if matched else 0
    same_person_accuracy = same_person_correct / matched if matched else 0
    speaker_counts = Counter(item.expected for item in scored)
    max_share = max(speaker_counts.values(), default=0) / matched if matched else 0
    usage = meta.get("usage", {}) if isinstance(meta.get("usage"), dict) else {}
    lines = [
        "# Speaker Naming Eval - 2025-04-23 Transportation",
        "",
        f"- References parsed: {len(references)}",
        f"- Matched by time (+/- {MATCH_TOLERANCE_SEC:.0f}s): {len(raw_matches)}",
        f"- Scored after de-skew: {matched}",
        f"- Largest speaker share after de-skew: {max_share:.1%}",
        f"- Same-person accuracy (headline): {same_person_accuracy:.1%} ({same_person_correct}/{matched})",
        f"- Strict spelling accuracy: {strict_accuracy:.1%} ({strict_correct}/{matched})",
        f"- Strict spelling misses that are same-person matches: {len(strict_only_mismatches)}",
        f"- Unmatched references: {len(misses)}",
        f"- Naming mode: {meta.get('mode', 'unknown')}",
        f"- Chunks: {meta.get('chunks', 'unknown')}",
        f"- Gemini tokens: prompt={usage.get('promptTokenCount', 'n/a')}, output={usage.get('candidatesTokenCount', 'n/a')}, thoughts={usage.get('thoughtsTokenCount', 'n/a')}, total={usage.get('totalTokenCount', 'n/a')}",
        f"- Estimated Gemini cost: ${float(meta.get('estimated_cost_usd') or 0):.4f}",
        "",
        "## Verification Corrections",
        "",
    ]
    verification = meta.get("verification", {}) if isinstance(meta.get("verification"), dict) else {}
    applied = verification.get("applied_corrections", [])
    if isinstance(applied, list) and applied:
        lines.append("| before | after | confidence | evidence |")
        lines.append("|---|---|---|---|")
        for item in applied:
            if not isinstance(item, dict):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        _md(str(item.get("before") or "")),
                        _md(str(item.get("after") or "")),
                        _md(str(item.get("confidence") or "")),
                        _md(str(item.get("evidence") or "")[:140]),
                    ]
                )
                + " |"
            )
    else:
        lines.append("No high-confidence verification corrections were applied.")

    lines.extend(
        [
            "",
            "## Speaker Distribution",
            "",
            "| speaker | scored references | share |",
            "|---|---:|---:|",
        ]
    )
    for speaker, count in speaker_counts.most_common():
        share = f"{count / matched:.1%}" if matched else "n/a"
        lines.append(f"| {_md(speaker)} | {count} | {share} |")

    lines.extend(
        [
            "",
            "## Confusion List",
            "",
        ]
    )
    if confusion:
        lines.append("| expected | predicted | count |")
        lines.append("|---|---|---:|")
        for (expected, predicted), count in confusion.most_common(30):
            lines.append(f"| {expected} | {predicted} | {count} |")
    else:
        lines.append("No speaker confusions among matched references.")

    if same_person_mismatches:
        lines.extend(["", "## Same-Person Mismatch Sample", ""])
        lines.append("| seek | expected | predicted | reference text | matched text |")
        lines.append("|---:|---|---|---|---|")
        for item in same_person_mismatches[:20]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"{item.ref.seek:.3f}",
                        _md(item.expected),
                        _md(item.predicted),
                        _md(item.ref.text[:100]),
                        _md(item.matched_text[:100]),
                    ]
                )
                + " |"
            )

    if strict_only_mismatches:
        lines.extend(["", "## Strict-Only Spelling Miss Sample", ""])
        lines.append("| seek | expected | predicted |")
        lines.append("|---:|---|---|")
        for item in strict_only_mismatches[:20]:
            lines.append(f"| {item.ref.seek:.3f} | {_md(item.expected)} | {_md(item.predicted)} |")

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


def _deskew_matches(matches: list[MatchedUtterance]) -> list[MatchedUtterance]:
    selected = list(matches)
    while selected:
        counts = Counter(item.expected for item in selected)
        speaker, count = counts.most_common(1)[0]
        total = len(selected)
        if count <= total * 0.25:
            break
        other_count = total - count
        if other_count <= 0:
            break
        target = max(1, other_count // 3)
        target = min(target, count - 1)
        positions = [index for index, item in enumerate(selected) if item.expected == speaker]
        keep = set(_evenly_spaced(positions, target))
        selected = [
            item
            for index, item in enumerate(selected)
            if item.expected != speaker or index in keep
        ]
    return selected


def _evenly_spaced(values: list[int], count: int) -> list[int]:
    if count >= len(values):
        return values
    if count <= 1:
        return [values[len(values) // 2]]
    return [values[round(i * (len(values) - 1) / (count - 1))] for i in range(count)]


def _display_speaker(value: str) -> str:
    value = re.sub(r"^Council\s+Member\s+", "", value.strip(), flags=re.I)
    value = re.sub(r"^Member\s+of\s+the\s+Public\s*-\s*", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value)


def _strict_key(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _display_speaker(value).lower()))


def _same_person(left: str, right: str) -> bool:
    left_key = _strict_key(left)
    right_key = _strict_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    left_parts = left_key.split()
    right_parts = right_key.split()
    if len(left_parts) >= 2 and len(right_parts) >= 2 and left_parts[-1] == right_parts[-1]:
        left_first = _canonical_first(left_parts[0])
        right_first = _canonical_first(right_parts[0])
        if left_first == right_first or _edit_distance(left_first, right_first) <= 2:
            return True
    return SequenceMatcher(None, left_key, right_key).ratio() >= 0.9


def _canonical_first(value: str) -> str:
    return NICKNAME_CANONICAL_FIRST_NAMES.get(value.lower(), value).lower()


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (0 if left_char == right_char else 1),
                )
            )
        previous = current
    return previous[-1]


def _normalize_speaker(value: str) -> str:
    value = _display_speaker(value)
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
