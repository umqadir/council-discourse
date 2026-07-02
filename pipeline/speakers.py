from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import (
    captions_to_utterances,
    normalize_utterances,
    read_json,
    read_jsonl,
    sec_to_clock,
    write_json,
    write_jsonl,
)
from .gemini import DEFAULT_MODEL, estimate_tokens, generate_json
from .models import Meeting
from .roster import roster_csv_for_prompt

MAX_PROMPT_TOKENS = 150_000
CHUNK_TARGET_TOKENS = 125_000
CHUNK_OVERLAP = 60


def name_speakers_meeting(meeting: Meeting, model: str = DEFAULT_MODEL) -> Path:
    input_path = _speaker_input_path(meeting.meeting_dir)
    utterances = normalize_utterances(read_jsonl(input_path))
    if not utterances:
        raise RuntimeError(f"no utterances found in {input_path}")

    roster_csv = roster_csv_for_prompt(meeting.event_date)
    context = _meeting_context(meeting)
    assignments: list[dict[str, Any]] = []
    usage_totals: Counter[str] = Counter()
    elapsed_total = 0.0

    chunks = _chunk_utterances(utterances, roster_csv, context)
    speaker_by_index: list[str | None] = [None] * len(utterances)
    for chunk in chunks:
        prompt = _speaker_prompt(
            roster_csv=roster_csv,
            context=context,
            utterances=utterances[chunk["start"] : chunk["end"]],
            offset=chunk["start"],
        )
        result, meta = generate_json(prompt, model=model, temperature=0.1)
        elapsed_total += float(meta.get("elapsed_sec", 0))
        usage_totals.update({k: int(v) for k, v in meta.get("usage", {}).items() if isinstance(v, int)})
        chunk_assignments = _extract_assignments(result)
        assignments.extend(chunk_assignments)
        _apply_assignments(speaker_by_index, chunk_assignments, chunk["start"], chunk["end"])

    named = []
    for index, row in enumerate(utterances):
        out = dict(row)
        out["speaker"] = speaker_by_index[index] or "UNKNOWN"
        named.append(out)

    output = meeting.meeting_dir / "utterances-named.jsonl"
    write_jsonl(output, named)
    write_json(
        meeting.meeting_dir / "name-speakers-meta.json",
        {
            "model": model,
            "input": str(input_path),
            "utterance_count": len(utterances),
            "chunks": len(chunks),
            "elapsed_sec": round(elapsed_total, 3),
            "usage": dict(usage_totals),
            "assignments": assignments,
        },
    )
    return output


def _speaker_input_path(meeting_dir: Path) -> Path:
    utterances = meeting_dir / "utterances.jsonl"
    if utterances.exists():
        return utterances
    captions = meeting_dir / "captions-clean.jsonl"
    if captions.exists():
        converted = captions_to_utterances(read_jsonl(captions))
        output = meeting_dir / "utterances.jsonl"
        write_jsonl(output, converted)
        return output
    raise RuntimeError(f"missing utterances.jsonl or captions-clean.jsonl in {meeting_dir}")


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


def _chunk_utterances(
    utterances: list[dict[str, Any]],
    roster_csv: str,
    context: str,
) -> list[dict[str, int]]:
    full_prompt = _speaker_prompt(roster_csv, context, utterances, 0)
    if estimate_tokens(full_prompt) <= MAX_PROMPT_TOKENS:
        return [{"start": 0, "end": len(utterances)}]

    chunks = []
    start = 0
    while start < len(utterances):
        end = start
        lines: list[str] = []
        while end < len(utterances):
            lines.append(_transcript_line(end, utterances[end]))
            prompt_tokens = estimate_tokens(_speaker_prompt(roster_csv, context, [], 0)) + estimate_tokens(
                "\n".join(lines)
            )
            if prompt_tokens > CHUNK_TARGET_TOKENS and end > start:
                break
            end += 1
        chunks.append({"start": start, "end": min(end, len(utterances))})
        if end >= len(utterances):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return chunks


def _speaker_prompt(
    roster_csv: str,
    context: str,
    utterances: list[dict[str, Any]],
    offset: int,
) -> str:
    transcript = "\n".join(_transcript_line(offset + i, row) for i, row in enumerate(utterances))
    return f"""You are assigning speaker names to every utterance in a NYC Council transcript.

MEETING CONTEXT:
{context}

CURRENT COUNCIL ROSTER CSV (party may be blank if the source dataset lacks it):
{roster_csv}

INFERENCE RULES:
- Assign one speaker to every utterance index in the transcript.
- Prefer direct self-introductions over all other evidence.
- Next strongest evidence: a chair, clerk, or counsel introduces the next speaker.
- Use content, procedural context, roll-call order, and roster names only when the text supports it.
- Do not invent people. If a public witness states a name, use "Member of the Public - Name".
- Allowed fallbacks are exactly "Council Staff", "Member of the Public", "Member of the Public - Name", and "UNKNOWN".
- For council members, use the roster name only, for example "Julie Menin", not titles.
- Use inclusive index ranges and cover every index exactly once.

TRANSCRIPT:
<transcript>
{transcript}
</transcript>

Return JSON only:
{{
  "assignments": [
    {{"start_index": 0, "end_index": 12, "speaker": "Julie Menin"}}
  ]
}}
"""


def _transcript_line(index: int, row: dict[str, Any]) -> str:
    return f"[{index}] {sec_to_clock(row['t0'])}-{sec_to_clock(row['t1'])}: {row['text']}"


def _extract_assignments(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("assignments") or result.get("ranges") or result.get("speaker_ranges")
    if not isinstance(raw, list):
        raise RuntimeError(f"Gemini speaker response lacks assignments: {result}")
    assignments = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start_index", item.get("start", item.get("from")))
        end = item.get("end_index", item.get("end", item.get("to", start)))
        speaker = str(item.get("speaker") or item.get("name") or "UNKNOWN").strip()
        if start is None:
            continue
        assignments.append(
            {
                "start_index": int(start),
                "end_index": int(end),
                "speaker": _clean_speaker(speaker),
            }
        )
    return assignments


def _apply_assignments(
    speaker_by_index: list[str | None],
    assignments: list[dict[str, Any]],
    chunk_start: int,
    chunk_end: int,
) -> None:
    for assignment in assignments:
        start = max(chunk_start, int(assignment["start_index"]))
        end = min(chunk_end - 1, int(assignment["end_index"]))
        if end < start:
            continue
        speaker = _clean_speaker(str(assignment["speaker"]))
        for index in range(start, end + 1):
            if speaker_by_index[index] is None or speaker_by_index[index] == "UNKNOWN":
                speaker_by_index[index] = speaker


def _clean_speaker(value: str) -> str:
    speaker = " ".join(value.replace("Council Member ", "").replace("Councilmember ", "").split())
    if speaker.lower() in {"unknown", "unk"}:
        return "UNKNOWN"
    return speaker or "UNKNOWN"

