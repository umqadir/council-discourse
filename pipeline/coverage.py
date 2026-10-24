"""Council meetings that took place but have no page.

Registry only, no API calls. A recent event counts as covered when it is
chapterized, or when a chapterized meeting began within JOINT_MINUTES of it in
the same room that day: a joint hearing gets one recording, filed under one
of its committees.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .legistar import parse_event_datetime
from .matching import VIEBIT_TIMEZONE
from .viebit import parse_filename_date

GRACE_HOURS = 24
WINDOW_DAYS = 7
JOINT_MINUTES = 10


def _location(row: sqlite3.Row) -> str:
    return " ".join(str(row["event_location"] or "").lower().split())


def missing_meetings(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Final, non-deferred events from the last WINDOW_DAYS with no page after GRACE_HOURS."""
    now = now or datetime.now(VIEBIT_TIMEZONE).replace(tzinfo=None)
    rows = conn.execute("SELECT * FROM meetings").fetchall()
    events = [r for r in rows if r["legistar_event_id"] is not None]
    published = [
        (parse_event_datetime(r["event_date"], r["event_time"]), _location(r))
        for r in events
        if r["chapterize_status"] == "chapterized"
    ]
    unmatched = [r for r in rows if r["viebit_filename"] and r["legistar_event_id"] is None]

    missing = []
    for row in events:
        start = parse_event_datetime(row["event_date"], row["event_time"])
        if (
            start is None
            or row["agenda_status_name"] != "Final"
            or row["chapterize_status"] == "chapterized"
            or not now - timedelta(days=WINDOW_DAYS) <= start <= now - timedelta(hours=GRACE_HOURS)
        ):
            continue
        location = _location(row)
        if any(
            when and loc == location and abs((when - start).total_seconds()) <= JOINT_MINUTES * 60
            for when, loc in published
        ):
            continue
        if row["viebit_filename"]:
            cause = f"recording {row['viebit_filename']} is linked but not published"
            if row["last_error"]:
                cause += f": {row['last_error']}"
        else:
            same_day = [
                r for r in unmatched if parse_filename_date(r["viebit_filename"]) == start.date()
            ]
            cause = "no recording linked"
            if same_day:
                cause += "; unmatched recordings that day: " + ", ".join(
                    r["viebit_filename"] + (f" ({r['last_error']})" if r["last_error"] else "") for r in same_day
                )
        missing.append(
            {
                "event_id": row["legistar_event_id"],
                "body": row["body_name"],
                "start": start,
                "location": row["event_location"],
                "cause": cause,
            }
        )
    return sorted(missing, key=lambda m: m["start"])


def missing_meetings_markdown(missing: list[dict]) -> str:
    lines = [
        "| Meeting | Location | Why |",
        "|---|---|---|",
    ]
    for m in missing:
        when = m["start"].strftime("%b %-d, %-I:%M %p")
        lines.append(f"| {when} {m['body']} (event {m['event_id']}) | {m['location'] or ''} | {m['cause']} |")
    return "\n".join(lines)
