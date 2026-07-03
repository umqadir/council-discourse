"""Prompt-caching probe (brief item 1).

Zero quality risk: identical tokens/outputs. We send the SAME large chaptering
prompt twice back-to-back to a model via OpenRouter and record, for each call,
the billed prompt/completion tokens, the provider-reported cached_tokens, and the
authoritative OpenRouter generation cost. If provider-side prefix caching is
active and passed through by OpenRouter, the second call should show
cached_tokens > 0 and a lower billed cost than the first.

We also test the explicit-breakpoint path: OpenRouter/Anthropic-style caching for
some providers requires a cache_control marker on the large prefix. We send a
third call with an explicit cache_control breakpoint to see whether that changes
anything for these models.

Usage: python experiments/12_cache_probe.py [--model z-ai/glm-5.2] [--benchmark stated]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.artifacts import normalize_utterances, read_json, read_jsonl
from pipeline.chapterize import _chapter_prompt
from pipeline.gemini import _cached_prompt_tokens, _openrouter_generation_cost_usd
from pipeline.models import Meeting
from pipeline.utils import load_dotenv


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

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
BENCHMARKS = {
    "transportation": ROOT / "data" / "benchmark" / "2025-04-23-transportation",
    "stated": ROOT / "data" / "benchmark" / "2025-04-24-stated",
}
OUT = ROOT / "experiments" / "out"


def main() -> int:
    args = parse_args()
    load_dotenv()
    key = os.environ["OPENROUTER_API_KEY"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    bench_dir = BENCHMARKS[args.benchmark]
    meeting = meeting_from_dir(bench_dir)
    utterances = normalize_utterances(read_jsonl(bench_dir / "utterances-voxtral-labeled.jsonl"))
    prompt = _chapter_prompt(meeting, utterances)

    results: list[dict[str, Any]] = []
    # Call A and B: identical plain prompts back-to-back (implicit/auto caching).
    for tag in ("A_first", "B_repeat"):
        results.append(one_call(headers, key, args.model, prompt, tag=tag, cache_control=False))
        time.sleep(1)
    # Call C: explicit cache_control breakpoint on the big transcript block.
    results.append(one_call(headers, key, args.model, prompt, tag="C_cache_control", cache_control=True))

    summary = {
        "model": args.model,
        "benchmark": args.benchmark,
        "prompt_chars": len(prompt),
        "calls": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    safe = args.model.replace("/", "-")
    (OUT / f"cache-probe-{safe}-{args.benchmark}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "calls"}, indent=2))
    for r in results:
        print(
            f"  {r['tag']:16s} prompt_tok={r.get('prompt_tokens')} "
            f"cached={r.get('cached_tokens')} compl={r.get('completion_tokens')} "
            f"cost=${r.get('cost_usd')} ({r.get('cost_source')})",
            flush=True,
        )
    return 0


def one_call(headers, key, model, prompt, *, tag: str, cache_control: bool) -> dict[str, Any]:
    if cache_control:
        # Split so the big transcript-bearing prefix carries a cache breakpoint.
        content = [
            {
                "type": "text",
                "text": prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        content = prompt
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only valid JSON for the user's request."},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": 20000,
        "usage": {"include": True},
    }
    started = time.monotonic()
    resp = httpx.post(CHAT_URL, json=payload, headers=headers, timeout=1200)
    elapsed = round(time.monotonic() - started, 1)
    row: dict[str, Any] = {"tag": tag, "cache_control": cache_control, "elapsed_sec": elapsed, "status": resp.status_code}
    if resp.status_code >= 400:
        row["error"] = resp.text[:600]
        return row
    body = resp.json()
    usage = body.get("usage", {})
    row["prompt_tokens"] = usage.get("prompt_tokens")
    row["completion_tokens"] = usage.get("completion_tokens")
    row["cached_tokens"] = _cached_prompt_tokens(usage)
    row["usage_raw"] = usage
    gen_id = body.get("id")
    if gen_id:
        cost = _openrouter_generation_cost_usd(str(gen_id), key)
        if cost is not None:
            row["cost_usd"] = cost
            row["cost_source"] = "openrouter_generation"
    return row


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="z-ai/glm-5.2")
    p.add_argument("--benchmark", default="stated", choices=sorted(BENCHMARKS))
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
