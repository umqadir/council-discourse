from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.artifacts import read_json, read_jsonl, write_json
from pipeline.chapterize import chapterize_meeting
from pipeline.models import Meeting
from pipeline.speakers import name_speakers_meeting
from pipeline.utils import load_dotenv, safe_key

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
BUDGET_USD = 8.0
BENCHMARKS = {
    "transportation": ROOT / "data" / "benchmark" / "2025-04-23-transportation",
    "stated": ROOT / "data" / "benchmark" / "2025-04-24-stated",
}
CANDIDATES = [
    ("deepseek/deepseek-v4-flash", None),
    ("deepseek/deepseek-v4-pro", None),
    ("qwen/qwen3.6-plus", None),
    ("openai/gpt-5.4-mini", None),
    ("z-ai/glm-5.2", "moonshotai/kimi-k2.6"),
    ("google/gemini-3.1-flash-lite", None),
]
SUMMARY_PATH = ROOT / "experiments" / "out" / "model-matrix-summary.md"
BASELINE_NOTES = [
    ["naming", "transportation", "gemini-3.5-flash", "same-person 87.6%; strict in existing report"],
    ["naming", "stated", "gemini-3.5-flash", "same-person 95.9%; strict in existing report"],
    ["chaptering", "transportation", "gemini-3.5-flash", "F1@30s 73.2%"],
    ["chaptering", "stated", "gemini-3.5-flash", "F1@30s 84.6%; type agreement 88.9%"],
]


@dataclass(frozen=True)
class ModelConfig:
    requested_id: str
    model_id: str
    slug: str
    pricing: dict[str, Any]


class BudgetStop(RuntimeError):
    pass


def main() -> int:
    args = parse_args()
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    eval07 = load_module("eval07", ROOT / "experiments" / "07_eval_speaker_naming.py")
    compare05 = load_module("compare05", ROOT / "experiments" / "05_compare_chapters.py")

    with httpx.Client(timeout=60) as client:
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        start_usage = args.budget_start_usage
        if start_usage is None:
            start_usage = openrouter_total_usage(client, headers)
        models = filter_models(resolve_models(client, headers), args.model)
        benchmarks = filter_benchmarks(args.benchmark)
        rows: list[dict[str, Any]] = []
        print(f"OpenRouter total_usage_start=${start_usage:.6f}", flush=True)
        print("models=" + ", ".join(model.model_id for model in models), flush=True)
        try:
            for model in models:
                for benchmark, bench_dir in benchmarks.items():
                    ensure_budget(client, headers, start_usage)
                    if args.task in {"all", "naming"}:
                        rows.append(run_naming(model, benchmark, bench_dir, eval07, reuse_existing=args.reuse_existing))
                        print_progress(client, headers, start_usage, rows[-1])
                        ensure_budget(client, headers, start_usage)
                    if args.task in {"all", "chaptering"}:
                        rows.append(
                            run_chaptering(model, benchmark, bench_dir, compare05, reuse_existing=args.reuse_existing)
                        )
                        print_progress(client, headers, start_usage, rows[-1])
        except BudgetStop as exc:
            print(str(exc), flush=True)
        finally:
            end_usage = openrouter_total_usage(client, headers)
            write_summary(rows, start_usage=start_usage, end_usage=end_usage, models=models)
            print(f"OpenRouter total_usage_end=${end_usage:.6f}", flush=True)
            print(f"OpenRouter run_delta=${max(0.0, end_usage - start_usage):.6f}", flush=True)
            print(f"summary={SUMMARY_PATH}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OpenRouter model matrix for speaker naming and chaptering.")
    parser.add_argument("--task", choices=["all", "naming", "chaptering"], default="all")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Optional OpenRouter model id or resolved slug to run. May be repeated.",
    )
    parser.add_argument(
        "--benchmark",
        action="append",
        choices=sorted(BENCHMARKS),
        default=[],
        help="Optional benchmark key to run. May be repeated.",
    )
    parser.add_argument(
        "--budget-start-usage",
        type=float,
        default=None,
        help="OpenRouter total_usage value to treat as the start of this budgeted run.",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Score existing matrix artifacts instead of re-calling the model when outputs already exist.",
    )
    return parser.parse_args()


def filter_models(models: list[ModelConfig], requested: list[str]) -> list[ModelConfig]:
    if not requested:
        return models
    wanted = set(requested)
    selected = [
        model
        for model in models
        if model.model_id in wanted or model.requested_id in wanted or model.slug in wanted
    ]
    missing = [
        item
        for item in requested
        if not any(item in {model.model_id, model.requested_id, model.slug} for model in selected)
    ]
    if missing:
        raise RuntimeError(f"requested model filters did not match: {missing}")
    return selected


def filter_benchmarks(requested: list[str]) -> dict[str, Path]:
    if not requested:
        return dict(BENCHMARKS)
    return {key: BENCHMARKS[key] for key in requested}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def resolve_models(client: httpx.Client, headers: dict[str, str]) -> list[ModelConfig]:
    response = client.get(OPENROUTER_MODELS_URL, headers=headers)
    response.raise_for_status()
    data = response.json().get("data", [])
    by_id = {str(item.get("id")): item for item in data if isinstance(item, dict) and item.get("id")}
    resolved = []
    for requested, fallback in CANDIDATES:
        model_id = requested
        if requested not in by_id:
            if fallback and fallback in by_id:
                model_id = fallback
            else:
                nearest = nearest_model_id(requested, by_id)
                if nearest is None:
                    raise RuntimeError(f"OpenRouter model not found: {requested}")
                model_id = nearest
        record = by_id[model_id]
        resolved.append(
            ModelConfig(
                requested_id=requested,
                model_id=model_id,
                slug=model_slug(model_id),
                pricing=record.get("pricing", {}) if isinstance(record.get("pricing"), dict) else {},
            )
        )
    return resolved


def nearest_model_id(requested: str, by_id: dict[str, Any]) -> str | None:
    provider, _, name = requested.partition("/")
    provider_matches = [model_id for model_id in by_id if model_id.startswith(provider + "/")]
    if provider_matches:
        requested_terms = set(re.findall(r"[a-z0-9]+", name.lower()))
        scored = []
        for model_id in provider_matches:
            terms = set(re.findall(r"[a-z0-9]+", model_id.lower()))
            scored.append((len(requested_terms & terms), model_id))
        scored.sort(reverse=True)
        if scored and scored[0][0] > 0:
            return scored[0][1]
    return None


def model_slug(model_id: str) -> str:
    return safe_key(model_id.replace("/", "-"))


def meeting_from_dir(bench_dir: Path) -> Meeting:
    payload = read_json(bench_dir / "meeting.json")
    return Meeting(
        meeting_key=str(payload.get("slug") or bench_dir.name),
        meeting_dir=bench_dir,
        legistar_event_id=payload.get("legistar_event_id"),
        legistar_event_guid=payload.get("legistar_event_guid"),
        viebit_filename=payload.get("viebit_file"),
        viebit_hash=payload.get("viebit_hash"),
        body_name=payload.get("body"),
        event_date=payload.get("date"),
        event_time=payload.get("time"),
        duration_seconds=payload.get("duration_sec"),
        meeting_type=payload.get("meeting_type"),
    )


def run_naming(
    model: ModelConfig,
    benchmark: str,
    bench_dir: Path,
    eval07,
    *,
    reuse_existing: bool,
) -> dict[str, Any]:
    matrix_dir = bench_dir / "matrix"
    matrix_dir.mkdir(parents=True, exist_ok=True)
    output_path = matrix_dir / f"naming-{model.slug}.jsonl"
    meta_path = matrix_dir / f"naming-{model.slug}.json"
    report_path = matrix_dir / f"naming-{model.slug}.md"
    started = time.monotonic()
    row = {
        "task": "naming",
        "benchmark": benchmark,
        "model": model.model_id,
        "requested_model": model.requested_id,
        "model_slug": model.slug,
        "report": str(report_path.relative_to(ROOT)),
        "status": "FAIL",
    }
    if reuse_existing and output_path.exists() and meta_path.exists():
        print(f"REUSE naming benchmark={benchmark} model={model.model_id}", flush=True)
    else:
        print(f"RUN naming benchmark={benchmark} model={model.model_id}", flush=True)
    try:
        if not (reuse_existing and output_path.exists() and meta_path.exists()):
            name_speakers_meeting(
                meeting_from_dir(bench_dir),
                model=model.model_id,
                input_path=bench_dir / "utterances-voxtral-labeled.jsonl",
                output_path=output_path,
                meta_path=meta_path,
                runlog_stage=f"matrix_name_speakers_{benchmark}_{model.slug}",
                write_runlog=False,
                llm_base_url=OPENROUTER_BASE_URL,
                llm_api_key_env="OPENROUTER_API_KEY",
                verification_model=None,
            )
        named = read_jsonl(output_path)
        meta = read_json(meta_path)
        asr_meta = read_json(bench_dir / "transcribe-voxtral-meta.json")
        refs = eval07._read_citymeetings_references(bench_dir)
        report = eval07._score(named, refs, meta, asr_meta=asr_meta, benchmark=benchmark, asr="voxtral")
        metrics = naming_metrics(named, refs, eval07)
        row.update(metrics)
        row.update(cost_fields(meta))
        row["status"] = "PASS"
        report_path.write_text(
            matrix_header(
                title=f"Model Matrix Naming - {benchmark} - {model.model_id}",
                model=model,
                status="PASS",
                meta=meta,
                elapsed_sec=time.monotonic() - started,
            )
            + "\n"
            + report
        )
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        report_path.write_text(
            failure_report(
                title=f"Model Matrix Naming - {benchmark} - {model.model_id}",
                model=model,
                error=exc,
                elapsed_sec=time.monotonic() - started,
            )
        )
    return row


def naming_metrics(named: list[dict[str, Any]], refs: list[Any], eval07) -> dict[str, Any]:
    starts = [float(row["t0"]) for row in named]
    raw_matches = []
    for ref in refs:
        idx = eval07._nearest_index(starts, ref.seek)
        if idx is None or abs(starts[idx] - ref.seek) > eval07.MATCH_TOLERANCE_SEC:
            continue
        raw_matches.append(
            eval07.MatchedUtterance(
                ref=ref,
                index=idx,
                expected=eval07._display_speaker(ref.speaker),
                predicted=eval07._display_speaker(str(named[idx].get("speaker") or "UNKNOWN")),
                matched_text=str(named[idx].get("text") or ""),
            )
        )
    scored = eval07._deskew_matches(raw_matches)
    strict = sum(1 for item in scored if eval07._strict_key(item.predicted) == eval07._strict_key(item.expected))
    same = sum(1 for item in scored if eval07._same_person(item.predicted, item.expected))
    total = len(scored)
    return {
        "references": len(refs),
        "matched": len(raw_matches),
        "scored": total,
        "same_person_accuracy": same / total if total else 0.0,
        "strict_accuracy": strict / total if total else 0.0,
    }


def run_chaptering(
    model: ModelConfig,
    benchmark: str,
    bench_dir: Path,
    compare05,
    *,
    reuse_existing: bool,
) -> dict[str, Any]:
    matrix_dir = bench_dir / "matrix"
    matrix_dir.mkdir(parents=True, exist_ok=True)
    chapters_path = matrix_dir / f"chaptering-{model.slug}.json"
    derived_path = matrix_dir / f"chaptering-{model.slug}-derived.json"
    report_path = matrix_dir / f"chaptering-{model.slug}.md"
    started = time.monotonic()
    row = {
        "task": "chaptering",
        "benchmark": benchmark,
        "model": model.model_id,
        "requested_model": model.requested_id,
        "model_slug": model.slug,
        "report": str(report_path.relative_to(ROOT)),
        "status": "FAIL",
    }
    if reuse_existing and chapters_path.exists():
        print(f"REUSE chaptering benchmark={benchmark} model={model.model_id}", flush=True)
    else:
        print(f"RUN chaptering benchmark={benchmark} model={model.model_id}", flush=True)
    try:
        if not (reuse_existing and chapters_path.exists()):
            chapterize_meeting(
                meeting_from_dir(bench_dir),
                model=model.model_id,
                input_path=str(bench_dir / "utterances-voxtral-labeled.jsonl"),
                output_path=str(chapters_path),
                derived_path=str(derived_path),
                runlog_stage=f"matrix_chapterize_{benchmark}_{model.slug}",
                write_runlog=False,
                llm_base_url=OPENROUTER_BASE_URL,
                llm_api_key_env="OPENROUTER_API_KEY",
            )
        meta = read_json(chapters_path)
        metrics, report = chaptering_metrics_report(benchmark, bench_dir, chapters_path, compare05)
        row.update(metrics)
        row.update(cost_fields(meta))
        row["status"] = "PASS"
        report_path.write_text(
            matrix_header(
                title=f"Model Matrix Chaptering - {benchmark} - {model.model_id}",
                model=model,
                status="PASS",
                meta=meta,
                elapsed_sec=time.monotonic() - started,
            )
            + "\n"
            + report
        )
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        report_path.write_text(
            failure_report(
                title=f"Model Matrix Chaptering - {benchmark} - {model.model_id}",
                model=model,
                error=exc,
                elapsed_sec=time.monotonic() - started,
            )
        )
    return row


def chaptering_metrics_report(benchmark: str, bench_dir: Path, chapters_path: Path, compare05) -> tuple[dict[str, Any], str]:
    reference = compare05.parse_reference_from_html(bench_dir)
    captions = compare05.load_captions(bench_dir / "captions-clean.jsonl")
    if captions:
        meeting_start_sec = min(row["t"] for row in captions)
        meeting_end_sec = max(row["t"] for row in captions)
    else:
        meeting_start_sec = min(ch.start_sec for ch in reference)
        meeting_end_sec = max(ch.start_sec + (ch.duration_sec or 0.0) for ch in reference)
    compare05.infer_ends(reference, meeting_end_sec, last_uses_duration=True)
    _, generated = compare05.load_generated(chapters_path)
    compare05.infer_ends(generated, meeting_end_sec, last_uses_duration=False)
    boundaries = compare05.boundary_metrics(reference, generated)
    alignments = compare05.align_chapters(reference, generated)
    type_agreement = compare05.normalized_type_agreement(alignments)
    coverage = compare05.coverage(generated, meeting_start_sec, meeting_end_sec)
    durations = compare05.distribution(compare05.chapter_durations(generated, prefer_parsed=False))
    sequence = compare05.sequence_checks(generated, meeting_start_sec, meeting_end_sec)
    confusion_table, _ = compare05.build_type_confusion_table(alignments)
    metrics = {
        "generated_chapters": len(generated),
        "reference_chapters": len(reference),
        "f1_15": boundaries[15]["f1"],
        "f1_30": boundaries[30]["f1"],
        "f1_60": boundaries[60]["f1"],
        "type_agreement": type_agreement,
        "coverage": coverage["coverage_pct"],
        "non_monotonic": len(sequence["non_monotonic"]),
    }
    report = "\n".join(
        [
            f"# Chaptering Eval - {benchmark} / voxtral",
            "",
            f"- Benchmark: {benchmark}",
            f"- Input: utterances-voxtral-labeled.jsonl",
            f"- Generated chapters: {len(generated)}",
            f"- Reference chapters: {len(reference)}",
            f"- Generated median duration: {compare05.format_duration(durations['median'])}",
            f"- Generated coverage: {compare05.format_percent(coverage['coverage_pct'])}",
            f"- F1 @15s: {compare05.format_percent(boundaries[15]['f1'])}",
            f"- F1 @30s: {compare05.format_percent(boundaries[30]['f1'])}",
            f"- F1 @60s: {compare05.format_percent(boundaries[60]['f1'])}",
            f"- Type agreement: {compare05.format_percent(type_agreement)}",
            f"- Non-monotonic starts: {len(sequence['non_monotonic'])}",
            "",
            "## Boundary Agreement",
            "",
            compare05.build_boundary_table(boundaries),
            "",
            "## Counts, Duration, And Coverage",
            "",
            compare05.build_duration_table(reference, generated),
            "",
            compare05.build_coverage_table(reference, generated, meeting_start_sec, meeting_end_sec),
            "",
            compare05.build_sequence_table(sequence),
            "",
            "## Type Confusion",
            "",
            confusion_table,
            "",
        ]
    )
    return metrics, report


def matrix_header(
    *,
    title: str,
    model: ModelConfig,
    status: str,
    meta: dict[str, Any],
    elapsed_sec: float,
) -> str:
    usage = meta.get("usage", {}) if isinstance(meta.get("usage"), dict) else {}
    cost = meta.get("exact_cost_usd")
    if not isinstance(cost, int | float):
        cost = meta.get("estimated_cost_usd")
    if not isinstance(cost, int | float):
        cost = 0.0
    lines = [
        f"# {title}",
        "",
        f"- Status: {status}",
        f"- Requested model: {model.requested_id}",
        f"- OpenRouter model: {model.model_id}",
        f"- Provider: {meta.get('provider', 'openai-compatible')}",
        f"- Structured mode: {meta.get('structured_mode', 'json_schema')}",
        f"- Input tokens: {usage.get('prompt_tokens', usage.get('promptTokenCount', 'n/a'))}",
        f"- Output tokens: {usage.get('completion_tokens', usage.get('candidatesTokenCount', 'n/a'))}",
        f"- Total tokens: {usage.get('total_tokens', usage.get('totalTokenCount', 'n/a'))}",
        f"- Cost: ${float(cost):.6f}",
        f"- Cost source: {meta.get('cost_source', 'estimated')}",
        f"- Wall time: {elapsed_sec:.1f}s",
        "",
    ]
    return "\n".join(lines)


def failure_report(*, title: str, model: ModelConfig, error: Exception, elapsed_sec: float) -> str:
    return "\n".join(
        [
            f"# {title}",
            "",
            "- Status: FAIL",
            f"- Requested model: {model.requested_id}",
            f"- OpenRouter model: {model.model_id}",
            "- Failure mode: " + f"{type(error).__name__}: {error}",
            f"- Wall time: {elapsed_sec:.1f}s",
            "",
            "## Traceback",
            "",
            "```text",
            "".join(traceback.format_exception(type(error), error, error.__traceback__)).strip(),
            "```",
            "",
        ]
    )


def cost_fields(meta: dict[str, Any]) -> dict[str, Any]:
    cost = meta.get("exact_cost_usd")
    if not isinstance(cost, int | float):
        cost = meta.get("estimated_cost_usd")
    if not isinstance(cost, int | float):
        cost = 0.0
    return {
        "cost_usd": float(cost),
        "cost_source": meta.get("cost_source", "estimated"),
        "input_tokens": token_value(meta, "prompt_tokens", "promptTokenCount"),
        "output_tokens": token_value(meta, "completion_tokens", "candidatesTokenCount"),
        "total_tokens": token_value(meta, "total_tokens", "totalTokenCount"),
    }


def token_value(meta: dict[str, Any], openai_key: str, gemini_key: str) -> int | str:
    usage = meta.get("usage", {}) if isinstance(meta.get("usage"), dict) else {}
    value = usage.get(openai_key, usage.get(gemini_key))
    return int(value) if isinstance(value, int | float) else "n/a"


def openrouter_total_usage(client: httpx.Client, headers: dict[str, str]) -> float:
    response = client.get(OPENROUTER_CREDITS_URL, headers=headers)
    response.raise_for_status()
    data = response.json().get("data", {})
    return float(data.get("total_usage") or 0.0)


def ensure_budget(client: httpx.Client, headers: dict[str, str], start_usage: float) -> None:
    current_usage = openrouter_total_usage(client, headers)
    spent = max(0.0, current_usage - start_usage)
    if spent >= BUDGET_USD:
        raise BudgetStop(f"STOP budget reached: spent=${spent:.6f} limit=${BUDGET_USD:.2f}")


def print_progress(
    client: httpx.Client,
    headers: dict[str, str],
    start_usage: float,
    row: dict[str, Any],
) -> None:
    usage = openrouter_total_usage(client, headers)
    spent = max(0.0, usage - start_usage)
    print(
        "DONE "
        f"{row['task']} benchmark={row['benchmark']} model={row['model']} "
        f"status={row['status']} report={row['report']} "
        f"cost=${float(row.get('cost_usd') or 0):.6f} openrouter_delta=${spent:.6f}",
        flush=True,
    )


def write_summary(
    rows: list[dict[str, Any]],
    *,
    start_usage: float,
    end_usage: float,
    models: list[ModelConfig],
) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    total_reported = sum(float(row.get("cost_usd") or 0.0) for row in rows)
    observed_delta = max(0.0, end_usage - start_usage)
    conservative_spend = max(observed_delta, total_reported)
    lines = [
        "# Model Matrix Summary",
        "",
        f"- OpenRouter total_usage start: ${start_usage:.6f}",
        f"- OpenRouter total_usage end: ${end_usage:.6f}",
        f"- OpenRouter observed run delta: ${observed_delta:.6f}",
        f"- Reported generation cost total: ${total_reported:.6f}",
        f"- Conservative budget spend: ${conservative_spend:.6f}",
        f"- Budget limit: ${BUDGET_USD:.2f}",
        "- Verification pass: disabled for matrix LLM calls; deterministic roster/Legistar spelling anchors still run.",
        "- Chaptering input: utterances-voxtral-labeled.jsonl.",
        "",
        "## Models",
        "",
        table(
            ["requested", "used", "prompt $/tok", "completion $/tok"],
            [
                [
                    model.requested_id,
                    model.model_id,
                    model.pricing.get("prompt", "n/a"),
                    model.pricing.get("completion", "n/a"),
                ]
                for model in models
            ],
        ),
        "",
        "## Results",
        "",
        table(
            [
                "task",
                "benchmark",
                "model",
                "status",
                "cost",
                "same-person",
                "strict",
                "chapters",
                "F1@15",
                "F1@30",
                "F1@60",
                "type agree",
                "report",
                "error",
            ],
            [summary_row(row) for row in rows],
        ),
        "",
        "## Baselines",
        "",
        table(["task", "benchmark", "model", "baseline"], BASELINE_NOTES),
        "",
    ]
    SUMMARY_PATH.write_text("\n".join(lines))
    write_json(
        SUMMARY_PATH.with_suffix(".json"),
        {
            "start_usage_usd": start_usage,
            "end_usage_usd": end_usage,
            "observed_delta_usd": observed_delta,
            "reported_generation_cost_usd": total_reported,
            "conservative_budget_spend_usd": conservative_spend,
            "budget_usd": BUDGET_USD,
            "rows": rows,
        },
    )


def summary_row(row: dict[str, Any]) -> list[Any]:
    return [
        row.get("task", ""),
        row.get("benchmark", ""),
        row.get("model", ""),
        row.get("status", ""),
        money(row.get("cost_usd")),
        pct(row.get("same_person_accuracy")),
        pct(row.get("strict_accuracy")),
        row.get("generated_chapters", ""),
        pct(row.get("f1_15")),
        pct(row.get("f1_30")),
        pct(row.get("f1_60")),
        pct(row.get("type_agreement")),
        row.get("report", ""),
        str(row.get("error", ""))[:160],
    ]


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(md(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(md(item) for item in row) + " |")
    return "\n".join(lines)


def pct(value: Any) -> str:
    return f"{float(value) * 100:.1f}%" if isinstance(value, int | float) else ""


def money(value: Any) -> str:
    return f"${float(value):.6f}" if isinstance(value, int | float) else ""


def md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
