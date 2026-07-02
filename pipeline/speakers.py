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
from .gemini import DEFAULT_MODEL, estimate_tokens, generate_json, pricing_details
from .models import Meeting
from .roster import roster_csv_for_prompt
from .runlog import append_gemini_runlog

MAX_PROMPT_TOKENS = 700_000
CHUNK_TARGET_TOKENS = 550_000
CHUNK_OVERLAP = 240
BOUNDARY_SNIPPET_UTTERANCES = 5
SOURCE_SPEAKER_KEYS = ("diarized_speaker", "speaker_label", "speaker_id", "channel", "speaker")


def name_speakers_meeting(meeting: Meeting, model: str = DEFAULT_MODEL) -> Path:
    input_path = _speaker_input_path(meeting.meeting_dir)
    utterances = normalize_utterances(read_jsonl(input_path))
    if not utterances:
        raise RuntimeError(f"no utterances found in {input_path}")

    roster_csv = roster_csv_for_prompt(meeting.event_date)
    context = _meeting_context(meeting)
    assignments: list[dict[str, Any]] = []
    chunk_records: list[dict[str, Any]] = []
    usage_totals: Counter[str] = Counter()
    elapsed_total = 0.0
    cost_total = 0.0

    chunks = _chunk_utterances(utterances, roster_csv, context)
    speaker_by_index: list[str | None] = [None] * len(utterances)
    for chunk_number, chunk in enumerate(chunks, start=1):
        prompt = _speaker_prompt(
            roster_csv=roster_csv,
            context=context,
            utterances=utterances[chunk["start"] : chunk["end"]],
            offset=chunk["start"],
        )
        result, meta = generate_json(prompt, model=model, temperature=0.1)
        elapsed_total += float(meta.get("elapsed_sec", 0))
        cost_total += float(meta.get("estimated_cost_usd") or 0)
        usage_totals.update({k: int(v) for k, v in meta.get("usage", {}).items() if isinstance(v, int)})
        chunk_assignments = _extract_assignments(result)
        assignments.extend(chunk_assignments)
        _apply_assignments(speaker_by_index, chunk_assignments, chunk["start"], chunk["end"])
        chunk_records.append(
            {
                "chunk": chunk_number,
                "start": chunk["start"],
                "end": chunk["end"],
                "usage": meta.get("usage", {}),
                "estimated_cost_usd": meta.get("estimated_cost_usd"),
                "assignments": chunk_assignments,
            }
        )

    reconciliation: dict[str, Any] | None = None
    if len(chunks) > 1:
        reconciliation, reconciliation_meta = _reconcile_chunk_assignments(
            utterances=utterances,
            chunks=chunks,
            speaker_by_index=speaker_by_index,
            roster_csv=roster_csv,
            context=context,
            model=model,
        )
        elapsed_total += float(reconciliation_meta.get("elapsed_sec", 0))
        cost_total += float(reconciliation_meta.get("estimated_cost_usd") or 0)
        usage_totals.update(
            {k: int(v) for k, v in reconciliation_meta.get("usage", {}).items() if isinstance(v, int)}
        )

    named = []
    for index, row in enumerate(utterances):
        out = dict(row)
        out["speaker"] = speaker_by_index[index] or "UNKNOWN"
        named.append(out)

    output = meeting.meeting_dir / "utterances-named.jsonl"
    write_jsonl(output, named)
    meta_payload: dict[str, Any] = {
        "model": model,
        "input": str(input_path),
        "utterance_count": len(utterances),
        "mode": "single_pass" if len(chunks) == 1 else "chunked_fallback",
        "chunks": len(chunks),
        "chunk_ranges": [{"start": chunk["start"], "end": chunk["end"]} for chunk in chunks],
        "elapsed_sec": round(elapsed_total, 3),
        "usage": dict(usage_totals),
        "estimated_cost_usd": round(cost_total, 6),
        "pricing": pricing_details(model),
        "assignments": assignments,
    }
    if chunk_records:
        meta_payload["chunk_records"] = chunk_records
    if reconciliation:
        meta_payload["reconciliation"] = reconciliation
    write_json(meeting.meeting_dir / "name-speakers-meta.json", meta_payload)
    append_gemini_runlog(
        meeting.meeting_dir,
        "name_speakers",
        model,
        meta_payload,
        {"mode": meta_payload["mode"], "chunks": len(chunks), "utterance_count": len(utterances)},
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
- Resolve identities globally across the full meeting; do not reset assumptions at public-witness handoffs or later Q&A rounds.
- Prefer direct self-introductions over all other evidence.
- Next strongest evidence: a chair, clerk, or counsel introduces the next speaker.
- If a witness or agency official is introduced and then answers several questions, keep that name until the transcript clearly switches speakers.
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
    label = _source_speaker_label(row)
    label_text = f" {label}:" if label else ""
    return f"[{index}] {sec_to_clock(row['t0'])}-{sec_to_clock(row['t1'])}:{label_text} {row['text']}"


def _extract_assignments(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("assignments") or result.get("ranges") or result.get("speaker_ranges")
    if not isinstance(raw, list):
        raise RuntimeError(f"Gemini speaker response lacks assignments: {result}")
    return _parse_assignments(raw)


def _parse_assignments(raw: list[Any]) -> list[dict[str, Any]]:
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
    *,
    overwrite: bool = False,
) -> None:
    for assignment in assignments:
        start = max(chunk_start, int(assignment["start_index"]))
        end = min(chunk_end - 1, int(assignment["end_index"]))
        if end < start:
            continue
        speaker = _clean_speaker(str(assignment["speaker"]))
        for index in range(start, end + 1):
            if overwrite or speaker_by_index[index] is None or speaker_by_index[index] == "UNKNOWN":
                speaker_by_index[index] = speaker


def _reconcile_chunk_assignments(
    utterances: list[dict[str, Any]],
    chunks: list[dict[str, int]],
    speaker_by_index: list[str | None],
    roster_csv: str,
    context: str,
    model: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = _reconciliation_prompt(utterances, chunks, speaker_by_index, roster_csv, context)
    result, meta = generate_json(prompt, model=model, temperature=0.0)
    mappings = _extract_label_mappings(result)
    overrides = _extract_range_overrides(result)
    _apply_label_mappings(speaker_by_index, utterances, mappings)
    _apply_assignments(speaker_by_index, overrides, 0, len(utterances), overwrite=True)
    return {
        "usage": meta.get("usage", {}),
        "estimated_cost_usd": meta.get("estimated_cost_usd"),
        "label_mappings": mappings,
        "range_overrides": overrides,
    }, meta


def _reconciliation_prompt(
    utterances: list[dict[str, Any]],
    chunks: list[dict[str, int]],
    speaker_by_index: list[str | None],
    roster_csv: str,
    context: str,
) -> str:
    label_table = _label_assignment_table(utterances, chunks, speaker_by_index)
    boundaries = _boundary_snippets(utterances, chunks, speaker_by_index)
    return f"""You are reconciling speaker names from overflow chunks of one NYC Council transcript.

The chunked naming pass has already assigned names. Your job is only to remove chunk-boundary identity drift and make one consistent mapping.

MEETING CONTEXT:
{context}

CURRENT COUNCIL ROSTER CSV:
{roster_csv}

PER-DIARIZED-LABEL ASSIGNMENTS BY CHUNK:
{label_table}

FIRST/LAST UTTERANCE SNIPPETS AROUND CHUNK BOUNDARIES:
{boundaries}

Rules:
- Prefer a global diarized-label mapping when the same source label consistently has the same identity.
- Use range_overrides only for obvious boundary handoff mistakes.
- Do not merge two public witnesses only because both are labeled Member of the Public.
- Keep "UNKNOWN" when the evidence is genuinely ambiguous.

Return JSON only:
{{
  "speaker_mappings": [
    {{"diarized_label": "SPEAKER_01", "speaker": "Julie Menin"}}
  ],
  "range_overrides": [
    {{"start_index": 1200, "end_index": 1220, "speaker": "Member of the Public - Name"}}
  ]
}}
"""


def _label_assignment_table(
    utterances: list[dict[str, Any]],
    chunks: list[dict[str, int]],
    speaker_by_index: list[str | None],
) -> str:
    label_counts: dict[str, dict[int, Counter[str]]] = {}
    for chunk_number, chunk in enumerate(chunks, start=1):
        for index in range(chunk["start"], chunk["end"]):
            label = _source_speaker_label(utterances[index])
            if not label:
                continue
            label_counts.setdefault(label, {}).setdefault(chunk_number, Counter()).update(
                [speaker_by_index[index] or "UNKNOWN"]
            )
    if not label_counts:
        return "No stable diarized speaker labels were present in the source utterances."

    lines = ["| diarized_label | per_chunk_assigned_names |", "|---|---|"]
    for label in sorted(label_counts):
        chunk_parts = []
        for chunk_number in sorted(label_counts[label]):
            top = ", ".join(
                f"{speaker} ({count})" for speaker, count in label_counts[label][chunk_number].most_common(3)
            )
            chunk_parts.append(f"chunk {chunk_number}: {top}")
        lines.append(f"| {label} | {'; '.join(chunk_parts)} |")
    return "\n".join(lines)


def _boundary_snippets(
    utterances: list[dict[str, Any]],
    chunks: list[dict[str, int]],
    speaker_by_index: list[str | None],
) -> str:
    if len(chunks) <= 1:
        return "No chunk boundaries."
    sections = []
    for chunk_number in range(1, len(chunks)):
        previous = chunks[chunk_number - 1]
        current = chunks[chunk_number]
        starts = [current["start"], previous["end"]]
        lines = [
            f"Boundary before chunk {chunk_number + 1}: previous chunk {previous['start']}-{previous['end'] - 1}, "
            f"next chunk {current['start']}-{current['end'] - 1}"
        ]
        seen: set[int] = set()
        for center in starts:
            start = max(0, center - BOUNDARY_SNIPPET_UTTERANCES)
            end = min(len(utterances), center + BOUNDARY_SNIPPET_UTTERANCES)
            for index in range(start, end):
                if index in seen:
                    continue
                seen.add(index)
                assigned = speaker_by_index[index] or "UNKNOWN"
                source = _source_speaker_label(utterances[index]) or "no_source_label"
                text = str(utterances[index]["text"])[:220]
                lines.append(f"[{index}] {sec_to_clock(utterances[index]['t0'])} {source} -> {assigned}: {text}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _extract_label_mappings(result: dict[str, Any]) -> list[dict[str, str]]:
    raw = result.get("speaker_mappings") or result.get("mappings") or []
    if not isinstance(raw, list):
        return []
    mappings = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("diarized_label") or item.get("label") or item.get("speaker_label") or "").strip()
        speaker = _clean_speaker(str(item.get("speaker") or item.get("name") or "UNKNOWN"))
        if label:
            mappings.append({"diarized_label": label, "speaker": speaker})
    return mappings


def _extract_range_overrides(result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("range_overrides") or result.get("overrides") or []
    if not isinstance(raw, list):
        return []
    return _parse_assignments(raw)


def _apply_label_mappings(
    speaker_by_index: list[str | None],
    utterances: list[dict[str, Any]],
    mappings: list[dict[str, str]],
) -> None:
    speaker_by_label = {item["diarized_label"]: _clean_speaker(item["speaker"]) for item in mappings}
    if not speaker_by_label:
        return
    for index, row in enumerate(utterances):
        label = _source_speaker_label(row)
        if label in speaker_by_label:
            speaker_by_index[index] = speaker_by_label[label]


def _source_speaker_label(row: dict[str, Any]) -> str | None:
    for key in SOURCE_SPEAKER_KEYS:
        value = row.get(key)
        if value is None:
            continue
        label = " ".join(str(value).split())
        if label and label.upper() not in {"UNKNOWN", "UNK"}:
            return label
    return None


def _clean_speaker(value: str) -> str:
    speaker = " ".join(value.replace("Council Member ", "").replace("Councilmember ", "").split())
    if speaker.lower() in {"unknown", "unk"}:
        return "UNKNOWN"
    return speaker or "UNKNOWN"
