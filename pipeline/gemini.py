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
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_GENERATION_URL = "https://openrouter.ai/api/v1/generation"
DEFAULT_MODEL = "gemini-3.5-flash"
GEMINI_PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
GEMINI_PRICING_PER_MILLION_USD = {
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
}
_OPENROUTER_PRICING_CACHE: dict[str, dict[str, Any]] | None = None


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
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv()
    if base_url:
        return _generate_json_openai_compatible(
            prompt=prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            json_schema=json_schema,
        )
    return _generate_json_gemini(
        prompt=prompt,
        model=model,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        thinking_level=thinking_level,
        tools=tools,
        response_mime_type=response_mime_type,
    )


def _generate_json_gemini(
    *,
    prompt: str,
    model: str,
    max_output_tokens: int,
    temperature: float,
    timeout_seconds: int,
    max_attempts: int,
    thinking_level: str | None,
    tools: list[dict[str, Any]] | None,
    response_mime_type: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is required for Gemini stages")

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
    attempts: list[dict[str, Any]] = []
    last_text = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            payload["contents"][0]["parts"][0]["text"] = _retry_json_prompt(prompt)
        response: httpx.Response | None = None
        attempt_started = time.monotonic()
        try:
            response = _post_with_retry(
                API_URL.format(model=model, key=key),
                json_payload=payload,
                headers=None,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise
        elapsed = time.monotonic() - attempt_started
        if response is None:
            raise RuntimeError(f"Gemini {model} did not return a response")
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            attempts.append(
                {
                    "provider": "gemini",
                    "model": model,
                    "elapsed_sec": round(elapsed, 3),
                    "attempt": attempt,
                    "parse_error": f"malformed response body: {exc}",
                }
            )
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"Gemini {model} returned a malformed response body "
                    f"after {attempt} attempt(s): {exc}; body excerpt: {response.text[:500]}"
                ) from exc
            continue
        if response.status_code != 200:
            raise RuntimeError(f"Gemini {model} failed: {json.dumps(body)[:2000]}")

        usage = body.get("usageMetadata", {})
        meta = {
            "provider": "gemini",
            "model": model,
            "elapsed_sec": round(elapsed, 3),
            "usage": usage,
            "attempt": attempt,
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
        last_text = text
        try:
            parsed = _parse_json_text(text)
        except Exception as exc:
            meta["parse_error"] = str(exc)
            attempts.append(meta)
            if attempt >= max_attempts:
                combined = _combine_generation_attempts(attempts)
                raise RuntimeError(
                    f"Gemini {model} returned unparsable JSON after {attempt} attempt(s): "
                    f"{exc}; response excerpt: {last_text[:500]}"
                ) from exc
            continue
        attempts.append(meta)
        return parsed, _combine_generation_attempts(attempts)
    raise RuntimeError(f"Gemini {model} did not return parseable JSON")


def _generate_json_openai_compatible(
    *,
    prompt: str,
    model: str,
    max_output_tokens: int,
    temperature: float,
    timeout_seconds: int,
    max_attempts: int,
    base_url: str,
    api_key: str | None,
    api_key_env: str | None,
    json_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key_env = api_key_env or ("OPENROUTER_API_KEY" if "openrouter.ai" in base_url else None)
    key = api_key or (os.environ.get(key_env) if key_env else None)
    if not key:
        env_hint = key_env or "an API key"
        raise RuntimeError(f"{env_hint} is required for OpenAI-compatible LLM stages")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    chat_url = _openai_compatible_chat_url(base_url)
    schema = json_schema or {"type": "object", "additionalProperties": True}
    attempts: list[dict[str, Any]] = []
    last_text = ""
    structured_mode = "json_schema"

    for attempt in range(1, max_attempts + 1):
        request_prompt = prompt if attempt == 1 else _retry_json_prompt(prompt)
        payload = _openai_payload(
            model=model,
            prompt=request_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            schema=schema,
            structured_mode=structured_mode,
        )
        attempt_started = time.monotonic()
        response = _post_with_retry(
            chat_url,
            json_payload=payload,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        if response.status_code >= 400 and structured_mode == "json_schema":
            body_excerpt = _response_excerpt(response)
            if response.status_code in {400, 404, 422}:
                structured_mode = "tool"
                payload = _openai_payload(
                    model=model,
                    prompt=request_prompt,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                    schema=schema,
                    structured_mode=structured_mode,
                )
                response = _post_with_retry(
                    chat_url,
                    json_payload=payload,
                    headers=headers,
                    timeout_seconds=timeout_seconds,
                    max_attempts=max_attempts,
                )
            if response.status_code >= 400:
                raise RuntimeError(f"OpenAI-compatible {model} failed: {_response_excerpt(response) or body_excerpt}")
        elif response.status_code >= 400:
            raise RuntimeError(f"OpenAI-compatible {model} failed: {_response_excerpt(response)}")

        elapsed = time.monotonic() - attempt_started
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            # Providers occasionally return a 200 with a truncated or non-JSON
            # body; treat it like an unparsable generation and retry.
            attempts.append(
                {
                    "provider": "openai-compatible",
                    "model": model,
                    "base_url": _redacted_base_url(base_url),
                    "elapsed_sec": round(elapsed, 3),
                    "attempt": attempt,
                    "parse_error": f"malformed response body: {exc}",
                }
            )
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"OpenAI-compatible {model} returned a malformed response body "
                    f"after {attempt} attempt(s): {exc}; body excerpt: {response.text[:500]}"
                ) from exc
            continue
        usage = body.get("usage", {})
        response_id = body.get("id")
        meta: dict[str, Any] = {
            "provider": "openai-compatible",
            "model": model,
            "base_url": _redacted_base_url(base_url),
            "elapsed_sec": round(elapsed, 3),
            "usage": usage,
            "attempt": attempt,
            "structured_mode": structured_mode,
        }
        if response_id:
            meta["generation_id"] = response_id
        cached = _cached_prompt_tokens(usage)
        if cached is not None:
            meta["cached_prompt_tokens"] = cached
        if "openrouter.ai" in base_url:
            _attach_openrouter_cost(meta, model=model, api_key=key)

        try:
            text = _openai_response_text(body)
            last_text = text
            parsed = _parse_json_text(text)
        except Exception as exc:
            meta["parse_error"] = str(exc)
            attempts.append(meta)
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"OpenAI-compatible {model} returned unparsable JSON after {attempt} attempt(s): "
                    f"{exc}; response excerpt: {last_text[:500]}"
                ) from exc
            continue
        attempts.append(meta)
        return parsed, _combine_generation_attempts(attempts)
    raise RuntimeError(f"OpenAI-compatible {model} did not return parseable JSON")


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


def _post_with_retry(
    url: str,
    *,
    json_payload: dict[str, Any],
    headers: dict[str, str] | None,
    timeout_seconds: int,
    max_attempts: int,
) -> httpx.Response:
    for attempt in range(1, max_attempts + 1):
        try:
            return httpx.post(url, json=json_payload, headers=headers, timeout=timeout_seconds)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if attempt >= max_attempts:
                raise
            time.sleep(min(30, 2**attempt))
    raise RuntimeError("HTTP request did not return a response")


def _retry_json_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\n"
        "The previous response could not be parsed as valid JSON. Return exactly one valid JSON object. "
        "Do not include Markdown fences, prose, comments, or trailing text."
    )


def _openai_compatible_chat_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return value + "/chat/completions"


def _openai_payload(
    *,
    model: str,
    prompt: str,
    max_output_tokens: int,
    temperature: float,
    schema: dict[str, Any],
    structured_mode: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return only valid JSON for the user's request.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_output_tokens,
    }
    if structured_mode == "tool":
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "return_json",
                    "description": "Return the complete JSON object requested by the prompt.",
                    "parameters": schema,
                },
            }
        ]
        payload["tool_choice"] = {"type": "function", "function": {"name": "return_json"}}
    else:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_result",
                "strict": False,
                "schema": schema,
            },
        }
    return payload


def _openai_response_text(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"response has no choices: {json.dumps(body)[:500]}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise RuntimeError(f"response choice has no message: {json.dumps(choices[0])[:500]}")
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        function = tool_calls[0].get("function") if isinstance(tool_calls[0], dict) else None
        if isinstance(function, dict) and function.get("arguments"):
            return str(function["arguments"])
    refusal = message.get("refusal")
    if refusal:
        raise RuntimeError(f"model refused JSON: {refusal}")
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        content = "".join(parts)
    if not content:
        raise RuntimeError(f"response message has no JSON content: {json.dumps(message)[:500]}")
    return str(content)


def _response_excerpt(response: httpx.Response) -> str:
    try:
        return json.dumps(response.json())[:2000]
    except Exception:
        return response.text[:2000]


def _redacted_base_url(base_url: str) -> str:
    return re.sub(r"([?&](?:key|api_key)=)[^&]+", r"\1REDACTED", base_url)


def _combine_generation_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        return {}
    combined = dict(attempts[-1])
    combined["elapsed_sec"] = round(sum(float(meta.get("elapsed_sec") or 0.0) for meta in attempts), 3)
    usage_totals: dict[str, int] = {}
    for meta in attempts:
        usage = meta.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, int | float):
                usage_totals[key] = usage_totals.get(key, 0) + int(value)
    if usage_totals:
        combined["usage"] = usage_totals
    for cost_key in ("estimated_cost_usd", "exact_cost_usd"):
        costs = [meta.get(cost_key) for meta in attempts if isinstance(meta.get(cost_key), int | float)]
        if costs:
            combined[cost_key] = round(sum(float(cost) for cost in costs), 6)
    if len(attempts) > 1:
        combined["attempts"] = attempts
    return combined


def _attach_openrouter_cost(meta: dict[str, Any], *, model: str, api_key: str) -> None:
    exact = _openrouter_generation_cost_usd(str(meta.get("generation_id") or ""), api_key)
    pricing = _openrouter_pricing_for_model(model, api_key)
    if pricing:
        meta["pricing"] = {
            "source": OPENROUTER_MODELS_URL,
            "prompt_per_token_usd": _float_or_none(pricing.get("prompt")),
            "completion_per_token_usd": _float_or_none(pricing.get("completion")),
            "internal_reasoning_per_token_usd": _float_or_none(pricing.get("internal_reasoning")),
            "input_cache_read_per_token_usd": _float_or_none(pricing.get("input_cache_read")),
        }
    if exact is not None:
        meta["exact_cost_usd"] = exact
        meta["estimated_cost_usd"] = exact
        meta["cost_source"] = "openrouter_generation"
        return
    estimated = _estimate_openrouter_usage_cost_usd(meta.get("usage", {}), pricing)
    if estimated is not None:
        meta["estimated_cost_usd"] = estimated
        meta["cost_source"] = "openrouter_model_pricing_estimate"


def _openrouter_generation_cost_usd(generation_id: str, api_key: str) -> float | None:
    if not generation_id:
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    params = {"id": generation_id}
    for delay in (0.0, 0.5, 1.5, 3.0):
        if delay:
            time.sleep(delay)
        try:
            response = httpx.get(OPENROUTER_GENERATION_URL, params=params, headers=headers, timeout=30)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            continue
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except json.JSONDecodeError:
            continue
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            continue
        for key in ("total_cost", "cost", "usage_cost"):
            value = _float_or_none(data.get(key))
            if value is not None:
                return round(value, 6)
    return None


def _openrouter_pricing_for_model(model: str, api_key: str) -> dict[str, Any] | None:
    global _OPENROUTER_PRICING_CACHE
    if _OPENROUTER_PRICING_CACHE is None:
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        try:
            response = httpx.get(OPENROUTER_MODELS_URL, headers=headers, timeout=30)
            response.raise_for_status()
            rows = response.json().get("data", [])
        except Exception:
            rows = []
        _OPENROUTER_PRICING_CACHE = {
            str(row.get("id")): row.get("pricing", {})
            for row in rows
            if isinstance(row, dict) and row.get("id")
        }
    pricing = _OPENROUTER_PRICING_CACHE.get(model)
    return pricing if isinstance(pricing, dict) else None


def _estimate_openrouter_usage_cost_usd(usage: Any, pricing: dict[str, Any] | None) -> float | None:
    if not isinstance(usage, dict) or not pricing:
        return None
    prompt_tokens = _int_usage(usage, "prompt_tokens")
    completion_tokens = _int_usage(usage, "completion_tokens")
    total = 0.0
    prompt_price = _float_or_none(pricing.get("prompt")) or 0.0
    completion_price = _float_or_none(pricing.get("completion")) or 0.0
    cache_read_price = _float_or_none(pricing.get("input_cache_read"))
    cached_tokens = _cached_prompt_tokens(usage) or 0
    if cache_read_price is not None and cached_tokens:
        uncached = max(0, prompt_tokens - cached_tokens)
        total += uncached * prompt_price
        total += cached_tokens * cache_read_price
    else:
        total += prompt_tokens * prompt_price
    total += completion_tokens * completion_price
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning_tokens = _int_usage(details, "reasoning_tokens")
        reasoning_price = _float_or_none(pricing.get("internal_reasoning")) or completion_price
        total += reasoning_tokens * reasoning_price
    return round(total, 6)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pricing_for_model(model: str) -> dict[str, float] | None:
    normalized = model.rsplit("/", 1)[-1]
    if normalized in GEMINI_PRICING_PER_MILLION_USD:
        return GEMINI_PRICING_PER_MILLION_USD[normalized]
    for prefix, pricing in GEMINI_PRICING_PER_MILLION_USD.items():
        if normalized.startswith(prefix):
            return pricing
    return None


def _cached_prompt_tokens(usage: Any) -> int | None:
    """Cached (prefix-hit) prompt tokens if the provider reports them.

    OpenRouter/OpenAI-compatible providers surface this as
    usage.prompt_tokens_details.cached_tokens; some also send cached_tokens
    at the top level. Returns None when the provider reports no cache info.
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and "cached_tokens" in details:
        return _int_usage(details, "cached_tokens")
    if "cached_tokens" in usage:
        return _int_usage(usage, "cached_tokens")
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
