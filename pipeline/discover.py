from __future__ import annotations

import os
import sqlite3
import sys

from . import db
from .legistar import (
    LegistarClient,
    body_slug,
    filename_matches_event,
    infer_meeting_type,
    meeting_slug,
    viebit_filename_from_url,
)
from .utils import load_dotenv
from .utils import with_retries
from .viebit import fetch_rss, normalize_filename, resolve_viebit_hash

LEGISTAR_CURSOR_KEY = "legistar_event_last_modified_utc"
# Forward-only coverage (PLAN.md section 12): bootstrap from just before the
# first covered month rather than sweeping decades of Legistar history.
LEGISTAR_BOOTSTRAP_CURSOR = os.environ.get("COUNCIL_COVERAGE_START", "2026-06-20T00:00:00")


def discover_viebit_rss(conn: sqlite3.Connection, rss_url: str | None = None) -> int:
    items = with_retries(lambda: fetch_rss(rss_url) if rss_url else fetch_rss())
    if not items:
        raise RuntimeError("Viebit RSS parsed to zero items")
    for item in items:
        db.upsert_meeting(
            conn,
            {
                "viebit_filename": normalize_filename(item.filename),
                "viebit_hash": item.hash,
                "viebit_pub_date": item.pub_date,
                "discover_status": "discovered",
            },
        )
    return len(items)


def _join_unmatched_rss_by_backstop(conn: sqlite3.Connection, event_values: dict) -> str | None:
    rows = conn.execute(
        """
        SELECT meeting_key, viebit_filename
        FROM meetings
        WHERE viebit_filename IS NOT NULL
          AND legistar_event_id IS NULL
        """
    ).fetchall()
    for row in rows:
        if filename_matches_event(
            str(row["viebit_filename"]),
            event_values.get("event_date"),
            event_values.get("event_time"),
            event_values.get("event_location"),
        ):
            return str(row["viebit_filename"])
    return None


def _values_from_event(event) -> dict:
    classification = infer_meeting_type(
        event.body_name,
        event.agenda_status_name,
        event.minutes_status_name,
    )
    return {
        "legistar_event_id": event.event_id,
        "legistar_event_guid": event.event_guid,
        "event_last_modified_utc": event.last_modified_utc,
        "body_name": event.body_name,
        "event_date": event.event_date,
        "event_time": event.event_time,
        "event_location": event.location,
        "agenda_pdf_url": event.agenda_pdf_url,
        "minutes_pdf_url": event.minutes_pdf_url,
        "insite_url": event.insite_url,
        "event_video_path": event.video_path,
        "agenda_status_name": event.agenda_status_name,
        "minutes_status_name": event.minutes_status_name,
        "meeting_type": classification,
        "meeting_slug": meeting_slug(event.event_date, event.event_time, event.body_name),
        "body_slug": body_slug(event.body_name),
        "discover_status": "discovered",
    }


def _attach_video_filename(conn: sqlite3.Connection, client: LegistarClient, event, values: dict) -> None:
    filename = viebit_filename_from_url(event.video_path or "") if event.video_path else None
    if filename and _filename_plausible_for_event(filename, event):
        values["viebit_filename"] = filename
    if not values.get("viebit_filename"):
        try:
            html = client.fetch_meeting_detail_html(event)
        except Exception as exc:
            # A dead/retired InSite page (404/410/timeouts) means no video info for
            # this event right now; discovery must continue with the rest.
            print(f"  detail-page fetch failed for event {event.event_id}: {exc}", file=sys.stderr, flush=True)
            html = None
        if html:
            from .legistar import extract_viebit_filename_from_insite_html

            filename = extract_viebit_filename_from_insite_html(html)
            if filename and _filename_plausible_for_event(filename, event):
                values["viebit_filename"] = filename
    if values.get("viebit_filename") and not values.get("viebit_hash"):
        try:
            values["viebit_hash"] = resolve_viebit_hash(str(values["viebit_filename"]))
        except Exception:
            pass
    if not values.get("viebit_filename"):
        backstop = _join_unmatched_rss_by_backstop(conn, values)
        if backstop:
            values["viebit_filename"] = backstop


def _filename_plausible_for_event(filename: str, event) -> bool:
    return filename_matches_event(
        filename,
        event.event_date,
        event.event_time,
        event.location,
        tolerance_minutes=150,
    )


def discover_legistar(
    conn: sqlite3.Connection,
    token: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[int, bool]:
    load_dotenv()
    token = token or os.environ.get("LEGISTAR_TOKEN")
    if not token:
        if os.environ.get("GITHUB_ACTIONS"):
            print("::error::LEGISTAR_TOKEN unset; skipping Legistar discovery in CI", file=sys.stderr, flush=True)
        return 0, True

    cursor = db.get_meta(conn, LEGISTAR_CURSOR_KEY) or LEGISTAR_BOOTSTRAP_CURSOR
    client = LegistarClient(token)
    latest_cursor = cursor
    count = 0
    try:
        if start_date and end_date:
            events = with_retries(lambda: client.get_events_by_date_window(start_date, end_date))
        else:
            events = with_retries(lambda: client.get_events_modified_since(cursor))
        for event in events:
            try:
                values = _values_from_event(event)
                _attach_video_filename(conn, client, event, values)
                _attach_event_topic(client, event, values)
                db.upsert_meeting(conn, values)
            except Exception as exc:
                # One malformed event (bad date strings, dead pages, missing fields)
                # must never abort the sync of everything else.
                print(f"  skipping malformed event {getattr(event, 'event_id', '?')}: {exc}", file=sys.stderr, flush=True)
                continue
            if event.last_modified_utc and event.last_modified_utc > latest_cursor:
                latest_cursor = event.last_modified_utc
            count += 1
        if not (start_date and end_date) and latest_cursor != cursor:
            db.set_meta(conn, LEGISTAR_CURSOR_KEY, latest_cursor)
            conn.commit()
        _backfill_event_topics(conn, client)
    finally:
        client.close()
    return count, False


def _attach_event_topic(client: LegistarClient, event, values: dict) -> None:
    """Store the first-agenda-item topic; '' marks 'checked, nothing usable'."""
    if values.get("meeting_type") == "STATED_MEETING":
        # Stated meetings vote on dozens of items; the first is always procedural.
        values["event_topic"] = ""
        return
    from .legistar import extract_event_topic

    try:
        items = client.get_event_items(event.event_id)
        values["event_topic"] = extract_event_topic(items, values.get("body_name")) or ""
    except Exception as exc:
        # Leave NULL so the backfill pass retries later; never block discovery.
        print(f"  topic fetch failed for event {event.event_id}: {exc}", file=sys.stderr, flush=True)


def _backfill_event_topics(conn: sqlite3.Connection, client: LegistarClient, limit: int = 60) -> None:
    from .legistar import extract_event_topic

    rows = conn.execute(
        """
        SELECT meeting_key, legistar_event_id, body_name, meeting_type FROM meetings
        WHERE legistar_event_id IS NOT NULL
          AND viebit_filename IS NOT NULL
          AND event_topic IS NULL
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        if row["meeting_type"] == "STATED_MEETING":
            db.update_meeting(conn, str(row["meeting_key"]), {"event_topic": ""})
            continue
        try:
            items = client.get_event_items(int(row["legistar_event_id"]))
            topic = extract_event_topic(items, row["body_name"]) or ""
            db.update_meeting(conn, str(row["meeting_key"]), {"event_topic": topic})
        except Exception as exc:
            print(
                f"  topic backfill failed for event {row['legistar_event_id']}: {exc}",
                file=sys.stderr,
                flush=True,
            )
