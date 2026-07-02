from __future__ import annotations

import re
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
from .runlog import append_gemini_runlog

ALLOWED_MEETING_TAGS = {"HEARING", "VOTE", "STATED_MEETING", "LAND_USE"}
CHAPTER_TYPE_ORDER = [
    "REMARKS",
    "AGENCY_TESTIMONY",
    "TESTIMONY",
    "QA",
    "VOTE",
    "VOICE_VOTE",
    "VOTE_OUTCOME",
    "ROLL_CALL",
    "PROCEDURE",
    "INVOCATION",
    "CEREMONY",
]
ALLOWED_CHAPTER_TYPES = set(CHAPTER_TYPE_ORDER)


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
            "estimated_cost_usd": meta.get("estimated_cost_usd"),
            "pricing": meta.get("pricing"),
        }
    )

    chapters_path = meeting.meeting_dir / "chapters.json"
    derived_path = meeting.meeting_dir / "meeting-derived.json"
    stage_meta = {
        "model": model,
        "elapsed_sec": meta["elapsed_sec"],
        "usage": meta.get("usage", {}),
        "estimated_cost_usd": meta.get("estimated_cost_usd"),
        "pricing": meta.get("pricing"),
        "chapters": chapters,
    }
    write_json(chapters_path, stage_meta)
    write_json(derived_path, derived)
    append_gemini_runlog(
        meeting.meeting_dir,
        "chapterize",
        model,
        stage_meta,
        {"chapter_count": len(chapters), "meeting_type": _meeting_type(meeting)},
    )
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
    meeting_type = _meeting_type(meeting)
    meeting_type_rules = _meeting_type_rules(meeting_type)
    chapter_types = ", ".join(CHAPTER_TYPE_ORDER)
    transcript = "\n".join(_chapter_line(index, row) for index, row in enumerate(utterances))
    return f"""You are dividing a NYC Council meeting transcript into chapters for a public website that helps residents navigate long meetings. Users skim chapter titles to find the 2-5 minute segments they care about.

MEETING CONTEXT:
{context}

MEETING TYPE:
{meeting_type}

TRANSCRIPT (timestamped ASR text; speaker names may be inferred and ASR errors are expected):
<transcript>
{transcript}
</transcript>

Divide the ENTIRE meeting into consecutive, non-overlapping chapters. Rules:
- A chapter should answer "one thing happened here." Prefer 1-5 minute chapters for substantive remarks and much shorter chapters for votes, roll calls, adoptions, and procedural outcomes.
- FINE granularity is essential. Split aggressively:
  * Opening remarks: one chapter per distinct topic the speaker covers (a 5-minute opening becomes 3-5 chapters).
  * Agency/public testimony: one chapter per testifying person; long testimony splits by topic.
  * Q&A: one chapter per question-and-answer exchange (a member asking about a new topic starts a new chapter, even mid-round). Never merge multiple members into one chapter.
  * Votes/roll calls/procedure: each discrete vote, roll call, adoption, or outcome is its own short chapter.
{meeting_type_rules}
- Cover the whole meeting; no gaps. First chapter starts at the meeting's first speech.
- chapter type: one of {chapter_types}.
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
        f"Inferred meeting type: {_meeting_type(meeting)}",
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


def _meeting_type(meeting: Meeting) -> str:
    values = [meeting.meeting_key, meeting.body_name or ""]
    meeting_json = meeting.meeting_dir / "meeting.json"
    if meeting_json.exists():
        payload = read_json(meeting_json)
        for key in ("body", "body_name", "title", "agenda_topic", "agenda", "meeting_type"):
            if payload.get(key):
                values.append(str(payload[key]))
    text = " ".join(values).lower()
    if "stated" in text:
        return "STATED_MEETING"
    if "land use" in text:
        return "LAND_USE"
    if "vote" in text:
        return "VOTE"
    return "HEARING"


def _meeting_type_rules(meeting_type: str) -> str:
    if meeting_type == "STATED_MEETING":
        return """- STATED MEETING splitting rules:
  * Every agenda item's vote, adoption, disposition, or announced result is its own chapter. Use VOICE_VOTE for ayes/nays voice votes and VOTE_OUTCOME for announced tallies or adoption results.
  * Roll calls are separate ROLL_CALL chapters. If members explain their votes during a roll call, split each council member's floor remarks/explanation of vote into its own REMARKS chapter, then resume the roll call or outcome chapter.
  * Do not merge a run of resolutions, introductions, land-use items, or finance items into one vote chapter; each item or item group being acted on gets a separate chapter.
  * Split agenda overviews by matter group when the Speaker or Majority Leader moves from one item/package to the next.
  * Ceremonial items each get their own chapter: use INVOCATION for prayers/invocations and CEREMONY for honoree presentations, tributes, proclamations, or recognitions."""
    return """- HEARING/GENERAL splitting rules:
  * Keep council member floor remarks separate by speaker and topic.
  * For voting moments in hearings, split the motion, roll call, and final outcome when the transcript has separate starts for them."""


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
        chapter_type = re.sub(r"[\s-]+", "_", str(raw.get("type") or "REMARKS").strip().upper())
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
    if any(type_counts[tag] for tag in ("VOTE", "VOICE_VOTE", "VOTE_OUTCOME", "ROLL_CALL")):
        tags.append("VOTE")
    if "land use" in text:
        tags.append("LAND_USE")
    return tags or ["HEARING"]
