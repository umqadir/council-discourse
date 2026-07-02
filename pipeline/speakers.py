from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import (
    normalize_utterances,
    read_json,
    read_jsonl,
    sec_to_clock,
    write_json,
    write_jsonl,
)
from .gemini import DEFAULT_MODEL, estimate_tokens, generate_json, pricing_details
from .models import Meeting
from .roster import current_roster, roster_csv_for_prompt
from .runlog import append_gemini_runlog

MAX_PROMPT_TOKENS = 700_000
CHUNK_TARGET_TOKENS = 550_000
CHUNK_OVERLAP = 240
BOUNDARY_SNIPPET_UTTERANCES = 5
MAX_SPEAKER_HINTS = 160
MAX_VERIFICATION_CANDIDATES = 120
VERIFICATION_MODEL = "gemini-3.1-flash-lite"
SOURCE_SPEAKER_KEYS = ("label", "diarized_speaker", "speaker_label", "speaker_id", "channel", "speaker")
LABELS_PER_PROMPT = 40
LABEL_SAMPLE_WINDOWS = 8
LABEL_WINDOW_RADIUS = 3
MAX_SAMPLE_LINE_CHARS = 220
INTRO_HINT_TERMS = ("MY NAME IS", "I'M ", "I AM ", "CALL THE", "PANEL", "FROM THE", "GO TO ZOOM")
GENERIC_SPEAKERS = {"UNKNOWN", "Council Staff", "Member of the Public", "Speaker"}
NAME_STOPWORDS = {
    "A",
    "ABOUT",
    "ACTUALLY",
    "AM",
    "AN",
    "AND",
    "ARE",
    "AS",
    "ASSOCIATE",
    "AT",
    "BEFORE",
    "BIG",
    "COUNSEL",
    "DEPUTY",
    "DIRECTOR",
    "EXECUTIVE",
    "FIRST",
    "FOR",
    "FROM",
    "I",
    "IM",
    "IN",
    "IS",
    "LEGAL",
    "MY",
    "NOT",
    "NOW",
    "OF",
    "ROLLING",
    "SORRY",
    "SURE",
    "THE",
    "TO",
    "VERY",
    "WITH",
}


def name_speakers_meeting(
    meeting: Meeting,
    model: str = DEFAULT_MODEL,
    *,
    input_path: Path | None = None,
    output_path: Path | None = None,
    meta_path: Path | None = None,
    runlog_stage: str = "name_speakers",
) -> Path:
    input_path = input_path or _speaker_input_path(meeting.meeting_dir)
    output = output_path or meeting.meeting_dir / "utterances-named.jsonl"
    meta_output = meta_path or meeting.meeting_dir / "name-speakers-meta.json"
    utterances = normalize_utterances(read_jsonl(input_path))
    if not utterances:
        raise RuntimeError(f"no utterances found in {input_path}")
    labels = _labels_in_order(utterances)
    if not labels:
        raise RuntimeError(f"no diarization labels found in {input_path}")

    roster_csv = roster_csv_for_prompt(meeting.event_date)
    context = _meeting_context(meeting)
    evidence = _build_label_evidence(utterances)
    mappings: list[dict[str, Any]] = []
    range_overrides: list[dict[str, Any]] = []
    chunk_records: list[dict[str, Any]] = []
    usage_totals: Counter[str] = Counter()
    elapsed_total = 0.0
    cost_total = 0.0

    chunks = _chunk_labels(labels, LABELS_PER_PROMPT)
    for chunk_number, chunk_labels in enumerate(chunks, start=1):
        prompt = _label_mapping_prompt(
            roster_csv=roster_csv,
            context=context,
            label_evidence=[evidence[label] for label in chunk_labels],
            label_count=len(labels),
        )
        result, meta = generate_json(prompt, model=model, temperature=0.1, thinking_level="low")
        elapsed_total += float(meta.get("elapsed_sec", 0))
        cost_total += float(meta.get("estimated_cost_usd") or 0)
        usage_totals.update({k: int(v) for k, v in meta.get("usage", {}).items() if isinstance(v, int)})
        chunk_mappings = _extract_label_mapping_records(result)
        if not chunk_mappings:
            raise RuntimeError(f"Gemini speaker response had no usable label mappings for chunk {chunk_number}")
        chunk_mappings = _with_unknown_mappings_for_missing_labels(chunk_labels, chunk_mappings)
        chunk_overrides = _extract_label_range_overrides(result)
        mappings.extend(chunk_mappings)
        range_overrides.extend(chunk_overrides)
        chunk_records.append(
            {
                "chunk": chunk_number,
                "labels": chunk_labels,
                "usage": meta.get("usage", {}),
                "estimated_cost_usd": meta.get("estimated_cost_usd"),
                "mappings": chunk_mappings,
                "range_overrides": chunk_overrides,
            }
        )

    named = join_label_mappings(utterances, mappings, range_overrides)

    verification, verification_meta = _verify_non_roster_speakers(named, meeting)
    elapsed_total += float(verification_meta.get("elapsed_sec", 0))
    cost_total += float(verification_meta.get("estimated_cost_usd") or 0)
    usage_totals.update({k: int(v) for k, v in verification_meta.get("usage", {}).items() if isinstance(v, int)})

    write_jsonl(output, named)
    meta_payload: dict[str, Any] = {
        "model": model,
        "input": str(input_path),
        "output": str(output),
        "utterance_count": len(utterances),
        "mode": "label_mapping",
        "label_count": len(labels),
        "labels": labels,
        "chunks": len(chunks),
        "chunk_ranges": [{"labels": chunk} for chunk in chunks],
        "elapsed_sec": round(elapsed_total, 3),
        "usage": dict(usage_totals),
        "estimated_cost_usd": round(cost_total, 6),
        "pricing": pricing_details(model),
        "mappings": mappings,
        "range_overrides": range_overrides,
    }
    if chunk_records:
        meta_payload["chunk_records"] = chunk_records
    meta_payload["verification"] = verification
    write_json(meta_output, meta_payload)
    append_gemini_runlog(
        meeting.meeting_dir,
        runlog_stage,
        model,
        meta_payload,
        {
            "mode": meta_payload["mode"],
            "chunks": len(chunks),
            "label_count": len(labels),
            "utterance_count": len(utterances),
        },
    )
    return output


def _speaker_input_path(meeting_dir: Path) -> Path:
    utterances = meeting_dir / "utterances-labeled.jsonl"
    if utterances.exists():
        return utterances
    raise RuntimeError(f"missing utterances-labeled.jsonl in {meeting_dir}; run diarize first")


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


def _labels_in_order(utterances: list[dict[str, Any]]) -> list[str]:
    labels = []
    seen: set[str] = set()
    for row in utterances:
        label = _source_speaker_label(row)
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def _chunk_labels(labels: list[str], chunk_size: int) -> list[list[str]]:
    return [labels[index : index + chunk_size] for index in range(0, len(labels), chunk_size)]


def _build_label_evidence(utterances: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indices_by_label: dict[str, list[int]] = {}
    total_duration = 0.0
    for index, row in enumerate(utterances):
        total_duration += max(0.0, float(row["t1"]) - float(row["t0"]))
        label = _source_speaker_label(row)
        if label:
            indices_by_label.setdefault(label, []).append(index)

    evidence: dict[str, dict[str, Any]] = {}
    for label, indices in indices_by_label.items():
        active_sec = sum(max(0.0, float(utterances[index]["t1"]) - float(utterances[index]["t0"])) for index in indices)
        sample_indices = _sample_indices_for_label(utterances, label, indices)
        evidence[label] = {
            "label": label,
            "utterance_count": len(indices),
            "total_speech_sec": round(active_sec, 1),
            "speech_share": round(active_sec / total_duration, 4) if total_duration else 0,
            "first_index": indices[0],
            "last_index": indices[-1],
            "first_activity": sec_to_clock(float(utterances[indices[0]]["t0"])),
            "last_activity": sec_to_clock(float(utterances[indices[-1]]["t0"])),
            "samples": [_label_sample_window(utterances, index) for index in sample_indices],
        }
    return evidence


def _sample_indices_for_label(
    utterances: list[dict[str, Any]],
    label: str,
    indices: list[int],
    limit: int = LABEL_SAMPLE_WINDOWS,
) -> list[int]:
    interesting = [index for index in indices if _is_interesting_label_sample(utterances, index, label)]
    anchors = [indices[0], indices[-1]]
    evenly_spaced = _evenly_spaced_indices(indices, limit)
    selected = _ordered_unique(interesting[: limit // 2] + anchors + evenly_spaced)
    if len(selected) < limit:
        selected = _ordered_unique(selected + indices)
    return sorted(selected[:limit])


def _is_interesting_label_sample(utterances: list[dict[str, Any]], index: int, label: str) -> bool:
    row = utterances[index]
    text = str(row.get("text") or "")
    upper = text.upper()
    if any(term in upper for term in INTRO_HINT_TERMS):
        return True
    previous = utterances[index - 1] if index > 0 else None
    next_row = utterances[index + 1] if index + 1 < len(utterances) else None
    if previous and _source_speaker_label(previous) != label:
        return True
    if next_row and _source_speaker_label(next_row) != label:
        return True
    nearby = " ".join(
        str(item.get("text") or "")
        for item in utterances[max(0, index - 2) : min(len(utterances), index + 3)]
        if _source_speaker_label(item) != label
    ).upper()
    return any(
        term in nearby
        for term in (
            "THANK YOU",
            "NEXT",
            "GO TO",
            "COUNCIL MEMBER",
            "CHAIR",
            "COMMISSIONER",
            "MR.",
            "MS.",
            "DOCTOR",
        )
    )


def _label_sample_window(utterances: list[dict[str, Any]], center: int) -> dict[str, Any]:
    start = max(0, center - LABEL_WINDOW_RADIUS)
    end = min(len(utterances), center + LABEL_WINDOW_RADIUS + 1)
    lines = []
    for index in range(start, end):
        row = utterances[index]
        marker = "TARGET" if index == center else "context"
        label = _source_speaker_label(row) or "NO_LABEL"
        text = " ".join(str(row.get("text") or "").split())[:MAX_SAMPLE_LINE_CHARS]
        lines.append(f"[{index}] {sec_to_clock(float(row['t0']))} {marker} {label}: {text}")
    return {
        "target_index": center,
        "target_time": sec_to_clock(float(utterances[center]["t0"])),
        "window": lines,
    }


def _evenly_spaced_indices(values: list[int], count: int) -> list[int]:
    if count >= len(values):
        return list(values)
    if count <= 1:
        return [values[len(values) // 2]]
    return [values[round(i * (len(values) - 1) / (count - 1))] for i in range(count)]


def _ordered_unique(values: list[int]) -> list[int]:
    seen: set[int] = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _label_mapping_prompt(
    roster_csv: str,
    context: str,
    label_evidence: list[dict[str, Any]],
    label_count: int,
) -> str:
    evidence_json = json.dumps(label_evidence, indent=2)
    return f"""You are mapping diarized speaker labels to real speaker names for a NYC Council transcript.

MEETING CONTEXT:
{context}

CURRENT COUNCIL ROSTER CSV (party may be blank if the source dataset lacks it):
{roster_csv}

DIARIZED LABEL EVIDENCE:
The full meeting has {label_count} labels. This prompt contains the labels below. Each sample window includes the target utterance and nearby dialogue from other labels.
{evidence_json}

TASK:
- Return one mapping for every label in this prompt.
- The name field is the final display speaker string for the transcript.
- For council members, use the roster name only, for example "Julie Menin".
- For public witnesses with a stated or introduced name, use "Member of the Public - Full Name".
- Allowed generic names are exactly "Council Staff", "Member of the Public", "Speaker", and "UNKNOWN".
- Use role and org for additional context such as Chair, Council Member, Commissioner, agency, advocacy group, or public witness organization.
- Prefer self-introductions over introductions by others. Next strongest: a chair, clerk, counsel, or committee staff introducing or addressing the speaker.
- Use speech content and roster context only when the dialogue supports it.
- Do not assign a whole label to a person if the samples show that the label is an impure roll-call/procedural bucket. Choose the dominant identity for the label, then add range_overrides for specific utterance ranges that clearly belong to another person.
- Keep confidence low when the evidence is ambiguous. Do not invent names.

Return JSON only:
{{
  "labels": [
    {{
      "label": "SPK_00",
      "name": "Julie Menin",
      "role": "Council Member",
      "org": "New York City Council",
      "confidence": "high",
      "reason": "brief evidence summary"
    }}
  ],
  "range_overrides": [
    {{
      "start_index": 1200,
      "end_index": 1204,
      "name": "Council Staff",
      "role": "Clerk",
      "org": "New York City Council",
      "confidence": "medium",
      "reason": "roll-call segment uses the wrong diarized label"
    }}
  ]
}}
"""


def _extract_label_mapping_records(result: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    raw: Any = result
    if isinstance(result, dict):
        raw = (
            result.get("labels")
            or result.get("label_mappings")
            or result.get("speaker_mappings")
            or result.get("mappings")
            or []
        )
    if not isinstance(raw, list):
        return []

    mappings = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("diarized_label") or item.get("speaker_label") or "").strip()
        if not label:
            continue
        name = _clean_speaker(str(item.get("name") or item.get("speaker") or item.get("display_name") or "UNKNOWN"))
        role = " ".join(str(item.get("role") or "").split())
        org = " ".join(str(item.get("org") or item.get("organization") or "").split())
        confidence_raw = item.get("confidence", "low")
        mappings.append(
            {
                "label": label,
                "name": name,
                "speaker": _speaker_from_mapping(name, role),
                "role": role,
                "org": org,
                "confidence": _confidence_value(confidence_raw),
                "confidence_label": str(confidence_raw),
                "reason": str(item.get("reason") or item.get("evidence") or "").strip()[:500],
            }
        )
    return mappings


def _with_unknown_mappings_for_missing_labels(
    labels: list[str],
    mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_label = {str(item["label"]): item for item in mappings}
    for label in labels:
        if label in by_label:
            continue
        by_label[label] = {
            "label": label,
            "name": "UNKNOWN",
            "speaker": "UNKNOWN",
            "role": "",
            "org": "",
            "confidence": 0.2,
            "confidence_label": "missing",
            "reason": "Gemini did not return this label.",
        }
    return [by_label[label] for label in labels]


def _extract_label_range_overrides(result: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    raw: Any = []
    if isinstance(result, dict):
        raw = result.get("range_overrides") or result.get("overrides") or []
    if not isinstance(raw, list):
        return []

    overrides = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start_index", item.get("start", item.get("from")))
        end = item.get("end_index", item.get("end", item.get("to", start)))
        if start is None:
            continue
        name = _clean_speaker(str(item.get("name") or item.get("speaker") or item.get("display_name") or "UNKNOWN"))
        role = " ".join(str(item.get("role") or "").split())
        org = " ".join(str(item.get("org") or item.get("organization") or "").split())
        confidence_raw = item.get("confidence", "medium")
        overrides.append(
            {
                "start_index": int(start),
                "end_index": int(end) if end is not None else int(start),
                "name": name,
                "speaker": _speaker_from_mapping(name, role),
                "role": role,
                "org": org,
                "confidence": _confidence_value(confidence_raw),
                "confidence_label": str(confidence_raw),
                "reason": str(item.get("reason") or item.get("evidence") or "").strip()[:500],
            }
        )
    return overrides


def join_label_mappings(
    utterances: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    range_overrides: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    mapping_by_label = {str(item.get("label")): item for item in mappings if item.get("label")}
    named = []
    for row in normalize_utterances(utterances):
        label = _source_speaker_label(row)
        mapping = mapping_by_label.get(label or "")
        speaker = _clean_speaker(str(mapping.get("speaker") if mapping else "UNKNOWN"))
        confidence = _confidence_value(mapping.get("confidence") if mapping else "low")
        named.append(
            {
                "t0": row["t0"],
                "t1": row["t1"],
                "text": row["text"],
                "speaker": speaker,
                "confidence": confidence,
            }
        )

    for override in range_overrides or []:
        start = max(0, int(override.get("start_index", 0)))
        end = min(len(named) - 1, int(override.get("end_index", start)))
        if end < start:
            continue
        speaker = _clean_speaker(str(override.get("speaker") or override.get("name") or "UNKNOWN"))
        confidence = _confidence_value(override.get("confidence", "medium"))
        for index in range(start, end + 1):
            named[index]["speaker"] = speaker
            named[index]["confidence"] = confidence
    return named


def _speaker_from_mapping(name: str, role: str) -> str:
    speaker = _clean_speaker(name)
    if speaker.upper() in {"UNKNOWN", "UNK"} or speaker in GENERIC_SPEAKERS:
        return speaker
    if re.match(r"^Member\s+of\s+the\s+Public\s*-", speaker, flags=re.I):
        return speaker
    role_lower = role.lower()
    if any(term in role_lower for term in ("member of the public", "public witness", "testifier")):
        return f"Member of the Public - {speaker}"
    return speaker


def _confidence_value(value: Any) -> float:
    if isinstance(value, int | float):
        return round(max(0.0, min(1.0, float(value))), 3)
    text = str(value or "").strip().lower()
    if text in {"very high", "high", "certain"}:
        return 0.9
    if text in {"medium", "moderate"}:
        return 0.65
    if text in {"low", "weak"}:
        return 0.35
    if text in {"unknown", "missing", "none"}:
        return 0.2
    return 0.5


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
    speaker_hints = _speaker_hints(utterances, offset)
    return f"""You are assigning speaker names to every utterance in a NYC Council transcript.

MEETING CONTEXT:
{context}

CURRENT COUNCIL ROSTER CSV (party may be blank if the source dataset lacks it):
{roster_csv}

AUTO-EXTRACTED SPEAKER HINTS (noisy; verify against the transcript before using):
{speaker_hints}

INFERENCE RULES:
- Assign one speaker to every utterance index in the transcript.
- Resolve identities globally across the full meeting; do not reset assumptions at public-witness handoffs or later Q&A rounds.
- Prefer direct self-introductions over all other evidence.
- Next strongest evidence: a chair, clerk, or counsel introduces the next speaker.
- If a witness or agency official is introduced and then answers several questions, keep that name until the transcript clearly switches speakers.
- If a public witness says "my name is" or "I'm Name", assign "Member of the Public - Name" rather than UNKNOWN.
- For public witness panels, an introduction such as "Andrew Rigie, Rob Bookman, Max Bookman" means later prepared remarks can switch between those panelists without a new chair prompt. Do not carry the first witness's name over a different named panelist's prepared remarks.
- If ASR renders a public witness name inconsistently, prefer the spelling from the chair's panel introduction over a phonetically garbled self-introduction.
- Use content, procedural context, roll-call order, and roster names only when the text supports it.
- Do not invent people. If a public witness states a name, use "Member of the Public - Name".
- Allowed fallbacks are exactly "Council Staff", "Member of the Public", "Member of the Public - Name", and "UNKNOWN".
- For council members, use the roster name only, for example "Julie Menin", not titles.
- Return a compact ordered list of speaker changes. Each segment starts at start_index and continues until the next segment's start_index minus one. The final segment covers through the last transcript index.
- Include a new segment at every speaker change. Cover every index exactly once by inference from the ordered starts.

TRANSCRIPT:
<transcript>
{transcript}
</transcript>

Return JSON only:
{{
  "segments": [
    {{"start_index": 0, "speaker": "Council Staff"}},
    {{"start_index": 22, "speaker": "Julie Menin"}}
  ]
}}
"""


def _transcript_line(index: int, row: dict[str, Any]) -> str:
    label = _source_speaker_label(row)
    label_text = f" {label}:" if label else ""
    return f"[{index}] {sec_to_clock(row['t0'])}-{sec_to_clock(row['t1'])}:{label_text} {row['text']}"


def _extract_assignments(result: dict[str, Any] | list[Any], default_end: int | None = None) -> list[dict[str, Any]]:
    if isinstance(result, list):
        raw = result
    else:
        raw = result.get("segments") or result.get("assignments") or result.get("ranges") or result.get(
            "speaker_ranges"
        )
    if not isinstance(raw, list):
        raise RuntimeError(f"Gemini speaker response lacks assignments: {result}")
    return _parse_assignments(raw, default_end=default_end)


def _parse_assignments(raw: list[Any], default_end: int | None = None) -> list[dict[str, Any]]:
    assignments = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start_index", item.get("start", item.get("from")))
        end = item.get("end_index", item.get("end", item.get("to")))
        speaker = str(item.get("speaker") or item.get("name") or "UNKNOWN").strip()
        if start is None:
            continue
        assignments.append(
            {
                "start_index": int(start),
                "end_index": int(end) if end is not None else None,
                "speaker": _clean_speaker(speaker),
            }
        )
    if default_end is None:
        for assignment in assignments:
            if assignment["end_index"] is None or int(assignment["end_index"]) < int(assignment["start_index"]):
                assignment["end_index"] = assignment["start_index"]
        return assignments

    assignments.sort(key=lambda assignment: int(assignment["start_index"]))
    for index, assignment in enumerate(assignments):
        next_start = int(assignments[index + 1]["start_index"]) if index + 1 < len(assignments) else default_end
        if assignment["end_index"] is None or int(assignment["end_index"]) < int(assignment["start_index"]):
            assignment["end_index"] = max(int(assignment["start_index"]), next_start - 1)
        else:
            assignment["end_index"] = min(int(assignment["end_index"]), default_end - 1)
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
    result, meta = generate_json(prompt, model=model, temperature=0.0, thinking_level="low")
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


def _verify_non_roster_speakers(
    named: list[dict[str, Any]],
    meeting: Meeting,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = _verification_candidates(named, meeting)
    verification: dict[str, Any] = {
        "enabled": True,
        "model": VERIFICATION_MODEL,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "results": [],
        "applied_corrections": [],
    }
    if not candidates:
        return verification, {"elapsed_sec": 0.0, "usage": {}, "estimated_cost_usd": 0.0}

    prompt = _verification_prompt(meeting, candidates)
    result, meta = generate_json(
        prompt,
        model=VERIFICATION_MODEL,
        temperature=0.0,
        max_output_tokens=16_384,
        tools=[{"google_search": {}}],
        response_mime_type=None,
    )
    corrections = _extract_verification_results(result)
    verification["results"] = corrections
    if meta.get("grounding"):
        verification["grounding"] = meta["grounding"]

    candidates_by_id = {item["id"]: item for item in candidates}
    candidates_by_speaker = {item["speaker"]: item for item in candidates}
    speaker_map: dict[str, str] = {}
    for correction in corrections:
        candidate = candidates_by_id.get(str(correction.get("id") or ""))
        if candidate is None:
            candidate = candidates_by_speaker.get(_clean_speaker(str(correction.get("input_speaker") or "")))
        if candidate is None:
            continue
        corrected = _corrected_speaker(candidate, correction)
        if not corrected or corrected == candidate["speaker"]:
            continue
        confidence = str(correction.get("confidence") or "").strip().lower()
        if confidence not in {"high"}:
            continue
        speaker_map[candidate["speaker"]] = corrected
        verification["applied_corrections"].append(
            {
                "id": candidate["id"],
                "before": candidate["speaker"],
                "after": corrected,
                "confidence": confidence,
                "evidence": str(correction.get("evidence") or correction.get("reason") or "").strip()[:500],
            }
        )

    if speaker_map:
        for row in named:
            speaker = _clean_speaker(str(row.get("speaker") or "UNKNOWN"))
            if speaker in speaker_map:
                row["speaker"] = speaker_map[speaker]
    return verification, meta


def _verification_candidates(named: list[dict[str, Any]], meeting: Meeting) -> list[dict[str, Any]]:
    roster_keys = {_name_key(row.get("name", "")) for row in current_roster(meeting.event_date)}
    roster_keys.discard("")
    by_speaker: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, row in enumerate(named):
        speaker = _clean_speaker(str(row.get("speaker") or "UNKNOWN"))
        if not _needs_verification(speaker, roster_keys):
            continue
        if speaker not in by_speaker:
            context_before = _speaker_context_before(named, index)
            by_speaker[speaker] = {
                "id": f"v{len(order) + 1:03d}",
                "speaker": speaker,
                "name": _speaker_base_name(speaker),
                "first_index": index,
                "first_timestamp": sec_to_clock(float(row.get("t0") or 0)),
                "utterance_count": 0,
                "role_org_hint": "",
                "quote_snippet": "",
                "context_before": context_before,
            }
            order.append(speaker)
        item = by_speaker[speaker]
        item["utterance_count"] += 1
        text = " ".join(str(row.get("text") or "").split())
        window = _speaker_forward_window(named, index, speaker)
        if text and (
            not item["quote_snippet"]
            or (_looks_like_intro(text) and _text_mentions_name(text, str(item["name"])))
        ):
            item["quote_snippet"] = (window or text)[:600]
        if not item["role_org_hint"]:
            item["role_org_hint"] = _role_org_hint(speaker, window or text)
    return [by_speaker[speaker] for speaker in order[:MAX_VERIFICATION_CANDIDATES]]


def _needs_verification(speaker: str, roster_keys: set[str]) -> bool:
    if speaker in GENERIC_SPEAKERS or speaker.upper() in {"UNKNOWN", "UNK"}:
        return False
    base = _speaker_base_name(speaker)
    if len(base.split()) < 2:
        return False
    return _name_key(base) not in roster_keys


def _speaker_base_name(speaker: str) -> str:
    value = re.sub(r"^Member\s+of\s+the\s+Public\s*-\s*", "", speaker, flags=re.I)
    value = re.sub(r"^Council\s+Staff\s*-\s*", "", value, flags=re.I)
    value = re.sub(r"^(Dr|Mr|Ms|Mrs|Mx)\.?\s+", "", value, flags=re.I)
    return " ".join(value.split())


def _name_key(value: str) -> str:
    value = _speaker_base_name(str(value))
    return " ".join(re.findall(r"[a-z]+", value.lower()))


def _speaker_context_before(named: list[dict[str, Any]], index: int) -> str:
    lines = []
    for row in named[max(0, index - 3) : index]:
        speaker = str(row.get("speaker") or "UNKNOWN")
        text = " ".join(str(row.get("text") or "").split())
        if text:
            lines.append(f"{speaker}: {text[:220]}")
    return "\n".join(lines)


def _speaker_forward_window(named: list[dict[str, Any]], index: int, speaker: str, limit: int = 5) -> str:
    lines = []
    for row in named[index : min(len(named), index + limit)]:
        if _clean_speaker(str(row.get("speaker") or "UNKNOWN")) != speaker:
            break
        text = " ".join(str(row.get("text") or "").split())
        if text:
            lines.append(text)
    return " ".join(lines)


def _looks_like_intro(text: str) -> bool:
    upper = text.upper()
    return bool(
        re.search(r"\bMY NAME IS\s+[A-Z]", upper)
        or re.search(r"\bI(?:'M| AM)\s+[A-Z][A-Z'.-]+\s+[A-Z][A-Z'.-]+", upper)
        or re.search(r"\b(?:FROM|REPRESENTING|ON BEHALF OF)\s+[A-Z]", upper)
    )


def _text_mentions_name(text: str, name: str) -> bool:
    text_tokens = set(re.findall(r"[A-Z][A-Z'.-]*", text.upper()))
    name_tokens = [token for token in re.findall(r"[A-Z][A-Z'.-]*", name.upper()) if len(token) > 2]
    return bool(name_tokens and name_tokens[-1] in text_tokens)


def _role_org_hint(speaker: str, text: str) -> str:
    speaker_tail = ""
    if " - " in speaker:
        speaker_tail = speaker.split(" - ", 1)[1]
    for pattern in (
        r"\b(?:from|with|at|representing|on behalf of)\s+([^.;:]{3,100})",
        r"\b(?:director|president|chair|commissioner|counsel|attorney)\s+(?:of|for|at)\s+([^.;:]{3,100})",
    ):
        match = re.search(pattern, text, flags=re.I)
        if match:
            return " ".join(match.group(1).split())[:120]
    return speaker_tail[:120]


def _verification_prompt(meeting: Meeting, candidates: list[dict[str, Any]]) -> str:
    payload = json.dumps(candidates, indent=2)
    return f"""You are verifying noisy ASR speaker names from a NYC Council meeting transcript.

Use the built-in Google Search grounding tool to verify or correct spelling for the listed non-roster speakers. These are public witnesses, agency staff, advocates, or other non-Council speakers. The initial names may be phonetic ASR spellings.

Meeting:
- key: {meeting.meeting_key}
- body: {meeting.body_name or "NYC Council"}
- date: {meeting.event_date or "unknown"}
- time: {meeting.event_time or "unknown"}

Rules:
- Correct only spelling/name form for the same person supported by search evidence and the transcript context.
- Do not replace a person with an organization, agency, or title.
- Do not infer a different person who merely has a similar name.
- If evidence is weak, keep the original and use confidence "low".
- For public witness names, return corrected_speaker in the same style: "Member of the Public - Correct Name".
- Return one result for every input id.

Candidates:
{payload}

Return JSON only:
{{
  "results": [
    {{
      "id": "v001",
      "input_speaker": "Member of the Public - Jeanne Ryan",
      "corrected_speaker": "Member of the Public - Jean Ryan",
      "verified_name": "Jean Ryan",
      "confidence": "high",
      "evidence": "Brief search-grounded reason, including role/org match when available"
    }}
  ]
}}
"""


def _extract_verification_results(result: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    raw = result
    if isinstance(result, dict):
        raw = result.get("results") or result.get("corrections") or result.get("verified_names") or []
    if not isinstance(raw, list):
        return []
    output = []
    for item in raw:
        if isinstance(item, dict):
            output.append(item)
    return output


def _corrected_speaker(candidate: dict[str, Any], correction: dict[str, Any]) -> str | None:
    raw = str(
        correction.get("corrected_speaker")
        or correction.get("speaker")
        or correction.get("corrected")
        or ""
    ).strip()
    name = str(
        correction.get("verified_name")
        or correction.get("corrected_name")
        or correction.get("name")
        or ""
    ).strip()
    original = str(candidate["speaker"])
    if raw:
        corrected = _clean_speaker(raw)
    elif name:
        corrected = _speaker_with_original_style(original, name)
    else:
        return None
    if corrected.upper() in {"UNKNOWN", "UNK"} or corrected in GENERIC_SPEAKERS:
        return None
    if len(_speaker_base_name(corrected).split()) < 2:
        return None
    return corrected


def _speaker_with_original_style(original: str, corrected_name: str) -> str:
    name = " ".join(corrected_name.split())
    if re.match(r"^Member\s+of\s+the\s+Public\s*-", original, flags=re.I):
        return f"Member of the Public - {name}"
    return name


def _source_speaker_label(row: dict[str, Any]) -> str | None:
    for key in SOURCE_SPEAKER_KEYS:
        value = row.get(key)
        if value is None:
            continue
        label = " ".join(str(value).split())
        if label and label.upper() not in {"UNKNOWN", "UNK"}:
            return label
    return None


def _speaker_hints(utterances: list[dict[str, Any]], offset: int) -> str:
    hints: list[str] = []
    seen: set[str] = set()
    for local_index, row in enumerate(utterances):
        text = str(row.get("text") or "")
        upper = text.upper()
        if not any(term in upper for term in INTRO_HINT_TERMS):
            continue
        window_rows = utterances[local_index : min(len(utterances), local_index + 4)]
        window = " ".join(str(item.get("text") or "") for item in window_rows)
        names = _intro_names(window)
        if names:
            for name in names:
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                hints.append(
                    f"[{offset + local_index}] {sec_to_clock(row['t0'])}: possible named speaker {name} | {window[:220]}"
                )
                if len(hints) >= MAX_SPEAKER_HINTS:
                    return "\n".join(hints)
        elif any(term in upper for term in ("CALL THE", "PANEL", "GO TO ZOOM")):
            key = window[:120].lower()
            if key not in seen:
                seen.add(key)
                hints.append(
                    f"[{offset + local_index}] {sec_to_clock(row['t0'])}: possible panel introduction | {window[:220]}"
                )
                if len(hints) >= MAX_SPEAKER_HINTS:
                    return "\n".join(hints)
    return "\n".join(hints) if hints else "No speaker self-introduction hints found."


def _intro_names(text: str) -> list[str]:
    patterns = [
        r"\bMY NAME IS\s+([A-Z][A-Z'.-]*(?:\s+[A-Z][A-Z'.-]*){0,4})",
        r"\b(?:HI|HELLO|GOOD MORNING|THANK YOU)\.?\s+I'M\s+([A-Z][A-Z'.-]*(?:\s+[A-Z][A-Z'.-]*){1,4})",
    ]
    names = []
    upper = text.upper()
    for pattern in patterns:
        for match in re.finditer(pattern, upper):
            name = _clean_intro_name(match.group(1))
            if name:
                names.append(name)
    return names


def _clean_intro_name(value: str) -> str | None:
    tokens = []
    for token in re.findall(r"[A-Z][A-Z'.-]*", value.upper()):
        stripped = token.strip("'.-").replace("'", "")
        if stripped in NAME_STOPWORDS:
            break
        tokens.append(stripped)
        if len(tokens) >= 4:
            break
    if len(tokens) < 2:
        return None
    return " ".join(token.capitalize() for token in tokens)


def _clean_speaker(value: str) -> str:
    speaker = " ".join(value.replace("Council Member ", "").replace("Councilmember ", "").split())
    if speaker.lower() in {"unknown", "unk"}:
        return "UNKNOWN"
    return speaker or "UNKNOWN"
