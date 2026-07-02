from __future__ import annotations

from collections import Counter
from typing import Any

from .artifacts import (
    captions_to_utterances,
    normalize_utterances,
    parse_clock,
    read_json,
    read_jsonl,
    sec_to_clock,
    utterance_start,
    write_json,
    write_jsonl,
)
from .gemini import DEFAULT_MODEL, generate_json
from .models import Meeting

ALLOWED_MEETING_TAGS = {"HEARING", "VOTE", "STATED_MEETING", "LAND_USE"}
ALLOWED_CHAPTER_TYPES = {"REMARKS", "AGENCY_TESTIMONY", "TESTIMONY", "QA", "VOTE", "PROCEDURE"}


def chapterize_meeting(meeting: Meeting, model: str = DEFAULT_MODEL) -> tuple[str, str]:
    input_path = _chapter_input_path(meeting)
    utterances = normalize_utterances(read_jsonl(input_path))
    if not utterances:
        raise RuntimeError(f"no utterances found in {input_path}")

    prompt = _chapter_prompt(meeting, utterances)
    result, meta = generate_json(prompt, model=model, temperature=0.3)
    chapters = _resolve_chapters(result, utterances, meeting.duration_seconds)
    derived = _meeting_derived(result, meeting)
    derived.update(
        {
            "model": model,
            "elapsed_sec": meta["elapsed_sec"],
            "usage": meta.get("usage", {}),
        }
    )

    chapters_path = meeting.meeting_dir / "chapters.json"
    derived_path = meeting.meeting_dir / "meeting-derived.json"
    write_json(
        chapters_path,
        {
            "model": model,
            "elapsed_sec": meta["elapsed_sec"],
            "usage": meta.get("usage", {}),
            "chapters": chapters,
        },
    )
    write_json(derived_path, derived)
    return str(chapters_path), str(derived_path)


def _chapter_input_path(meeting: Meeting):
    for name in ("utterances-named.jsonl", "utterances.jsonl"):
        path = meeting.meeting_dir / name
        if path.exists():
            return path
    captions = meeting.meeting_dir / "captions-clean.jsonl"
    if captions.exists():
        output = meeting.meeting_dir / "utterances.jsonl"
        write_jsonl(output, captions_to_utterances(read_jsonl(captions)))
        return output
    raise RuntimeError(f"missing named utterances, raw utterances, or captions in {meeting.meeting_dir}")


def _chapter_prompt(meeting: Meeting, utterances: list[dict[str, Any]]) -> str:
    context = _meeting_context(meeting)
    transcript = "\n".join(_chapter_line(index, row) for index, row in enumerate(utterances))
    return f"""You are dividing a NYC Council meeting transcript into chapters for a public website that helps residents navigate long meetings. Users skim chapter titles to find the 2-5 minute segments they care about.

MEETING CONTEXT:
{context}

TRANSCRIPT (timestamped ASR text; speaker names may be inferred and ASR errors are expected):
<transcript>
{transcript}
</transcript>

Divide the ENTIRE meeting into consecutive, non-overlapping chapters. Rules:
- FINE granularity is essential: chapters are typically 1-4 MINUTES long; a 4-hour meeting should produce roughly 90-130 chapters. Split aggressively:
  * Opening remarks: one chapter per distinct topic the speaker covers (a 5-minute opening becomes 3-5 chapters).
  * Agency/public testimony: one chapter per testifying person; long testimony splits by topic.
  * Q&A: one chapter per question-and-answer exchange (a member asking about a new topic starts a new chapter, even mid-round). Never merge multiple members into one chapter.
  * Votes/roll calls/procedure: each is its own short chapter.
- Cover the whole meeting; no gaps. First chapter starts at the meeting's first speech.
- chapter type: one of REMARKS, AGENCY_TESTIMONY, TESTIMONY, QA, VOTE, PROCEDURE.
- meeting tags: choose all applicable from HEARING, VOTE, STATED_MEETING, LAND_USE.
- title: a specific headline naming who and what, e.g. "Council Member Menin questions DOT on application processing delays". Never use generic titles like "Opening remarks continued".
- summary: 2-4 sentences, concrete, naming speakers and specifics.
- start: the timestamp (H:MM:SS) copied from a transcript line.
- start_index: the utterance index where the chapter begins.
- meeting_summary: 3 concise bullets as strings.

Return JSON only:
{{
  "meeting_summary": ["...", "...", "..."],
  "tags": ["HEARING"],
  "chapters": [
    {{"start": "0:01:09", "start_index": 12, "type": "REMARKS", "title": "...", "summary": "..."}}
  ]
}}
"""


def _meeting_context(meeting: Meeting) -> str:
    parts = [
        f"Meeting key: {meeting.meeting_key}",
        f"Body: {meeting.body_name or 'NYC Council'}",
        f"Date: {meeting.event_date or 'unknown'}",
        f"Time: {meeting.event_time or 'unknown'}",
    ]
    meeting_json = meeting.meeting_dir / "meeting.json"
    if meeting_json.exists():
        payload = read_json(meeting_json)
        for key in ("agenda_topic", "agenda", "title", "body"):
            if payload.get(key):
                parts.append(f"{key}: {payload[key]}")
    agenda_txt = meeting.meeting_dir / "agenda.txt"
    if agenda_txt.exists():
        parts.append("Agenda text excerpt:\n" + agenda_txt.read_text(errors="replace")[:8000])
    return "\n".join(parts)


def _chapter_line(index: int, row: dict[str, Any]) -> str:
    speaker = row.get("speaker") or "Speaker"
    return f"[{index}] [{sec_to_clock(row['t0'])}] {speaker}: {row['text']}"


def _resolve_chapters(
    result: dict[str, Any],
    utterances: list[dict[str, Any]],
    duration_seconds: float | None,
) -> list[dict[str, Any]]:
    raw_chapters = result.get("chapters")
    if not isinstance(raw_chapters, list):
        raise RuntimeError(f"Gemini chapter response lacks chapters: {result}")

    starts = [utterance_start(row) for row in utterances]
    chapters: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_chapters):
        if not isinstance(raw, dict):
            continue
        start_sec = _chapter_start_sec(raw, starts)
        chapter_type = str(raw.get("type") or "REMARKS").strip().upper()
        if chapter_type not in ALLOWED_CHAPTER_TYPES:
            chapter_type = "REMARKS"
        title = str(raw.get("title") or f"Chapter {index + 1}").strip()
        summary = str(raw.get("summary") or "").strip()
        if chapters and start_sec <= chapters[-1]["start_sec"]:
            continue
        chapters.append(
            {
                "start": sec_to_clock(start_sec),
                "start_sec": round(start_sec, 3),
                "end_sec": 0.0,
                "type": chapter_type,
                "title": title,
                "summary": summary,
            }
        )

    if not chapters:
        raise RuntimeError("Gemini returned no usable chapters")

    inferred_duration = duration_seconds or (starts[-1] + 5 if starts else chapters[-1]["start_sec"] + 5)
    for index, chapter in enumerate(chapters):
        next_start = chapters[index + 1]["start_sec"] if index + 1 < len(chapters) else inferred_duration
        chapter["end_sec"] = round(max(chapter["start_sec"] + 1, next_start), 3)
    return chapters


def _chapter_start_sec(raw: dict[str, Any], starts: list[float]) -> float:
    if raw.get("start_index") is not None:
        index = max(0, min(len(starts) - 1, int(raw["start_index"])))
        return starts[index]
    if raw.get("start_sec") is not None:
        return float(raw["start_sec"])
    if raw.get("start") is not None:
        return parse_clock(str(raw["start"]))
    return starts[0]


def _meeting_derived(result: dict[str, Any], meeting: Meeting) -> dict[str, Any]:
    summary = result.get("meeting_summary") or result.get("summary") or []
    if isinstance(summary, str):
        summary = [summary]
    summary = [str(item).strip() for item in summary if str(item).strip()][:5]
    tags = [str(tag).strip().upper() for tag in result.get("tags", [])]
    tags = [tag for tag in tags if tag in ALLOWED_MEETING_TAGS]
    if not tags:
        tags = _infer_tags(meeting, result.get("chapters") or [])
    return {"summary": summary, "tags": tags}


def _infer_tags(meeting: Meeting, chapters: list[dict[str, Any]]) -> list[str]:
    text = " ".join([meeting.body_name or "", meeting.meeting_key]).lower()
    tags = []
    if "stated" in text:
        tags.append("STATED_MEETING")
    if "committee" in text or "hearing" in text:
        tags.append("HEARING")
    type_counts = Counter(str(chapter.get("type", "")).upper() for chapter in chapters if isinstance(chapter, dict))
    if type_counts["VOTE"]:
        tags.append("VOTE")
    if "land use" in text:
        tags.append("LAND_USE")
    return tags or ["HEARING"]
