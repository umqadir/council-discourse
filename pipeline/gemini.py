from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx
from json_repair import repair_json

from .utils import load_dotenv

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
DEFAULT_MODEL = "gemini-3.5-flash"
GEMINI_PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
GEMINI_PRICING_PER_MILLION_USD = {
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
}


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 2))


def estimate_usage_cost_usd(model: str, usage: dict[str, Any]) -> float | None:
    pricing = _pricing_for_model(model)
    if pricing is None:
        return None
    input_tokens = _int_usage(usage, "promptTokenCount")
    output_tokens = _int_usage(usage, "candidatesTokenCount") + _int_usage(usage, "thoughtsTokenCount")
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    return round(cost, 6)


def pricing_details(model: str) -> dict[str, Any] | None:
    pricing = _pricing_for_model(model)
    if pricing is None:
        return None
    return {
        "source": GEMINI_PRICING_SOURCE,
        "input_per_million_usd": pricing["input"],
        "output_per_million_usd": pricing["output"],
    }


def generate_json(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_output_tokens: int = 65536,
    temperature: float = 0.2,
    timeout_seconds: int = 1200,
    max_attempts: int = 2,
    thinking_level: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    response_mime_type: str | None = "application/json",
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv()
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is required for Gemini stages")

    started = time.monotonic()
    response: httpx.Response | None = None
    generation_config: dict[str, Any] = {
        "maxOutputTokens": max_output_tokens,
        "temperature": temperature,
    }
    if response_mime_type:
        generation_config["responseMimeType"] = response_mime_type
    if thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    if tools:
        payload["tools"] = tools
    for attempt in range(1, max_attempts + 1):
        try:
            response = httpx.post(
                API_URL.format(model=model, key=key),
                json=payload,
                timeout=timeout_seconds,
            )
            break
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if attempt >= max_attempts:
                raise
            time.sleep(min(30, 2**attempt))
    elapsed = time.monotonic() - started
    if response is None:
        raise RuntimeError(f"Gemini {model} did not return a response")
    body = response.json()
    if response.status_code != 200:
        raise RuntimeError(f"Gemini {model} failed: {json.dumps(body)[:2000]}")

    usage = body.get("usageMetadata", {})
    meta = {
        "model": model,
        "elapsed_sec": round(elapsed, 3),
        "usage": usage,
    }
    if thinking_level:
        meta["thinking_level"] = thinking_level
    estimated_cost = estimate_usage_cost_usd(model, usage)
    if estimated_cost is not None:
        meta["estimated_cost_usd"] = estimated_cost
        meta["pricing"] = pricing_details(model)

    candidate = body["candidates"][0]
    grounding = candidate.get("groundingMetadata")
    if grounding:
        meta["grounding"] = _compact_grounding_metadata(grounding)
    text = candidate["content"]["parts"][0]["text"]
    return _parse_json_text(text), meta


def _parse_json_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        repaired = repair_json(stripped)
        if isinstance(repaired, str):
            return json.loads(repaired)
        return repaired


def _pricing_for_model(model: str) -> dict[str, float] | None:
    normalized = model.rsplit("/", 1)[-1]
    if normalized in GEMINI_PRICING_PER_MILLION_USD:
        return GEMINI_PRICING_PER_MILLION_USD[normalized]
    for prefix, pricing in GEMINI_PRICING_PER_MILLION_USD.items():
        if normalized.startswith(prefix):
            return pricing
    return None


def _int_usage(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    return int(value) if isinstance(value, int | float) else 0


def _compact_grounding_metadata(grounding: dict[str, Any]) -> dict[str, Any]:
    queries = [str(item) for item in grounding.get("webSearchQueries", []) if str(item).strip()]
    chunks = []
    for chunk in grounding.get("groundingChunks", []) or []:
        web = chunk.get("web") if isinstance(chunk, dict) else None
        if isinstance(web, dict):
            chunks.append(
                {
                    "title": web.get("title"),
                    "uri": web.get("uri"),
                }
            )
    return {
        "web_search_queries": queries,
        "web_search_query_count": len(queries),
        "grounding_chunks": chunks[:40],
        "grounding_chunk_count": len(chunks),
    }
