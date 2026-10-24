"""Preflight provider checks for the production pipeline.

Gates the paid batch on real, *persistent* problems - exhausted OpenRouter
credits, or a Mistral/Google auth/billing failure - while tolerating transient
upstream blips (a momentary 5xx or timeout) so a single provider hiccup does
not fail an otherwise-healthy multi-hour run. This mirrors the transient-retry
policy the rest of the pipeline already uses (see ``gemini._post_with_retry``
and the deploy step's retry around Cloudflare Pages 500s); an auth/billing
failure (401/402/403) is persistent, so it is never retried and fails fast.

It also enforces a daily spend ceiling. Spend is the day's increase in the
registry's recorded per-meeting ``cost_usd``, booked by merge-results into the
registry ``meta`` table (``spend_usd:<date>``; New York calendar day). The
per-row ``updated_at`` cannot date a cost: discover touches every row each run.
Once today's spend reaches ``COUNCIL_DAILY_BUDGET_USD`` (default $15), the
pending selection is empty and the run stays green with a warning.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO
from zoneinfo import ZoneInfo

from .config import REGISTRY_DB

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

DAILY_BUDGET_ENV = "COUNCIL_DAILY_BUDGET_USD"
DEFAULT_DAILY_BUDGET_USD = 15.0
SPEND_LEDGER_PREFIX = "spend_usd:"
SPEND_DAY_TZ = ZoneInfo("America/New_York")


def daily_budget_usd() -> float:
    raw = os.environ.get(DAILY_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_DAILY_BUDGET_USD
    try:
        return float(raw)
    except ValueError:
        print(f"::warning::ignoring non-numeric {DAILY_BUDGET_ENV}={raw!r}; using ${DEFAULT_DAILY_BUDGET_USD:.2f}")
        return DEFAULT_DAILY_BUDGET_USD


def spend_day(now: datetime | None = None) -> str:
    return (now or datetime.now(SPEND_DAY_TZ)).astimezone(SPEND_DAY_TZ).date().isoformat()


def spend_for_day(conn: sqlite3.Connection, day: str | None = None) -> float:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (SPEND_LEDGER_PREFIX + (day or spend_day()),)).fetchone()
    try:
        return float(row[0]) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def record_spend(conn: sqlite3.Connection, amount_usd: float, day: str | None = None) -> None:
    """Add newly recorded cost to the day's ledger (caller commits)."""
    if amount_usd <= 0:
        return
    day = day or spend_day()
    total = round(spend_for_day(conn, day) + amount_usd, 6)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SPEND_LEDGER_PREFIX + day, str(total)),
    )


def budget_exhausted(conn: sqlite3.Connection, *, log: TextIO | None = None) -> bool:
    spent, cap = spend_for_day(conn), daily_budget_usd()
    if spent < cap:
        return False
    print(
        f"::warning::daily spend ${spent:.2f} has reached the ${cap:.2f} budget ({DAILY_BUDGET_ENV}); "
        "selecting no meetings this run",
        file=log or sys.stderr,
        flush=True,
    )
    return True


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


def run_preflight(*, sleep: Callable[[float], Any] = time.sleep, db_path: Path | None = None) -> int:
    """Return 0 if every provider is healthy, 1 if any real problem is found.

    An exhausted daily budget is reported as a warning, not a failure:
    discovery then selects nothing and the run stays green.
    """
    failed = False
    db_path = db_path or REGISTRY_DB
    if db_path.exists():
        from . import db

        conn = db.connect(db_path)
        print(f"daily spend: ${spend_for_day(conn):.2f} of ${daily_budget_usd():.2f} budget")
        budget_exhausted(conn, log=sys.stdout)
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
        print(f"::error::Google preflight failed (speaker naming depends on it): {exc}")
        failed = True
    return 1 if failed else 0


def main() -> int:
    return run_preflight()


if __name__ == "__main__":
    sys.exit(main())
