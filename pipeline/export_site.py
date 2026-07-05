from __future__ import annotations

import json
import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import db
from .artifacts import clean_text, normalize_utterances, parse_clock, read_json, read_jsonl, round_sec
from .config import DATA_DIR, REGISTRY_DB, ROOT

SITE_DATA_DIR = ROOT / "site" / "src" / "data" / "meetings"
SITE_R2_DATA_DIR = ROOT / "site" / "r2-data"
R2_CHAPTER_DATA_PREFIX = "data/meetings"
BENCHMARK_DIR = DATA_DIR / "benchmark"
CHAPTER_MODEL = "gemini-3.5-flash"
COUNCIL_BODY = "New York City Council"
COUNCIL_LOCATION_MARKERS = (
    "council chambers",
    "committee room",
    "city hall",
    "250 broadway",
)

BENCHMARK_OVERRIDES = {
    "2025-04-23-transportation": {
        "body": "New York City Council",
        "title": "Committee on Transportation and Infrastructure",
        "slug": "2025-04-23-1000-am-committee-on-transportation-and-infrastructure",
        "tags": ["HEARING"],
        "summary": [
            "A joint hearing examined Dining Out NYC, the permanent outdoor dining program that replaced the temporary pandemic-era program.",
            "Discussion focused on application complexity, approval delays, restaurant costs, seasonal roadway dining rules, clearance requirements, and accessibility.",
            "The meeting includes opening remarks, DOT testimony, council member questioning, and public testimony from restaurant, disability, transportation, and neighborhood advocates.",
        ],
    },
    "2025-04-24-stated": {
        "body": "New York City Council",
        "title": "Stated Meeting",
        "slug": "2025-04-24-0130-pm-stated-meeting",
        "tags": ["STATED_MEETING", "VOTE", "LAND_USE"],
        "summary": [
            "The Council held a stated meeting to vote on legislation, introduce new bills, and handle land use and procedural business.",
            "Members approved measures including bills supporting transgender, gender non-conforming, and non-binary New Yorkers and measures regulating non-essential helicopter flights.",
            "The Council also considered land use items, member remarks, and introductions covering deed theft, stormwater management, public safety, and housing administration.",
        ],
    },
}

# ASR transcripts frequently split a decimal number into two "sentences",
# e.g. "$125. 8 billion" for "$125.8 billion" or "3. 5 percent" for "3.5
# percent". The reliable tell is a period + space between two digit runs where
# the trailing run is immediately followed by a scale/unit token (%, percent,
# billion, million, ...) or continues into another decimal fragment. Requiring
# that unit anchor deliberately leaves genuine sentence boundaries like
# "...held in 2012. 5 members voted no." untouched, since "members" is not a
# numeric unit.
_UNIT = r"(?:%|(?:percent|billion|million|thousand|trillion|bn|mm?)\b)"
# Innermost fragment: "<digit>. <digits><unit>" -> "<digit>.<digits><unit>".
_DECIMAL_UNIT = re.compile(rf"(\d)\.\s+(\d+\s*{_UNIT})", re.IGNORECASE)
# Chained fragment: "<digit>. <digits>." feeding into a fragment we already
# know terminates in a unit, so "$1. 234.5 billion" collapses left-to-right.
_DECIMAL_CHAIN = re.compile(r"(\d)\.\s+(\d+\.\d)")
# A stray space before the decimal point ("$125 .8 billion").
_DECIMAL_PRESPACE = re.compile(r"(\d)\s+\.(\d)")


def normalize_summary_text(value: str) -> str:
    """Repair ASR decimal-split artifacts in prose without altering wording."""
    text = str(value or "")
    # Run repeatedly so chained artifacts ("$1. 234. 5 billion") fully collapse.
    while True:
        repaired = _DECIMAL_PRESPACE.sub(r"\1.\2", text)
        repaired = _DECIMAL_UNIT.sub(r"\1.\2", repaired)
        repaired = _DECIMAL_CHAIN.sub(r"\1.\2", repaired)
        if repaired == text:
            break
        text = repaired
    return re.sub(r"[ \t]+", " ", text).strip()


def export_site(
    db_path: Path = REGISTRY_DB,
    out_dir: Path = SITE_DATA_DIR,
    r2_out_dir: Path = SITE_R2_DATA_DIR,
    include_benchmark: bool = True,
    allow_empty: bool = False,
) -> list[Path]:
    meetings = []
    if include_benchmark:
        meetings.extend(_benchmark_meetings())
    meetings.extend(_registry_meetings(db_path))

    if not meetings:
        if allow_empty:
            return sorted(out_dir.glob("*.json")) if out_dir.exists() else []
        raise RuntimeError("no completed meetings found to export")

    # Incremental export: meeting artifacts live only on the machine that
    # processed them, so regenerate the meetings we have artifacts for and
    # leave previously committed site JSONs in place for the rest.
    out_dir.mkdir(parents=True, exist_ok=True)
    r2_out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    seen: set[str] = set()
    for meeting in meetings:
        slug = meeting["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        text = _json_text(meeting)
        path = out_dir / f"{slug}.json"
        path.write_text(text)
        written.append(path)

        data_key = _chapter_data_key(slug, text)
        r2_path = r2_out_dir / data_key
        r2_path.parent.mkdir(parents=True, exist_ok=True)
        r2_path.write_text(text)
        written.append(r2_path)
    return written


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _chapter_data_key(slug: str, text: str) -> Path:
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return Path(R2_CHAPTER_DATA_PREFIX) / f"{slug}.{version}.json"


def _registry_meetings(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = db.connect(db_path)
    rows = conn.execute(
        """
        SELECT * FROM meetings
        WHERE transcribe_status = 'transcribed'
          AND name_speakers_status = 'named'
          AND chapterize_status = 'chapterized'
        ORDER BY COALESCE(event_date, viebit_pub_date, discovered_at), meeting_key
        """
    ).fetchall()
    out = []
    for row in rows:
        meeting = db.meeting_from_row(row)
        if not _has_export_artifacts(meeting.meeting_dir):
            # Processed on another machine; its committed site JSON stands.
            continue
        out.append(_convert_meeting(meeting, dict(row), None))
    return out


def _has_export_artifacts(meeting_dir: Path) -> bool:
    has_chapters = (meeting_dir / "chapters.json").exists() or (
        meeting_dir / f"chapters-{CHAPTER_MODEL}.json"
    ).exists()
    return has_chapters and _utterances_path(meeting_dir) is not None


def _benchmark_meetings() -> list[dict[str, Any]]:
    if not BENCHMARK_DIR.exists():
        return []
    out = []
    for meeting_dir in sorted(path for path in BENCHMARK_DIR.iterdir() if path.is_dir()):
        if not (meeting_dir / "meeting.json").exists():
            continue
        key = meeting_dir.name
        payload = read_json(meeting_dir / "meeting.json")
        from .models import Meeting

        out.append(
            _convert_meeting(
                Meeting(
                    meeting_key=key,
                    meeting_dir=meeting_dir,
                    legistar_event_id=payload.get("legistar_event_id"),
                    legistar_event_guid=payload.get("legistar_event_guid"),
                    viebit_filename=payload.get("viebit_filename") or payload.get("viebit_file"),
                    viebit_hash=payload.get("viebit_hash"),
                    body_name=payload.get("body_name") or payload.get("body"),
                    event_date=payload.get("event_date") or payload.get("date"),
                    event_time=payload.get("event_time") or payload.get("time"),
                    event_location=payload.get("event_location"),
                    duration_seconds=payload.get("duration_seconds") or payload.get("duration_sec"),
                    agenda_pdf_url=payload.get("agenda_pdf_url"),
                    minutes_pdf_url=payload.get("minutes_pdf_url"),
                    insite_url=payload.get("insite_url"),
                    event_video_path=payload.get("event_video_path"),
                    agenda_status_name=payload.get("agenda_status_name"),
                    minutes_status_name=payload.get("minutes_status_name"),
                    meeting_type=payload.get("meeting_type"),
                    meeting_slug=payload.get("meeting_slug") or payload.get("slug"),
                    video_web_url=payload.get("video_web_url"),
                ),
                payload,
                BENCHMARK_OVERRIDES.get(key),
            )
        )
    return out


def _convert_meeting(meeting, payload: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    chapters_result = _read_chapters(meeting.meeting_dir)
    derived = _read_derived(meeting.meeting_dir)
    utterances_path = _utterances_path(meeting.meeting_dir)
    utterances = normalize_utterances(read_jsonl(utterances_path)) if utterances_path else []
    date = str(meeting.event_date or payload.get("date") or "")[:10]
    time = str(meeting.event_time or payload.get("time") or "")
    title = override["title"] if override else _meeting_title(meeting, payload)
    body = override["body"] if override else _site_body(meeting, payload)
    slug = override["slug"] if override else _meeting_slug(date, time, title)
    duration = float(meeting.duration_seconds or payload.get("duration_sec") or _last_utterance_end(utterances))

    chapter_starts = [_chapter_start(chapter) for chapter in chapters_result["chapters"]]
    seen_slugs: dict[str, int] = {}
    chapters = []
    for index, chapter in enumerate(chapters_result["chapters"]):
        start_sec = chapter_starts[index]
        end_sec = float(chapter.get("end_sec") or (chapter_starts[index + 1] if index + 1 < len(chapter_starts) else duration))
        title_text = str(chapter.get("title") or f"Chapter {index + 1}")
        chapters.append(
            {
                "id": str(index + 1).zfill(3),
                "slug": _chapter_slug(title_text, index, seen_slugs),
                "type": str(chapter.get("type") or "REMARKS"),
                "title": title_text,
                "summary": normalize_summary_text(chapter.get("summary") or ""),
                "start_sec": round_sec(start_sec),
                "end_sec": round_sec(max(start_sec + 1, end_sec)),
                "utterances": _utterances_for_chapter(utterances, utterances_path, start_sec, end_sec),
            }
        )

    return {
        "slug": slug,
        "body": body,
        "title": title,
        "date": date,
        "time": time,
        "duration_sec": round_sec(duration),
        "video": {
            "url": _video_url(meeting, payload),
            "poster": "/og-placeholder.svg",
        },
        "summary": _summary(derived, override),
        "tags": _tags(derived, override),
        "chapters": chapters,
    }


def _read_chapters(meeting_dir: Path) -> dict[str, Any]:
    preferred = meeting_dir / "chapters.json"
    if preferred.exists():
        return read_json(preferred)
    fallback = meeting_dir / f"chapters-{CHAPTER_MODEL}.json"
    if fallback.exists():
        return read_json(fallback)
    raise RuntimeError(f"missing chapters.json in {meeting_dir}")


def _read_derived(meeting_dir: Path) -> dict[str, Any]:
    path = meeting_dir / "meeting-derived.json"
    return read_json(path) if path.exists() else {}


def _utterances_path(meeting_dir: Path) -> Path | None:
    for name in ("utterances-named.jsonl", "utterances.jsonl", "captions-clean.jsonl"):
        path = meeting_dir / name
        if path.exists():
            return path
    return None


def _chapter_start(chapter: dict[str, Any]) -> float:
    if chapter.get("start_sec") is not None:
        return float(chapter["start_sec"])
    return parse_clock(str(chapter["start"]))


def _utterances_for_chapter(
    utterances: list[dict[str, Any]],
    path: Path | None,
    start_sec: float,
    end_sec: float,
) -> list[dict[str, Any]]:
    if path and path.name == "captions-clean.jsonl" and utterances and "speaker" not in utterances[0]:
        return _caption_utterances_for_chapter(utterances, start_sec, end_sec)
    rows = []
    for row in utterances:
        t0 = float(row["t0"])
        if start_sec <= t0 < end_sec:
            text = clean_text(row.get("text"))
            if text:
                rows.append(
                    {
                        "t_sec": round_sec(t0),
                        "speaker": str(row.get("speaker") or "Speaker"),
                        "text": text,
                    }
                )
    return rows


def _caption_utterances_for_chapter(
    captions: list[dict[str, Any]],
    start_sec: float,
    end_sec: float,
) -> list[dict[str, Any]]:
    rows = []
    for caption in captions:
        t0 = float(caption["t0"])
        if not (start_sec <= t0 < end_sec):
            continue
        raw_text = clean_text(caption.get("text"))
        starts_turn = raw_text.startswith(">>")
        text = re.sub(r"^>>\s*", "", raw_text).strip()
        if not text:
            continue
        last = rows[-1] if rows else None
        if not last or starts_turn or t0 - float(last["t_sec"]) > 18 or len(str(last["text"])) > 280:
            rows.append({"t_sec": round_sec(t0), "speaker": "Speaker", "text": text})
        else:
            last["text"] = f"{last['text']} {text}"
    return rows


def _summary(derived: dict[str, Any], override: dict[str, Any] | None) -> list[str]:
    if derived.get("summary"):
        summary = derived["summary"]
        items = summary if isinstance(summary, list) else [str(summary)]
        return [normalize_summary_text(item) for item in items]
    if override:
        return [normalize_summary_text(item) for item in override["summary"]]
    return ["Summary pending."]


def _tags(derived: dict[str, Any], override: dict[str, Any] | None) -> list[str]:
    if derived.get("tags"):
        return [str(tag) for tag in derived["tags"]]
    if override:
        return list(override["tags"])
    return ["HEARING"]


def _site_body(meeting, payload: dict[str, Any]) -> str:
    body = str(meeting.body_name or payload.get("body_name") or payload.get("body") or "").strip()
    location = str(meeting.event_location or payload.get("event_location") or payload.get("location") or "")
    if _is_council_location(location) or _is_council_body_name(body):
        return COUNCIL_BODY
    return body or COUNCIL_BODY


def _is_council_location(location: str) -> bool:
    value = location.lower()
    return any(marker in value for marker in COUNCIL_LOCATION_MARKERS)


def _is_council_body_name(body: str) -> bool:
    value = body.lower()
    return (
        value in {"city council", "new york city council"}
        or value.startswith("committee on ")
        or value.startswith("subcommittee on ")
    )


def _meeting_title(meeting, payload: dict[str, Any]) -> str:
    body = str(meeting.body_name or payload.get("body_name") or payload.get("body") or "Meeting").strip()
    meeting_type = str(meeting.meeting_type or payload.get("meeting_type") or "")
    if "stated" in body.lower() or meeting_type == "STATED_MEETING" or body.lower() == "city council":
        return "Stated Meeting"
    return body


def _meeting_slug(date: str, time: str, title: str) -> str:
    time_part = _time_slug(time)
    parts = [date, time_part, title] if time_part else [date, title]
    return _slugify(" ".join(part for part in parts if part))


def _time_slug(value: str) -> str:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AP]M)\s*$", value, re.I)
    if match:
        hour = int(match.group(1))
        return f"{hour:02d}{match.group(2)}-{match.group(3).lower()}"
    return _slugify(value)


def _chapter_slug(title: str, index: int, seen: dict[str, int]) -> str:
    base = _slugify(title) or f"chapter-{index + 1}"
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}-{count + 1}"


def _slugify(value: str) -> str:
    return (
        re.sub(r"[^a-z0-9]+", "-", value.lower())
        .strip("-")[:96]
        .strip("-")
    )


def _video_url(meeting, payload: dict[str, Any]) -> str:
    video_base_url = os.environ.get("VIDEO_BASE_URL", "").strip().rstrip("/")
    if video_base_url:
        return f"{video_base_url}/{meeting.meeting_key}/video-web.mp4"
    configured = meeting.video_web_url or payload.get("video_web_url")
    if configured:
        return str(configured)
    return f"/videos/{meeting.meeting_key}.mp4"


def _last_utterance_end(utterances: Iterable[dict[str, Any]]) -> float:
    last = 0.0
    for row in utterances:
        last = max(last, float(row.get("t1") or row.get("t0") or 0))
    return last
