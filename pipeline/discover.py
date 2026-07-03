from __future__ import annotations

import os
import sqlite3

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
from .viebit import fetch_rss, normalize_filename, resolve_viebit_hash

LEGISTAR_CURSOR_KEY = "legistar_event_last_modified_utc"
LEGISTAR_BOOTSTRAP_CURSOR = "1970-01-01T00:00:00"


def discover_viebit_rss(conn: sqlite3.Connection, rss_url: str | None = None) -> int:
    items = fetch_rss(rss_url) if rss_url else fetch_rss()
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
            print(f"  detail-page fetch failed for event {event.event_id}: {exc}", flush=True)
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
        return 0, True

    cursor = db.get_meta(conn, LEGISTAR_CURSOR_KEY) or LEGISTAR_BOOTSTRAP_CURSOR
    client = LegistarClient(token)
    latest_cursor = cursor
    count = 0
    try:
        if start_date and end_date:
            events = client.get_events_by_date_window(start_date, end_date)
        else:
            events = client.get_events_modified_since(cursor)
        for event in events:
            values = _values_from_event(event)
            _attach_video_filename(conn, client, event, values)
            db.upsert_meeting(conn, values)
            if event.last_modified_utc and event.last_modified_utc > latest_cursor:
                latest_cursor = event.last_modified_utc
            count += 1
        if not (start_date and end_date) and latest_cursor != cursor:
            db.set_meta(conn, LEGISTAR_CURSOR_KEY, latest_cursor)
            conn.commit()
    finally:
        client.close()
    return count, False
