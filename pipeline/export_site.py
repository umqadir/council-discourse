from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import db
from .artifacts import clean_text, normalize_utterances, parse_clock, read_json, read_jsonl, round_sec
from .config import DATA_DIR, REGISTRY_DB, ROOT

SITE_DATA_DIR = ROOT / "site" / "src" / "data" / "meetings"
BENCHMARK_DIR = DATA_DIR / "benchmark"
CHAPTER_MODEL = "gemini-3.5-flash"

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


def export_site(
    db_path: Path = REGISTRY_DB,
    out_dir: Path = SITE_DATA_DIR,
    include_benchmark: bool = True,
) -> list[Path]:
    meetings = []
    if include_benchmark:
        meetings.extend(_benchmark_meetings())
    meetings.extend(_registry_meetings(db_path))

    if not meetings:
        raise RuntimeError("no completed meetings found to export")

    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    seen: set[str] = set()
    for meeting in meetings:
        slug = meeting["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        path = out_dir / f"{slug}.json"
        path.write_text(json.dumps(meeting, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


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
    return [_convert_meeting(db.meeting_from_row(row), dict(row), None) for row in rows]


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
    title = override["title"] if override else _meeting_title(meeting)
    body = override["body"] if override else (meeting.body_name or "New York City Council")
    slug = override["slug"] if override else (meeting.meeting_slug or _meeting_slug(date, time, body))
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
                "summary": str(chapter.get("summary") or ""),
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
        return summary if isinstance(summary, list) else [str(summary)]
    if override:
        return list(override["summary"])
    return ["Summary pending."]


def _tags(derived: dict[str, Any], override: dict[str, Any] | None) -> list[str]:
    if derived.get("tags"):
        return [str(tag) for tag in derived["tags"]]
    if override:
        return list(override["tags"])
    return ["HEARING"]


def _meeting_title(meeting) -> str:
    body = meeting.body_name or "Meeting"
    if "stated" in body.lower():
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
    configured = meeting.video_web_url or payload.get("video_web_url")
    if configured:
        return str(configured)
    return f"/videos/{meeting.meeting_key}.mp4"


def _last_utterance_end(utterances: Iterable[dict[str, Any]]) -> float:
    last = 0.0
    for row in utterances:
        last = max(last, float(row.get("t1") or row.get("t0") or 0))
    return last
