from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx
from json_repair import repair_json

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
DEFAULT_MODEL = "gemini-3.5-flash"


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 2))


def generate_json(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_output_tokens: int = 65536,
    temperature: float = 0.2,
    timeout_seconds: int = 1200,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is required for Gemini stages")

    started = time.monotonic()
    response = httpx.post(
        API_URL.format(model=model, key=key),
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": max_output_tokens,
                "temperature": temperature,
            },
        },
        timeout=timeout_seconds,
    )
    elapsed = time.monotonic() - started
    body = response.json()
    if response.status_code != 200:
        raise RuntimeError(f"Gemini {model} failed: {json.dumps(body)[:2000]}")

    text = body["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_json_text(text), {
        "model": model,
        "elapsed_sec": round(elapsed, 3),
        "usage": body.get("usageMetadata", {}),
    }


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
