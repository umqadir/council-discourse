"""Compare generated meeting chapters against citymeetings.nyc references.

Usage:
  python experiments/05_compare_chapters.py 2025-04-23-transportation
  python experiments/05_compare_chapters.py 2025-04-23-transportation --model gemini-3.5-flash

Output:
  data/benchmark/{slug}/comparison-report.md
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "benchmark"
BOUNDARY_TOLERANCES = (15, 30, 60)
TIMESTAMP_MATCH_WINDOW_SEC = 120
MAX_TIMESTAMP_EXAMPLES = 8
STOPWORDS = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "have",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
    "you",
    "your",
}


@dataclass
class Chapter:
    index: int
    source: str
    chapter_id: str | None
    start_sec: float
    chapter_type: str
    title: str
    summary: str
    duration_sec: float | None = None
    end_sec: float | None = None


@dataclass
class Alignment:
    ref: Chapter
    gen: Chapter | None
    overlap_sec: float
    start_delta_sec: float | None


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    replacements = {
        "\u00a0": " ",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2022": "-",
        "\u2026": "...",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return " ".join(text.split())


def md(text: object) -> str:
    value = clean_text(str(text))
    return value.replace("|", "\\|")


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = [
        "| " + " | ".join(md(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md(cell) for cell in row) + " |")
    return "\n".join(lines)


def parse_clock(text: str | None) -> float | None:
    if not text:
        return None
    value = clean_text(text)
    match = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", value)
    if not match:
        return None
    parts = [float(part) for part in match.group(1).split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    return None


def parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    value = clean_text(text).lower()
    total = 0.0
    matched = False
    for amount, unit in re.findall(
        r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|hr|minutes?|mins?|min|seconds?|secs?|sec)\b",
        value,
    ):
        matched = True
        n = float(amount)
        if unit.startswith(("hour", "hr")):
            total += n * 3600
        elif unit.startswith(("minute", "min")):
            total += n * 60
        else:
            total += n
    if matched:
        return total
    return parse_clock(value)


def format_clock(seconds: float | None) -> str:
    if seconds is None or math.isnan(seconds):
        return "n/a"
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02}:{secs:02}"


def format_duration(seconds: float | None) -> str:
    if seconds is None or math.isnan(seconds):
        return "n/a"
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes}:{secs:02}"


def format_delta(seconds: float | None) -> str:
    if seconds is None or math.isnan(seconds):
        return "n/a"
    sign = "+" if seconds >= 0 else "-"
    return sign + format_duration(abs(seconds))


def format_number(value: float | None, digits: int = 1) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value:.{digits}f}"


def format_percent(value: float | None) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def reference_fallback_by_id(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = json.loads(path.read_text())
    return {str(row.get("chapter_id")): row for row in rows}


def choose_summary(card) -> str:
    summary = card.select_one("p.text-gray-600")
    if summary:
        return clean_text(summary.get_text(" ", strip=True))
    for p in card.find_all("p"):
        text = clean_text(p.get_text(" ", strip=True))
        if not text or text == "-":
            continue
        if parse_clock(text) is not None:
            continue
        if parse_duration(text) is not None:
            continue
        return text
    return ""


def parse_reference_from_html(slug_dir: Path) -> list[Chapter]:
    html_path = slug_dir / "citymeetings-meeting-page.html"
    fallback = reference_fallback_by_id(slug_dir / "citymeetings-chapters.json")
    if not html_path.exists():
        return parse_reference_from_json(slug_dir / "citymeetings-chapters.json")

    soup = BeautifulSoup(html_path.read_text(), "html.parser")
    chapters: list[Chapter] = []
    seen: set[str] = set()
    for card in soup.select("a[data-chapter-id]"):
        chapter_id = str(card.get("data-chapter-id", "")).strip()
        if not chapter_id or chapter_id in seen:
            continue
        seen.add(chapter_id)
        fallback_row = fallback.get(chapter_id, {})
        badge_el = card.select_one("span")
        title_el = card.find(["h3", "h2"])
        p_texts = [clean_text(p.get_text(" ", strip=True)) for p in card.find_all("p")]
        start_sec = next((parse_clock(text) for text in p_texts if parse_clock(text) is not None), None)
        duration_sec = next(
            (parse_duration(text) for text in reversed(p_texts) if parse_duration(text) is not None),
            None,
        )
        if start_sec is None:
            start_sec = parse_clock(fallback_row.get("start_ts"))
        if start_sec is None:
            raise ValueError(f"Reference chapter {chapter_id} has no parseable start timestamp")
        chapters.append(
            Chapter(
                index=len(chapters) + 1,
                source="reference",
                chapter_id=chapter_id,
                start_sec=start_sec,
                chapter_type=clean_text(badge_el.get_text(" ", strip=True) if badge_el else fallback_row.get("badge")),
                title=clean_text(title_el.get_text(" ", strip=True) if title_el else ""),
                summary=choose_summary(card),
                duration_sec=duration_sec,
            )
        )
    if not chapters:
        return parse_reference_from_json(slug_dir / "citymeetings-chapters.json")
    return chapters


def title_from_url(url: str | None) -> str:
    if not url:
        return ""
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()


def parse_reference_from_json(path: Path) -> list[Chapter]:
    rows = json.loads(path.read_text())
    chapters = []
    for row in rows:
        start_sec = parse_clock(row.get("start_ts") or row.get("card_text"))
        if start_sec is None:
            raise ValueError(f"Reference chapter {row.get('chapter_id')} has no parseable start timestamp")
        duration_sec = None
        text = clean_text(row.get("card_text"))
        duration_match = re.search(r"\b\d+(?:\.\d+)?\s*(?:sec|secs|seconds|min|mins|minutes)\b", text, re.I)
        if duration_match:
            duration_sec = parse_duration(duration_match.group(0))
        chapters.append(
            Chapter(
                index=len(chapters) + 1,
                source="reference",
                chapter_id=str(row.get("chapter_id") or ""),
                start_sec=start_sec,
                chapter_type=clean_text(row.get("badge")),
                title=title_from_url(row.get("url")),
                summary="",
                duration_sec=duration_sec,
            )
        )
    return chapters


def load_generated(path: Path) -> tuple[str, list[Chapter]]:
    data = json.loads(path.read_text())
    model = clean_text(data.get("model")) or path.stem.removeprefix("chapters-")
    chapters = []
    for row in data.get("chapters", []):
        start_sec = parse_clock(row.get("start"))
        if start_sec is None:
            raise ValueError(f"{path.name} chapter {len(chapters) + 1} has no parseable start")
        duration_sec = None
        for key in ("duration_sec", "duration", "duration_text"):
            if key in row:
                duration_sec = parse_duration(str(row[key]))
                break
        chapters.append(
            Chapter(
                index=len(chapters) + 1,
                source=model,
                chapter_id=None,
                start_sec=start_sec,
                chapter_type=clean_text(row.get("type")),
                title=clean_text(row.get("title")),
                summary=clean_text(row.get("summary")),
                duration_sec=duration_sec,
            )
        )
    return model, chapters


def load_captions(path: Path) -> list[dict]:
    captions = []
    if not path.exists():
        return captions
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "t" in row and "text" in row:
            captions.append({"t": float(row["t"]), "text": clean_text(row["text"])})
    return captions


def infer_ends(chapters: list[Chapter], meeting_end_sec: float | None, last_uses_duration: bool) -> list[Chapter]:
    ordered = sorted(chapters, key=lambda ch: (ch.start_sec, ch.index))
    for i, chapter in enumerate(ordered):
        next_start = ordered[i + 1].start_sec if i + 1 < len(ordered) else None
        if next_start is not None and next_start > chapter.start_sec:
            chapter.end_sec = next_start
        elif chapter.duration_sec is not None:
            chapter.end_sec = chapter.start_sec + chapter.duration_sec
        elif i + 1 == len(ordered) and last_uses_duration and chapter.duration_sec is not None:
            chapter.end_sec = chapter.start_sec + chapter.duration_sec
        elif i + 1 == len(ordered) and meeting_end_sec is not None:
            chapter.end_sec = meeting_end_sec
        else:
            chapter.end_sec = chapter.start_sec
        if chapter.end_sec < chapter.start_sec:
            chapter.end_sec = chapter.start_sec

    if ordered and last_uses_duration and ordered[-1].duration_sec is not None:
        ordered[-1].end_sec = ordered[-1].start_sec + ordered[-1].duration_sec
    return ordered


def chapter_durations(chapters: list[Chapter], prefer_parsed: bool) -> list[float]:
    durations = []
    for chapter in chapters:
        if prefer_parsed and chapter.duration_sec is not None:
            durations.append(chapter.duration_sec)
        elif chapter.end_sec is not None:
            durations.append(max(0.0, chapter.end_sec - chapter.start_sec))
    return durations


def distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p10": None, "median": None, "mean": None, "p90": None, "max": None}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p10": percentile(ordered, 10),
        "median": median(ordered),
        "mean": sum(ordered) / len(ordered),
        "p90": percentile(ordered, 90),
        "max": ordered[-1],
    }


def percentile(ordered_values: list[float], pct: float) -> float:
    if not ordered_values:
        return math.nan
    if len(ordered_values) == 1:
        return ordered_values[0]
    pos = (len(ordered_values) - 1) * pct / 100
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered_values[int(pos)]
    weight = pos - lower
    return ordered_values[lower] * (1 - weight) + ordered_values[upper] * weight


def coverage(chapters: list[Chapter], meeting_start_sec: float, meeting_end_sec: float) -> dict[str, float | None]:
    clipped = []
    for chapter in chapters:
        if chapter.end_sec is None:
            continue
        start = max(meeting_start_sec, chapter.start_sec)
        end = min(meeting_end_sec, chapter.end_sec)
        if end > start:
            clipped.append((start, end))
    if not clipped:
        return {"covered_sec": 0.0, "coverage_pct": 0.0, "first_start": None, "last_end": None}

    clipped.sort()
    covered = 0.0
    cur_start, cur_end = clipped[0]
    for start, end in clipped[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            covered += cur_end - cur_start
            cur_start, cur_end = start, end
    covered += cur_end - cur_start
    span = max(1.0, meeting_end_sec - meeting_start_sec)
    return {
        "covered_sec": covered,
        "coverage_pct": covered / span,
        "first_start": min(ch.start_sec for ch in chapters),
        "last_end": max(ch.end_sec for ch in chapters if ch.end_sec is not None),
    }


def sequence_checks(chapters: list[Chapter], meeting_start_sec: float, meeting_end_sec: float) -> dict[str, object]:
    starts = [ch.start_sec for ch in chapters]
    non_monotonic = []
    duplicate_starts = []
    for i in range(1, len(starts)):
        if starts[i] < starts[i - 1]:
            non_monotonic.append((i + 1, starts[i - 1], starts[i]))
        if starts[i] == starts[i - 1]:
            duplicate_starts.append((i + 1, starts[i]))
    explicit_ends = all(ch.duration_sec is not None for ch in chapters)
    leading_uncovered = max(0.0, min(starts) - meeting_start_sec) if starts else None
    last_end = max((ch.end_sec for ch in chapters if ch.end_sec is not None), default=None)
    trailing_uncovered = max(0.0, meeting_end_sec - last_end) if last_end is not None else None
    return {
        "non_monotonic": non_monotonic,
        "duplicate_starts": duplicate_starts,
        "explicit_ends": explicit_ends,
        "leading_uncovered": leading_uncovered,
        "trailing_uncovered": trailing_uncovered,
    }


def boundary_metrics(reference: list[Chapter], generated: list[Chapter]) -> dict[int, dict[str, float]]:
    ref_starts = [ch.start_sec for ch in reference]
    gen_starts = [ch.start_sec for ch in generated]
    results = {}
    for tolerance in BOUNDARY_TOLERANCES:
        candidates = []
        for gen_i, gen_start in enumerate(gen_starts):
            for ref_i, ref_start in enumerate(ref_starts):
                delta = gen_start - ref_start
                if abs(delta) <= tolerance:
                    candidates.append((abs(delta), gen_i, ref_i, delta))
        candidates.sort()
        matched_gen = set()
        matched_ref = set()
        deltas = []
        for abs_delta, gen_i, ref_i, delta in candidates:
            if gen_i in matched_gen or ref_i in matched_ref:
                continue
            matched_gen.add(gen_i)
            matched_ref.add(ref_i)
            deltas.append(abs_delta)
        tp = len(deltas)
        precision = tp / len(gen_starts) if gen_starts else 0.0
        recall = tp / len(ref_starts) if ref_starts else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        results[tolerance] = {
            "matched": tp,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "median_abs_delta": median(deltas) if deltas else math.nan,
        }
    return results


def interval_overlap(a: Chapter, b: Chapter) -> float:
    if a.end_sec is None or b.end_sec is None:
        return 0.0
    return max(0.0, min(a.end_sec, b.end_sec) - max(a.start_sec, b.start_sec))


def align_chapters(reference: list[Chapter], generated: list[Chapter]) -> list[Alignment]:
    alignments = []
    for ref in sorted(reference, key=lambda ch: (ch.start_sec, ch.index)):
        best = None
        best_key = None
        for gen in generated:
            overlap = interval_overlap(ref, gen)
            delta = gen.start_sec - ref.start_sec
            key = (overlap, -abs(delta), -gen.index)
            if best_key is None or key > best_key:
                best = gen
                best_key = key
        if best is None:
            alignments.append(Alignment(ref=ref, gen=None, overlap_sec=0.0, start_delta_sec=None))
        else:
            alignments.append(
                Alignment(
                    ref=ref,
                    gen=best,
                    overlap_sec=interval_overlap(ref, best),
                    start_delta_sec=best.start_sec - ref.start_sec,
                )
            )
    return alignments


def type_confusion(alignments: list[Alignment]) -> tuple[list[str], list[str], dict[tuple[str, str], int]]:
    ref_types = sorted({a.ref.chapter_type or "UNKNOWN" for a in alignments if a.gen and a.overlap_sec > 0})
    gen_types = sorted({a.gen.chapter_type or "UNKNOWN" for a in alignments if a.gen and a.overlap_sec > 0})
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for alignment in alignments:
        if not alignment.gen or alignment.overlap_sec <= 0:
            continue
        counts[(alignment.ref.chapter_type or "UNKNOWN", alignment.gen.chapter_type or "UNKNOWN")] += 1
    return ref_types, gen_types, counts


def normalize_type_label(label: str) -> str:
    value = clean_text(label).upper().replace("&", "AND")
    value = re.sub(r"[^A-Z0-9]+", "_", value).strip("_")
    aliases = {
        "PUBLIC_TESTIMONY": "TESTIMONY",
        "Q_A": "QA",
        "AGENCY_TESTIMONY": "AGENCY_TESTIMONY",
        "REMARKS": "REMARKS",
        "PROCEDURE": "PROCEDURE",
        "VOTE": "VOTE",
    }
    return aliases.get(value, value or "UNKNOWN")


def normalized_type_agreement(alignments: list[Alignment]) -> float | None:
    total = 0
    agree = 0
    for alignment in alignments:
        if not alignment.gen or alignment.overlap_sec <= 0:
            continue
        total += 1
        if normalize_type_label(alignment.ref.chapter_type) == normalize_type_label(alignment.gen.chapter_type):
            agree += 1
    return agree / total if total else None


def normalize_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", clean_text(text).lower())


def content_words(words: list[str]) -> set[str]:
    return {word for word in words if len(word) >= 4 and word not in STOPWORDS}


def extract_sample_utterances(slug_dir: Path, reference: list[Chapter]) -> list[dict]:
    samples_path = slug_dir / "citymeetings-chapter-samples.json"
    if not samples_path.exists():
        return []
    samples = json.loads(samples_path.read_text())
    url_to_id = {
        row.get("url"): str(row.get("chapter_id"))
        for row in json.loads((slug_dir / "citymeetings-chapters.json").read_text())
    }
    id_to_title = {chapter.chapter_id: chapter.title for chapter in reference}
    utterances = []
    for sample in samples:
        chapter_id = url_to_id.get(sample.get("url"))
        if not chapter_id:
            continue
        html_path = slug_dir / f"citymeetings-chapter-{chapter_id}.html"
        if not html_path.exists():
            continue
        soup = BeautifulSoup(html_path.read_text(), "html.parser")
        page_utterances = []
        for time_el in soup.find_all(attrs={"@click": re.compile(r"seekTo")}):
            click = time_el.get("@click", "")
            match = re.search(r"seekTo\(([\d.]+)\)", click)
            if not match:
                continue
            parent = time_el.parent
            sentence_el = parent.find(class_="sentence") if parent else None
            sentence = clean_text(sentence_el.get_text(" ", strip=True) if sentence_el else "")
            if len(normalize_words(sentence)) < 5:
                continue
            page_utterances.append(
                {
                    "chapter_id": chapter_id,
                    "chapter_title": id_to_title.get(chapter_id, clean_text(sample.get("title"))),
                    "seek": float(match.group(1)),
                    "sentence": sentence,
                }
            )
        if not page_utterances:
            continue
        step = max(1, len(page_utterances) // 8)
        utterances.extend(page_utterances[::step][:8])
    return utterances


def best_caption_match(utterance: dict, captions: list[dict]) -> dict | None:
    utter_words = normalize_words(utterance["sentence"])
    utter_word_set = set(utter_words)
    utter_content = content_words(utter_words)
    if not utter_content:
        return None
    candidates = []
    seek = utterance["seek"]
    for caption in captions:
        delta = caption["t"] - seek
        if abs(delta) > TIMESTAMP_MATCH_WINDOW_SEC:
            continue
        cap_words = normalize_words(caption["text"])
        if len(cap_words) < 3:
            continue
        cap_content = content_words(cap_words)
        content_overlap = cap_content & utter_content
        if len(content_overlap) < 2:
            continue
        cap_overlap = sum(1 for word in cap_words if word in utter_word_set) / len(cap_words)
        utter_overlap = sum(1 for word in utter_words if word in set(cap_words)) / len(utter_words)
        score = 0.8 * cap_overlap + 0.2 * utter_overlap
        if score >= 0.55:
            candidates.append((abs(delta), -score, caption, score, delta))
    if not candidates:
        return None
    candidates.sort()
    _, neg_score, caption, score, delta = candidates[0]
    return {
        "caption_t": caption["t"],
        "caption_text": caption["text"],
        "offset_sec": delta,
        "score": -neg_score if neg_score else score,
    }


def timestamp_sanity(slug_dir: Path, reference: list[Chapter], captions: list[dict]) -> dict:
    utterances = extract_sample_utterances(slug_dir, reference)
    matches = []
    for utterance in utterances:
        match = best_caption_match(utterance, captions)
        if not match:
            continue
        matches.append({**utterance, **match})
    offsets = [match["offset_sec"] for match in matches]
    return {
        "sampled": len(utterances),
        "matched": len(matches),
        "median_offset": median(offsets) if offsets else math.nan,
        "p10_offset": percentile(sorted(offsets), 10) if offsets else math.nan,
        "p90_offset": percentile(sorted(offsets), 90) if offsets else math.nan,
        "examples": matches[:MAX_TIMESTAMP_EXAMPLES],
    }


def model_files(slug_dir: Path, requested_models: list[str]) -> list[Path]:
    if not requested_models:
        return sorted(slug_dir.glob("chapters-*.json"))
    files = []
    for model in requested_models:
        candidate = slug_dir / f"chapters-{model}.json"
        if candidate.exists():
            files.append(candidate)
            continue
        path = Path(model)
        if path.exists():
            files.append(path)
            continue
        raise FileNotFoundError(f"No generated chapters file found for model {model!r}")
    return files


def build_alignment_table(alignments: list[Alignment]) -> str:
    rows = []
    for alignment in alignments:
        ref = alignment.ref
        gen = alignment.gen
        rows.append(
            [
                ref.index,
                ref.chapter_id or "",
                format_clock(ref.start_sec),
                format_delta(alignment.start_delta_sec),
                format_duration(alignment.overlap_sec),
                ref.chapter_type,
                gen.chapter_type if gen else "UNMATCHED",
                f"{gen.index}: {format_clock(gen.start_sec)}" if gen else "n/a",
                ref.title,
                gen.title if gen else "",
            ]
        )
    return markdown_table(
        [
            "#",
            "ref id",
            "ref start",
            "our start delta",
            "overlap",
            "ref type",
            "our type",
            "our chapter",
            "reference title",
            "generated title",
        ],
        rows,
    )


def build_boundary_table(metrics: dict[int, dict[str, float]]) -> str:
    rows = []
    for tolerance in BOUNDARY_TOLERANCES:
        row = metrics[tolerance]
        rows.append(
            [
                f"{tolerance}s",
                int(row["matched"]),
                format_percent(row["precision"]),
                format_percent(row["recall"]),
                format_percent(row["f1"]),
                format_duration(row["median_abs_delta"]),
            ]
        )
    return markdown_table(["tolerance", "matched starts", "precision", "recall", "F1", "median abs delta"], rows)


def build_duration_table(reference: list[Chapter], generated: list[Chapter]) -> str:
    ref_dist = distribution(chapter_durations(reference, prefer_parsed=True))
    gen_dist = distribution(chapter_durations(generated, prefer_parsed=False))
    rows = []
    for label, chapters, dist in [("reference", reference, ref_dist), ("generated", generated, gen_dist)]:
        rows.append(
            [
                label,
                len(chapters),
                format_duration(dist["min"]),
                format_duration(dist["p10"]),
                format_duration(dist["median"]),
                format_duration(dist["mean"]),
                format_duration(dist["p90"]),
                format_duration(dist["max"]),
            ]
        )
    return markdown_table(["set", "chapters", "min", "p10", "median", "mean", "p90", "max"], rows)


def build_coverage_table(
    reference: list[Chapter],
    generated: list[Chapter],
    meeting_start_sec: float,
    meeting_end_sec: float,
) -> str:
    ref_coverage = coverage(reference, meeting_start_sec, meeting_end_sec)
    gen_coverage = coverage(generated, meeting_start_sec, meeting_end_sec)
    rows = []
    for label, cov in [("reference", ref_coverage), ("generated", gen_coverage)]:
        rows.append(
            [
                label,
                format_clock(cov["first_start"]),
                format_clock(cov["last_end"]),
                format_duration(cov["covered_sec"]),
                format_percent(cov["coverage_pct"]),
            ]
        )
    return markdown_table(["set", "first start", "last inferred end", "covered span", "coverage"], rows)


def build_sequence_table(checks: dict[str, object]) -> str:
    internal_gap_status = (
        "not directly observable: generated chapters have starts but no explicit ends"
        if not checks["explicit_ends"]
        else "checked from explicit durations"
    )
    rows = [
        ["non-monotonic starts", len(checks["non_monotonic"])],
        ["duplicate starts", len(checks["duplicate_starts"])],
        ["internal gaps/overlaps", internal_gap_status],
        ["leading uncovered span", format_duration(checks["leading_uncovered"])],
        ["trailing uncovered span", format_duration(checks["trailing_uncovered"])],
    ]
    return markdown_table(["check", "result"], rows)


def build_type_confusion_table(alignments: list[Alignment]) -> tuple[str, float | None]:
    ref_types, gen_types, counts = type_confusion(alignments)
    if not ref_types or not gen_types:
        return "No overlapping aligned pairs.", None
    rows = []
    agree = 0
    total = 0
    for ref_type in ref_types:
        row = [ref_type]
        for gen_type in gen_types:
            count = counts[(ref_type, gen_type)]
            row.append(count)
            total += count
            if ref_type == gen_type:
                agree += count
        rows.append(row)
    agreement = agree / total if total else None
    return markdown_table(["ref \\ our", *gen_types], rows), agreement


def truncate(text: str, limit: int = 100) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def build_timestamp_section(sanity: dict) -> str:
    lines = [
        "## Timestamp sanity check",
        "",
        (
            "Offset is `caption_t - citymeetings_seekTo_sec`; values near zero mean the saved "
            "citymeetings chapter pages and `captions-clean.jsonl` use the same video clock."
        ),
        "",
        markdown_table(
            ["sampled utterances", "matched", "median offset", "p10 offset", "p90 offset"],
            [
                [
                    sanity["sampled"],
                    sanity["matched"],
                    format_number(sanity["median_offset"], 2) + "s",
                    format_number(sanity["p10_offset"], 2) + "s",
                    format_number(sanity["p90_offset"], 2) + "s",
                ]
            ],
        ),
    ]
    if sanity["examples"]:
        lines.extend(
            [
                "",
                markdown_table(
                    ["chapter", "seek", "caption t", "offset", "score", "citymeetings text", "caption fragment"],
                    [
                        [
                            truncate(example["chapter_title"], 55),
                            format_clock(example["seek"]),
                            format_clock(example["caption_t"]),
                            format_number(example["offset_sec"], 2) + "s",
                            format_number(example["score"], 2),
                            truncate(example["sentence"], 90),
                            truncate(example["caption_text"], 70),
                        ]
                        for example in sanity["examples"]
                    ],
                ),
            ]
        )
    return "\n".join(lines)


def build_report(slug: str, slug_dir: Path, requested_models: list[str]) -> str:
    reference = parse_reference_from_html(slug_dir)
    if any(not chapter.title for chapter in reference):
        empty_ids = [chapter.chapter_id for chapter in reference if not chapter.title]
        raise ValueError(f"Reference parser produced empty titles for chapters: {empty_ids[:10]}")

    captions = load_captions(slug_dir / "captions-clean.jsonl")
    if captions:
        meeting_start_sec = min(row["t"] for row in captions)
        meeting_end_sec = max(row["t"] for row in captions)
    else:
        meeting_start_sec = min(ch.start_sec for ch in reference)
        meeting_end_sec = max(ch.start_sec + (ch.duration_sec or 0.0) for ch in reference)

    infer_ends(reference, meeting_end_sec, last_uses_duration=True)
    model_results = []
    for path in model_files(slug_dir, requested_models):
        model, generated = load_generated(path)
        infer_ends(generated, meeting_end_sec, last_uses_duration=False)
        boundaries = boundary_metrics(reference, generated)
        alignments = align_chapters(reference, generated)
        confusion_table, _ = build_type_confusion_table(alignments)
        model_results.append(
            {
                "model": model,
                "path": path,
                "generated": generated,
                "boundaries": boundaries,
                "alignments": alignments,
                "type_confusion": confusion_table,
                "type_agreement": normalized_type_agreement(alignments),
                "sequence": sequence_checks(generated, meeting_start_sec, meeting_end_sec),
                "coverage": coverage(generated, meeting_start_sec, meeting_end_sec),
                "durations": distribution(chapter_durations(generated, prefer_parsed=False)),
            }
        )

    ref_dist = distribution(chapter_durations(reference, prefer_parsed=True))
    summary_rows = []
    for result in model_results:
        sequence = result["sequence"]
        summary_rows.append(
            [
                result["model"],
                len(result["generated"]),
                len(reference),
                format_duration(result["durations"]["median"]),
                format_duration(ref_dist["median"]),
                format_percent(result["coverage"]["coverage_pct"]),
                format_percent(result["boundaries"][15]["f1"]),
                format_percent(result["boundaries"][30]["f1"]),
                format_percent(result["boundaries"][60]["f1"]),
                format_percent(result["type_agreement"]),
                len(sequence["non_monotonic"]),
            ]
        )

    lines = [
        f"# Chapter comparison report: {slug}",
        "",
        f"Meeting span from captions: {format_clock(meeting_start_sec)} to {format_clock(meeting_end_sec)} ({format_duration(meeting_end_sec - meeting_start_sec)}).",
        f"Reference chapters parsed from saved citymeetings meeting HTML: {len(reference)}.",
        "",
        "## Metrics summary",
        "",
        markdown_table(
            [
                "model",
                "generated chapters",
                "reference chapters",
                "generated median duration",
                "reference median duration",
                "generated coverage",
                "F1 @15s",
                "F1 @30s",
                "F1 @60s",
                "normalized type agreement",
                "non-monotonic starts",
            ],
            summary_rows,
        ),
    ]

    for result in model_results:
        model = result["model"]
        generated = result["generated"]
        lines.extend(
            [
                "",
                f"## {model}",
                "",
                "### Alignment table",
                "",
                build_alignment_table(result["alignments"]),
                "",
                "### Boundary agreement",
                "",
                build_boundary_table(result["boundaries"]),
                "",
                "### Counts, duration, and coverage",
                "",
                build_duration_table(reference, generated),
                "",
                build_coverage_table(reference, generated, meeting_start_sec, meeting_end_sec),
                "",
                build_sequence_table(result["sequence"]),
                "",
                "### Type confusion",
                "",
                result["type_confusion"],
            ]
        )

    lines.extend(["", build_timestamp_section(timestamp_sanity(slug_dir, reference, captions)), ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Benchmark meeting slug under data/benchmark/")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Model name without chapters- prefix. May be repeated. Defaults to all chapters-*.json files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Report path. Defaults to data/benchmark/{slug}/comparison-report.md.",
    )
    args = parser.parse_args()

    slug_dir = DATA / args.slug
    if not slug_dir.exists():
        raise FileNotFoundError(f"Benchmark directory not found: {slug_dir}")
    report = build_report(args.slug, slug_dir, args.model)
    output = args.output or slug_dir / "comparison-report.md"
    output.write_text(report)
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
