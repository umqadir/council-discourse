"""Combined single-pass experiment: speakers + chapters + summaries in one call.

Brief item 2. One structured-output pass over the full transcript emits speaker
segments, chapters, and a meeting summary together, replacing the separate naming
(chunked evidence windows) and chaptering (full transcript) passes with a single
full-transcript read. glm-5.2 only, both benchmarks. We score the naming output
with the same eval as the matrix (07) and the chapters with the same eval (05),
and log real billed prompt/completion tokens and cost.

Watch for: task interference (naming quality drops when the model is also
chaptering) and output truncation (a big transcript plus segments plus chapters
can exceed max_tokens).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.artifacts import normalize_utterances, read_json, read_jsonl, sec_to_clock
from pipeline.gemini import generate_json
from pipeline.speakers import _parse_assignments, _source_speaker_label
from pipeline.utils import load_dotenv

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "z-ai/glm-5.2"
BENCHMARKS = {
    "transportation": ROOT / "data" / "benchmark" / "2025-04-23-transportation",
    "stated": ROOT / "data" / "benchmark" / "2025-04-24-stated",
}
OUT_DIR = ROOT / "experiments" / "out"
COMBINED_SCHEMA = {
    "type": "object",
    "properties": {
        "meeting_summary": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "speaker_segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_index": {"type": "integer"},
                    "speaker": {"type": "string"},
                },
                "required": ["start_index", "speaker"],
                "additionalProperties": True,
            },
        },
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "start_index": {"type": "integer"},
                    "type": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["type", "title", "summary"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["speaker_segments", "chapters"],
    "additionalProperties": True,
}


def main() -> int:
    args = parse_args()
    load_dotenv()
    matrix = load_module("matrix10", ROOT / "experiments" / "10_model_matrix.py")
    eval07 = load_module("eval07c", ROOT / "experiments" / "07_eval_speaker_naming.py")
    compare05 = load_module("compare05c", ROOT / "experiments" / "05_compare_chapters.py")

    benchmarks = {k: v for k, v in BENCHMARKS.items() if not args.benchmark or k in args.benchmark}
    rows: list[dict[str, Any]] = []
    for benchmark, bench_dir in benchmarks.items():
        print(f"RUN combined benchmark={benchmark} model={MODEL}", flush=True)
        rows.append(run_combined(benchmark, bench_dir, matrix, eval07, compare05))
        r = rows[-1]
        print(
            f"DONE combined benchmark={benchmark} status={r['status']} "
            f"cost=${r.get('cost_usd', 0):.6f} same={r.get('same_person_accuracy')} "
            f"f1_30={r.get('f1_30')} err={r.get('error', '')}",
            flush=True,
        )
    write_summary(rows)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="append", choices=sorted(BENCHMARKS), default=[])
    return parser.parse_args()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_combined(benchmark: str, bench_dir: Path, matrix, eval07, compare05) -> dict[str, Any]:
    row: dict[str, Any] = {"benchmark": benchmark, "model": MODEL, "status": "FAIL"}
    started = time.monotonic()
    out_dir = bench_dir / "matrix"
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters_path = out_dir / "combined-glm-5.2-chapters.json"
    named_path = out_dir / "combined-glm-5.2-named.jsonl"
    raw_path = out_dir / "combined-glm-5.2-raw.json"
    try:
        input_path = bench_dir / "utterances-voxtral-labeled.jsonl"
        utterances = normalize_utterances(read_jsonl(input_path))
        meeting = matrix.meeting_from_dir(bench_dir)
        prompt = combined_prompt(meeting, utterances, compare05)
        result, meta = generate_json(
            prompt,
            model=MODEL,
            temperature=0.2,
            max_output_tokens=65536,
            base_url=OPENROUTER_BASE_URL,
            api_key_env="OPENROUTER_API_KEY",
            json_schema=COMBINED_SCHEMA,
        )
        raw_path.write_text(json.dumps(result, indent=2))

        # --- naming: map speaker_segments to per-utterance speakers ---
        named = segments_to_named(result, utterances)
        write_jsonl_local(named_path, named)
        refs = eval07._read_citymeetings_references(bench_dir)
        naming = matrix.naming_metrics(named, refs, eval07)
        row.update({f"naming_{k}" if k in {"references", "matched", "scored"} else k: v for k, v in naming.items()})

        # --- chaptering: write in matrix-compatible shape, then score with 05 ---
        chapters_meta = build_chapters_meta(result, utterances, meeting, matrix, meta)
        from pipeline.artifacts import write_json as _wj

        _wj(chapters_path, chapters_meta)
        chap_metrics, _ = matrix.chaptering_metrics_report(benchmark, bench_dir, chapters_path, compare05)
        row.update(chap_metrics)

        row.update(matrix.cost_fields(meta))
        row["cached_prompt_tokens"] = meta.get("cached_prompt_tokens")
        row["truncated"] = detect_truncation(result, meta)
        row["elapsed_sec"] = round(time.monotonic() - started, 1)
        row["status"] = "PASS"
    except Exception as exc:
        import traceback

        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc()[-1500:]
    return row


def combined_prompt(meeting, utterances: list[dict[str, Any]], compare05) -> str:
    from pipeline.chapterize import _meeting_context, _meeting_type, _meeting_type_rules, CHAPTER_TYPE_ORDER
    from pipeline.speakers import roster_csv_for_prompt

    context = _meeting_context(meeting)
    meeting_type = _meeting_type(meeting)
    rules = _meeting_type_rules(meeting_type)
    chapter_types = ", ".join(CHAPTER_TYPE_ORDER)
    roster_csv = roster_csv_for_prompt(meeting.event_date)
    transcript = "\n".join(_line(index, row) for index, row in enumerate(utterances))
    return f"""You are processing a NYC Council meeting transcript for a public website. In a SINGLE pass you must do TWO jobs at once: (1) assign speaker names to every utterance, and (2) divide the meeting into navigable chapters with summaries.

MEETING CONTEXT:
{context}

MEETING TYPE:
{meeting_type}

CURRENT COUNCIL ROSTER CSV (party may be blank if the source dataset lacks it):
{roster_csv}

TRANSCRIPT (each line is "[index] [H:MM:SS] LABEL: text"; LABEL is a diarized speaker label, names are unknown, ASR errors are expected):
<transcript>
{transcript}
</transcript>

JOB 1 - SPEAKER SEGMENTS:
- Return an ordered list of speaker_segments. Each segment starts at start_index and runs until the next segment's start_index. Cover every index exactly once.
- Include a new segment at every speaker change.
- Resolve identities globally; do not reset at public-witness handoffs or later Q&A rounds.
- Prefer direct self-introductions; next strongest, a chair/clerk/counsel introducing the next speaker.
- For council members, use the roster name only, e.g. "Julie Menin", not titles.
- If a public witness states a name, use "Member of the Public - Name". Allowed fallbacks are exactly "Council Staff", "Member of the Public", "Member of the Public - Name", and "UNKNOWN". Do not invent people.

JOB 2 - CHAPTERS:
Divide the ENTIRE meeting into consecutive, non-overlapping chapters. Rules:
- A chapter answers "one thing happened here." Prefer 1-5 minute chapters; shorter for votes, roll calls, outcomes. Avoid micro-chapters for transition-only lines.
{rules}
- Cover the whole meeting; no gaps. First chapter starts at the meeting's first speech.
- chapter type: one of {chapter_types}.
- title: a specific headline naming who and what. Never generic like "Opening remarks continued".
- summary: 2-4 sentences, concrete, naming speakers and specifics.
- start_index: the utterance index where the chapter begins.
- meeting_summary: 3 concise bullets as strings. tags: choose all applicable from HEARING, VOTE, STATED_MEETING, LAND_USE.

Return JSON only:
{{
  "meeting_summary": ["...", "...", "..."],
  "tags": ["HEARING"],
  "speaker_segments": [
    {{"start_index": 0, "speaker": "Council Staff"}},
    {{"start_index": 22, "speaker": "Julie Menin"}}
  ],
  "chapters": [
    {{"start_index": 12, "type": "REMARKS", "title": "...", "summary": "..."}}
  ]
}}
"""


def _line(index: int, row: dict[str, Any]) -> str:
    label = _source_speaker_label(row) or "NO_LABEL"
    return f"[{index}] [{sec_to_clock(row['t0'])}] {label}: {row['text']}"


def segments_to_named(result: dict[str, Any], utterances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = result.get("speaker_segments") or result.get("segments") or []
    assignments = _parse_assignments(raw, default_end=len(utterances))
    speaker_by_index: list[str] = ["UNKNOWN"] * len(utterances)
    for a in assignments:
        start = max(0, int(a["start_index"]))
        end = min(len(utterances) - 1, int(a["end_index"]))
        for i in range(start, end + 1):
            speaker_by_index[i] = str(a["speaker"])
    named = []
    for i, row in enumerate(utterances):
        named.append(
            {
                "t0": row["t0"],
                "t1": row["t1"],
                "text": row["text"],
                "speaker": speaker_by_index[i],
                "confidence": 0.6,
            }
        )
    return named


def build_chapters_meta(result, utterances, meeting, matrix, meta) -> dict[str, Any]:
    from pipeline.chapterize import _postprocess_chapters, _resolve_chapters

    chapters = _resolve_chapters(result, utterances, meeting.duration_seconds)
    chapters = _postprocess_chapters(chapters, utterances, meeting, meeting.duration_seconds)
    return {
        "model": MODEL,
        "provider": meta.get("provider"),
        "usage": meta.get("usage", {}),
        "estimated_cost_usd": meta.get("estimated_cost_usd"),
        "exact_cost_usd": meta.get("exact_cost_usd"),
        "cost_source": meta.get("cost_source"),
        "chapters": chapters,
    }


def detect_truncation(result: dict[str, Any], meta: dict[str, Any]) -> bool:
    # heuristic: last chapter/segment missing required fields, or no chapters at all
    chapters = result.get("chapters") or []
    segments = result.get("speaker_segments") or []
    if not chapters or not segments:
        return True
    last = chapters[-1]
    return not (last.get("title") and last.get("summary"))


def write_jsonl_local(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def write_summary(rows: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "combined-pass.json").write_text(json.dumps(rows, indent=2))
    lines = ["# Combined Single-Pass (glm-5.2)", ""]
    header = ["benchmark", "status", "cost", "same-person", "strict", "chapters", "F1@30", "type-agree", "cached", "trunc"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for r in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r.get("benchmark", "")),
                    str(r.get("status", "")),
                    f"${float(r.get('cost_usd', 0) or 0):.6f}",
                    _pct(r.get("same_person_accuracy")),
                    _pct(r.get("strict_accuracy")),
                    str(r.get("generated_chapters", "")),
                    _pct(r.get("f1_30")),
                    _pct(r.get("type_agreement")),
                    str(r.get("cached_prompt_tokens", "")),
                    str(r.get("truncated", "")),
                ]
            )
            + " |"
        )
        if r.get("error"):
            lines.append(f"\n_error {r['benchmark']}: {r['error']}_\n")
    (OUT_DIR / "combined-pass.md").write_text("\n".join(lines) + "\n")


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.1f}%" if isinstance(value, (int, float)) else ""


if __name__ == "__main__":
    raise SystemExit(main())
