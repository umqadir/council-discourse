"""Preflight provider checks for the production pipeline.

Gates the paid batch on real, *persistent* problems - exhausted OpenRouter
credits, or a Mistral/Google auth/billing failure - while tolerating transient
upstream blips (a momentary 5xx or timeout) so a single provider hiccup does
not fail an otherwise-healthy multi-hour run. This mirrors the transient-retry
policy the rest of the pipeline already uses (see ``gemini._post_with_retry``
and the deploy step's retry around Cloudflare Pages 500s); an auth/billing
failure (401/402/403) is persistent, so it is never retried and fails fast.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

# HTTP statuses that signal a transient upstream condition worth retrying.
# Auth/billing failures (401/402/403) are deliberately excluded: they are
# persistent, need a human, and retrying only wastes time.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_TIMEOUT_SECONDS = 30

# Floor must sit at/below the dashboard auto-top-up trigger ($2): a floor above
# it creates a dead zone where preflight refuses to spend, the balance never
# drops to the trigger, and top-up never fires. A mid-batch 402 is self-healing
# (artifacts persist and the meeting resumes next run), so the lower floor is safe.
OPENROUTER_CREDIT_FLOOR = 2.0


def _get_with_retry(
    url: str,
    key: str | None = None,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    sleep: Callable[[float], Any] = time.sleep,
) -> Any:
    """GET ``url`` as JSON, retrying transient upstream failures with backoff.

    Retries socket timeouts, connection errors, and transient HTTP statuses
    (429/5xx); re-raises everything else (notably 401/402/403 auth/billing) on
    the first failure so a real problem still fails the run promptly.
    """
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_STATUS and attempt < max_attempts:
                sleep(min(30, 2**attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < max_attempts:
                sleep(min(30, 2**attempt))
                continue
            raise
    raise RuntimeError("HTTP request did not return a response")


def run_preflight(*, sleep: Callable[[float], Any] = time.sleep) -> int:
    """Return 0 if every provider is healthy, 1 if any real problem is found."""
    failed = False
    try:
        data = _get_with_retry(
            "https://openrouter.ai/api/v1/credits",
            os.environ["OPENROUTER_API_KEY"],
            sleep=sleep,
        )["data"]
        remaining = float(data["total_credits"]) - float(data["total_usage"])
        print(f"openrouter credits remaining: ${remaining:.2f}")
        if remaining < OPENROUTER_CREDIT_FLOOR:
            print(
                "::error::OpenRouter credits below $2 - auto-top-up should fire; "
                "if it did not, add credits at https://openrouter.ai/settings/credits"
            )
            failed = True
    except Exception as exc:
        print(f"::error::OpenRouter preflight failed: {exc}")
        failed = True
    try:
        _get_with_retry("https://api.mistral.ai/v1/models", os.environ["MISTRAL_API_KEY"], sleep=sleep)
        print("mistral auth: ok")
    except Exception as exc:
        print(f"::error::Mistral preflight failed (check key/billing): {exc}")
        failed = True
    try:
        _get_with_retry(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={os.environ['GOOGLE_API_KEY']}&pageSize=1",
            sleep=sleep,
        )
        print("google auth: ok")
    except Exception as exc:
        print(f"::error::Google preflight failed (naming verification depends on it): {exc}")
        failed = True
    return 1 if failed else 0


def main() -> int:
    return run_preflight()


if __name__ == "__main__":
    sys.exit(main())
