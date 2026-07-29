from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path
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
SERIAL_VOTE_PARENT_TYPES = {"VOICE_VOTE", "VOTE"}
# Sized to pass any real meeting and only trip on a genuinely degenerate
# transcript. The chaptering model (GLM-5.2) has a ~1M-token context and 131k
# max output; a full-day marathon hearing (~10h, e.g. the 9.4h Committee on
# Health session) is only ~200k tokens. The old 180k value assumed the ~200k
# context of GLM-5.2's siblings and hard-failed normal long meetings. Anything
# past this is not a long meeting, it is a broken/looping transcript.
DEFAULT_MAX_CHAPTER_PROMPT_TOKENS = 700_000
CHAPTER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "meeting_summary": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "start_index": {"type": "integer"},
                    "start_sec": {"type": "number"},
                    "type": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["type", "title", "summary"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["chapters"],
    "additionalProperties": True,
}


def chapterize_meeting(
    meeting: Meeting,
    model: str = DEFAULT_MODEL,
    *,
    input_path: str | None = None,
    output_path: str | None = None,
    derived_path: str | None = None,
    runlog_stage: str = "chapterize",
    write_runlog: bool = True,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    llm_api_key_env: str | None = None,
) -> tuple[str, str]:
    chapters_path = Path(output_path) if output_path else meeting.meeting_dir / "chapters.json"
    derived_output_path = Path(derived_path) if derived_path else meeting.meeting_dir / "meeting-derived.json"
    cached_meta = _cached_chapterize_meta(chapters_path)
    if cached_meta is not None:
        if write_runlog:
            append_gemini_runlog(
                meeting.meeting_dir,
                runlog_stage,
                str(cached_meta.get("model") or model),
                cached_meta,
                {"chapter_count": len(cached_meta.get("chapters") or []), "cached": True},
            )
        return str(chapters_path), str(derived_output_path)

    input_path_obj = Path(input_path) if input_path else _chapter_input_path(meeting)
    utterances = normalize_utterances(read_jsonl(input_path_obj))
    if not utterances:
        raise RuntimeError(f"no utterances found in {input_path_obj}")

    meeting_type = _meeting_type(meeting)
    prompt = _chapter_prompt(meeting, utterances)
    _raise_if_chapter_prompt_too_large(prompt)
    temperature = 0.2 if meeting_type == "STATED_MEETING" else 0.3
    result, meta = generate_json(
        prompt,
        model=model,
        temperature=temperature,
        base_url=llm_base_url,
        api_key=llm_api_key,
        api_key_env=llm_api_key_env,
        json_schema=CHAPTER_JSON_SCHEMA,
    )
    metas = [meta]
    chapters = _resolve_chapters(result, utterances, meeting.duration_seconds)
    chapters = _postprocess_chapters(chapters, utterances, meeting, meeting.duration_seconds)
    retry_note = _coarse_retry_note(meeting_type, len(chapters), meeting.duration_seconds)
    if retry_note:
        retry_prompt = f"{prompt}\n\nQUALITY GATE:\n{retry_note}\nReturn a complete replacement JSON object, not a patch."
        retry_result, retry_meta = generate_json(
            retry_prompt,
            model=model,
            temperature=min(0.4, temperature + 0.1),
            base_url=llm_base_url,
            api_key=llm_api_key,
            api_key_env=llm_api_key_env,
            json_schema=CHAPTER_JSON_SCHEMA,
        )
        metas.append(retry_meta)
        retry_chapters = _resolve_chapters(retry_result, utterances, meeting.duration_seconds)
        retry_chapters = _postprocess_chapters(retry_chapters, utterances, meeting, meeting.duration_seconds)
        if _chapter_count_distance(meeting_type, len(retry_chapters), meeting.duration_seconds) <= _chapter_count_distance(
            meeting_type,
            len(chapters),
            meeting.duration_seconds,
        ):
            result = retry_result
            chapters = retry_chapters
    meta = _combined_generation_meta(metas)
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

    stage_meta = {
        "model": model,
        "provider": meta.get("provider"),
        "input": str(input_path_obj),
        "elapsed_sec": meta["elapsed_sec"],
        "usage": meta.get("usage", {}),
        "estimated_cost_usd": meta.get("estimated_cost_usd"),
        "exact_cost_usd": meta.get("exact_cost_usd"),
        "cost_source": meta.get("cost_source"),
        "pricing": meta.get("pricing"),
        "structured_mode": meta.get("structured_mode"),
        "chapters": chapters,
    }
    write_json(chapters_path, stage_meta)
    write_json(derived_output_path, derived)
    if write_runlog:
        append_gemini_runlog(
            meeting.meeting_dir,
            runlog_stage,
            model,
            stage_meta,
            {"chapter_count": len(chapters), "meeting_type": meeting_type},
        )
    return str(chapters_path), str(derived_output_path)


def _cached_chapterize_meta(chapters_path: Path) -> dict[str, Any] | None:
    try:
        if not chapters_path.exists() or not chapters_path.is_file() or chapters_path.stat().st_size <= 0:
            return None
        payload = read_json(chapters_path)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    chapters = payload.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        return None
    return payload


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
- A chapter should answer "one thing happened here." Prefer 1-5 minute chapters for substantive remarks and Q&A, and shorter chapters for real votes, roll calls, adoptions, and outcomes. Avoid micro-chapters for transition-only lines.
- FINE granularity is essential, but split on civic events and topic changes, not every sentence:
  * Opening remarks: one chapter per distinct topic the speaker covers; a dense 5-minute opening often becomes 4-6 chapters.
  * Agency/public testimony: one chapter per testifying person; split long prepared testimony by topic when it runs past several minutes.
  * Q&A: one chapter per question-and-answer exchange or topic thread. A member asking about a new topic starts a new chapter, even mid-round. Never merge multiple council members into one chapter.
  * Votes/roll calls/procedure: each discrete substantive vote, roll call, adoption, or outcome is its own short chapter; do not create standalone chapters for brief transitions, repeated name-only roll-call fragments, or staff housekeeping.
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


def _raise_if_chapter_prompt_too_large(prompt: str) -> None:
    tokens = max(1, int(len(prompt) / 4))
    limit = int(os.environ.get("COUNCIL_CHAPTER_MAX_PROMPT_TOKENS", DEFAULT_MAX_CHAPTER_PROMPT_TOKENS))
    if tokens > limit:
        approx = max(1, round(tokens / 1000))
        raise RuntimeError(f"transcript too long for chaptering: ~{approx}k tokens")


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
    if meeting.meeting_type in ALLOWED_MEETING_TAGS:
        return meeting.meeting_type
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
  * A typical 90-120 minute stated meeting often lands around 50-75 chapters, depending on agenda density. Prefer item-level civic events, but do not inflate the count with name-only roll-call continuations or purely formal one-liners.
  * Every agenda item's vote, adoption, disposition, or announced result is its own chapter. Use VOICE_VOTE for ayes/nays voice votes and VOTE_OUTCOME for announced tallies or adoption results. Use generic VOTE only when neither label fits.
  * Roll calls are separate ROLL_CALL chapters only when the roll call itself is a substantial event. If members explain their votes during a roll call, each council member's floor remarks/explanation of vote is its own REMARKS chapter. Do not create separate chapters for short resumed name-only roll-call stretches between explanations; fold those into the adjacent REMARKS or final VOTE_OUTCOME chapter.
  * Do not merge a run of resolutions, introductions, land-use items, or finance items into one vote chapter. When the transcript reads Resolution/Introduction/LU numbers one after another and says each is adopted, create one short VOICE_VOTE or VOTE_OUTCOME chapter per number, even if each chapter is only 10-30 seconds. Never use plural titles like "Voice Votes on Resolutions"; use titles like "Voice Vote on Resolution 8: Lead Service Line Replacement Funding".
  * Split agenda overviews by matter group when the Speaker or Majority Leader moves from one item/package to the next.
  * Treat member statements, explanations of vote, substantive communications from members, and new bill introductions as REMARKS unless the main event is the vote itself. Fold purely formal one-line motions, communications readings, and adjournment into neighboring chapters unless they include a substantive outcome.
  * Ceremonial items each get their own chapter: use INVOCATION for prayers/invocations and CEREMONY for honoree presentations, tributes, proclamations, or recognitions."""
    return """- HEARING/GENERAL splitting rules:
  * Fine granularity is especially important in long hearings: chapters are typically 1-4 minutes, and a 4-hour oversight hearing should usually produce roughly 90-130 chapters unless much of it is silence or procedure.
  * Keep council member floor remarks separate by speaker and topic.
  * Split long Q&A rounds aggressively by topic thread. If one generated QA chapter would cover more than about 4 minutes, look for the next question, follow-up, witness answer, or topic shift and start a new QA chapter there.
  * Split long agency testimony by topic when a prepared statement moves from history to current implementation, statistics, process, future plans, or recommendations; a 7-minute prepared statement is usually several chapters.
  * Public testimony is normally one chapter per witness. Do not make separate panel-transition chapters unless the transition contains substantive instructions or context.
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


def _postprocess_chapters(
    chapters: list[dict[str, Any]],
    utterances: list[dict[str, Any]],
    meeting: Meeting,
    duration_seconds: float | None,
) -> list[dict[str, Any]]:
    if _meeting_type(meeting) != "STATED_MEETING":
        return chapters
    chapters = _split_serial_voice_votes(chapters, utterances)
    return _with_chapter_ends(chapters, utterances, duration_seconds)


def _coarse_retry_note(meeting_type: str, chapter_count: int, duration_seconds: float | None) -> str | None:
    floor = _chapter_count_floor(meeting_type, duration_seconds)
    if floor is None or chapter_count >= floor:
        return None
    hours = (duration_seconds or 0.0) / 3600
    return (
        f"The draft has only {chapter_count} chapters for a {hours:.1f}-hour {meeting_type} meeting, "
        f"which is too coarse. Produce at least {floor} chapters by splitting long Q&A, testimony, "
        "member remarks, agenda-item discussions, and item-level votes at their natural topic boundaries. "
        "Keep chapters useful and consecutive; do not add empty transition chapters just to hit the count."
    )


def _chapter_count_floor(meeting_type: str, duration_seconds: float | None) -> int | None:
    if not duration_seconds:
        return None
    hours = duration_seconds / 3600
    if meeting_type == "HEARING":
        return max(12, round(hours * 20))
    if meeting_type == "STATED_MEETING" and duration_seconds >= 3600:
        return max(35, round(hours * 30))
    return None


def _chapter_count_distance(meeting_type: str, chapter_count: int, duration_seconds: float | None) -> int:
    floor = _chapter_count_floor(meeting_type, duration_seconds)
    if floor is None or chapter_count >= floor:
        return 0
    return floor - chapter_count


def _combined_generation_meta(metas: list[dict[str, Any]]) -> dict[str, Any]:
    if len(metas) == 1:
        return metas[0]
    combined = dict(metas[-1])
    combined["elapsed_sec"] = round(sum(float(meta.get("elapsed_sec") or 0.0) for meta in metas), 3)
    usage_totals: Counter[str] = Counter()
    for meta in metas:
        usage = meta.get("usage")
        if isinstance(usage, dict):
            usage_totals.update({k: int(v) for k, v in usage.items() if isinstance(v, int | float)})
    if usage_totals:
        combined["usage"] = dict(usage_totals)
    for key in ("estimated_cost_usd", "exact_cost_usd"):
        costs = [meta.get(key) for meta in metas]
        if all(isinstance(cost, int | float) for cost in costs):
            combined[key] = round(sum(float(cost) for cost in costs), 6)
    combined["attempts"] = metas
    return combined


_AGENDA_ITEM_START_RE = re.compile(
    r"^\s*(Resolution|Introduction|Intro|LU|Land Use(?: Item)?)\s+"
    r"([0-9]+[A-Z]?(?:-[A-Z])?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}


def _split_serial_voice_votes(
    chapters: list[dict[str, Any]],
    utterances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for chapter in chapters:
        if chapter.get("type") not in SERIAL_VOTE_PARENT_TYPES:
            output.append(chapter)
            continue

        item_starts = _agenda_item_starts_in_span(utterances, chapter["start_sec"], chapter["end_sec"])
        if len(item_starts) < 2:
            output.append(chapter)
            continue

        for item_index, (row, match) in enumerate(item_starts):
            row_start = utterance_start(row)
            next_start = (
                utterance_start(item_starts[item_index + 1][0])
                if item_index + 1 < len(item_starts)
                else chapter["end_sec"]
            )
            output.append(
                {
                    "start": sec_to_clock(row_start),
                    "start_sec": round(row_start, 3),
                    "end_sec": round(max(row_start + 1, float(next_start)), 3),
                    "type": "VOICE_VOTE",
                    "title": _serial_voice_vote_title(match, row.get("text") or ""),
                    "summary": _serial_voice_vote_summary(match, row.get("text") or ""),
                }
            )
    return output


def _agenda_item_starts_in_span(
    utterances: list[dict[str, Any]],
    start_sec: float,
    end_sec: float,
) -> list[tuple[dict[str, Any], re.Match[str]]]:
    matches: list[tuple[dict[str, Any], re.Match[str]]] = []
    for row in utterances:
        row_start = utterance_start(row)
        if row_start < start_sec - 3 or row_start >= end_sec:
            continue
        match = _AGENDA_ITEM_START_RE.match(str(row.get("text") or ""))
        if match:
            matches.append((row, match))
    return matches


def _serial_voice_vote_title(match: re.Match[str], text: str) -> str:
    item = _agenda_item_label(match)
    topic = _agenda_item_topic(match, text)
    return f"Voice Vote on {item}: {topic}" if topic else f"Voice Vote on {item}"


def _serial_voice_vote_summary(match: re.Match[str], text: str) -> str:
    item = _agenda_item_label(match)
    topic = _agenda_item_topic(match, text)
    if topic:
        return f"The Council took a voice vote on {item}, concerning {topic}. The ayes had it and the item was adopted."
    return f"The Council took a voice vote on {item}. The ayes had it and the item was adopted."


def _agenda_item_label(match: re.Match[str]) -> str:
    kind = match.group(1).title()
    if kind == "Intro":
        kind = "Introduction"
    elif kind.startswith("Lu"):
        kind = "LU"
    number = match.group(2)
    number = _NUMBER_WORDS.get(number.lower(), number.upper())
    return f"{kind} {number}"


def _agenda_item_topic(match: re.Match[str], text: str) -> str:
    topic = text[match.end() :].strip(" .:-")
    topic = re.sub(r"^(calls on|calls for|calls upon|would|approves?|authorizes?)\s+", "", topic, flags=re.IGNORECASE)
    topic = re.sub(r"\s+", " ", topic).strip(" .")
    words = topic.split()
    if len(words) > 12:
        topic = " ".join(words[:12]).rstrip(",;:") + "..."
    return topic


def _with_chapter_ends(
    chapters: list[dict[str, Any]],
    utterances: list[dict[str, Any]],
    duration_seconds: float | None,
) -> list[dict[str, Any]]:
    if not chapters:
        return chapters
    starts = [utterance_start(row) for row in utterances]
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
