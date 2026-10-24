from __future__ import annotations

import os
import sqlite3
import sys

from . import db
from .legistar import (
    LegistarClient,
    body_slug,
    body_suffix_fit,
    filename_matches_event,
    infer_meeting_type,
    meeting_slug,
    parse_event_datetime,
    viebit_filename_from_url,
)
from .matching import HOLD_NOTE_PREFIX, EventSlot, MatchPlan, RecordingSlot, plan_matches
from .utils import load_dotenv
from .utils import with_retries
from .viebit import (
    fetch_rss,
    filename_body_suffix,
    normalize_filename,
    parse_filename_date,
    parse_filename_timestamp,
    resolve_viebit_hash,
)

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
                "viebit_cdn_filename": item.cdn_filename,
                "discover_status": "discovered",
            },
        )
    return len(items)


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
    """Attach the recording the clerk linked to this event, if it is plausible.

    Events without a clerk link are paired later, one-to-one, by
    match_recordings once every source has been read.
    """
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
            from .legistar import extract_viebit_filenames_from_insite_html

            filename = _closest_plausible_filename(extract_viebit_filenames_from_insite_html(html), event)
            if filename:
                values["viebit_filename"] = filename
    if values.get("viebit_filename") and not _clerk_link_allowed(conn, event.event_id, str(values["viebit_filename"])):
        values.pop("viebit_filename")
    if values.get("viebit_filename"):
        values["match_source"] = "legistar"
    if values.get("viebit_filename") and not values.get("viebit_hash"):
        try:
            values["viebit_hash"] = resolve_viebit_hash(str(values["viebit_filename"]))
        except Exception:
            pass


def _filename_plausible_for_event(filename: str, event) -> bool:
    return filename_matches_event(
        filename,
        event.event_date,
        event.event_time,
        event.location,
        tolerance_minutes=150,
        body_name=event.body_name,
    )


def _closest_plausible_filename(filenames: list[str], event) -> str | None:
    """Of the videos an InSite page links, the one starting closest to the event."""
    event_dt = parse_event_datetime(event.event_date, event.event_time)
    ranked = []
    for filename in filenames:
        if not _filename_plausible_for_event(filename, event):
            continue
        start = parse_filename_timestamp(filename)
        if start and event_dt:
            rank = abs((start - event_dt).total_seconds())
        else:
            fit = body_suffix_fit(filename_body_suffix(filename), event.body_name)
            rank = float(fit) if fit is not None else float("inf")
        ranked.append((rank, filename))
    return min(ranked)[1] if ranked else None


def _clerk_link_allowed(conn: sqlite3.Connection, event_id: int, filename: str) -> bool:
    """Keep pairs one-to-one and never swap the recording under processed work."""
    owner = conn.execute(
        "SELECT meeting_key, legistar_event_id FROM meetings WHERE viebit_filename = ?", (filename,)
    ).fetchone()
    if owner and owner["legistar_event_id"] is not None and int(owner["legistar_event_id"]) != int(event_id):
        print(
            f"::warning::Legistar links {filename} to event {event_id}, but it already belongs to "
            f"event {owner['legistar_event_id']}; keeping the existing pairing",
            file=sys.stderr,
            flush=True,
        )
        return False
    current = conn.execute(
        "SELECT meeting_key, viebit_filename, fetch_status FROM meetings WHERE legistar_event_id = ?", (event_id,)
    ).fetchone()
    if (
        current
        and current["viebit_filename"]
        and current["viebit_filename"] != filename
        and current["fetch_status"] != "pending"
    ):
        print(
            f"::warning::Legistar now links event {event_id} to {filename}, but {current['meeting_key']} "
            "is already processed under it; not switching (reset-meeting, then detach, to re-pair)",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def match_recordings(conn: sqlite3.Connection, coverage_start: str | None = None) -> MatchPlan:
    """Pair unmatched recordings with unmatched events, one-to-one, closest first.

    Runs after both discovery sources and needs no Legistar token: events and
    recordings both come from the registry. Existing pairs are never changed
    here; recordings that look wrong for the only event they could belong to
    are held and surfaced (warning + last_error note) instead of linked.
    """
    floor = (coverage_start or os.environ.get("COUNCIL_COVERAGE_START", "2026-06-20"))[:10]
    rows = conn.execute(
        """
        SELECT meeting_key, legistar_event_id, viebit_filename, viebit_pub_date, duration_seconds,
               body_name, event_date, event_time, event_location, last_error
        FROM meetings
        WHERE legistar_event_id IS NOT NULL OR viebit_filename IS NOT NULL
        """
    ).fetchall()
    events: list[EventSlot] = []
    recordings: list[RecordingSlot] = []
    unmatched: dict[str, sqlite3.Row] = {}
    for row in rows:
        if row["legistar_event_id"] is not None and str(row["event_date"] or "")[:10] >= floor:
            events.append(
                EventSlot(
                    event_id=int(row["legistar_event_id"]),
                    body_name=row["body_name"],
                    event_date=row["event_date"],
                    event_time=row["event_time"],
                    location=row["event_location"],
                    linked_filename=row["viebit_filename"],
                )
            )
        if row["viebit_filename"]:
            day = parse_filename_date(str(row["viebit_filename"]))
            if day is None or day.isoformat() < floor:
                continue
            recordings.append(
                RecordingSlot(
                    filename=str(row["viebit_filename"]),
                    pub_date=row["viebit_pub_date"],
                    duration_seconds=row["duration_seconds"],
                    linked_event_id=row["legistar_event_id"],
                )
            )
            if row["legistar_event_id"] is None:
                unmatched[str(row["viebit_filename"])] = row

    plan = plan_matches(events, recordings)
    for event_id, filename, reason in plan.links:
        print(f"match: {filename} -> event {event_id} ({reason})", file=sys.stderr, flush=True)
        db.upsert_meeting(
            conn,
            {"legistar_event_id": event_id, "viebit_filename": filename, "match_source": plan.sources[filename]},
        )
    # A discover-time hold describes an unpaired recording; once the recording
    # is paired (here or by the clerk's link) the note is stale.
    conn.execute(
        "UPDATE meetings SET last_error = NULL WHERE legistar_event_id IS NOT NULL AND last_error LIKE ?",
        (HOLD_NOTE_PREFIX + "%",),
    )
    conn.commit()
    for filename, row in unmatched.items():
        if any(link[1] == filename for link in plan.links):
            continue
        note = plan.holds.get(filename)
        current = str(row["last_error"] or "")
        if note:
            print(f"::warning::recording {filename} not matched: {note}", file=sys.stderr, flush=True)
            if current != HOLD_NOTE_PREFIX + note:
                db.update_meeting(conn, str(row["meeting_key"]), {"last_error": HOLD_NOTE_PREFIX + note})
        elif current.startswith(HOLD_NOTE_PREFIX):
            db.update_meeting(conn, str(row["meeting_key"]), {"last_error": None})
    return plan


# Columns that describe the Legistar event (not the recording or its processing).
EVENT_COLUMNS = (
    "legistar_event_id",
    "legistar_event_guid",
    "event_last_modified_utc",
    "body_name",
    "event_date",
    "event_time",
    "event_location",
    "agenda_pdf_url",
    "minutes_pdf_url",
    "insite_url",
    "event_video_path",
    "agenda_status_name",
    "minutes_status_name",
    "meeting_type",
    "meeting_slug",
    "body_slug",
    "event_topic",
)


def detach_event(conn: sqlite3.Connection, meeting_key: str) -> str:
    """Split a wrongly paired recording row back into a recording and an event.

    The recording row keeps its key, artifacts and statuses but loses the
    event fields; the event moves to its own event-<id> row so discovery can
    pair it again. Run `reset-meeting` on the row first so the durable R2
    result record cannot re-merge the old pairing. Returns the event row key.
    """
    row = db.get_meeting(conn, meeting_key)
    if row["legistar_event_id"] is None or not row["viebit_filename"]:
        raise ValueError(f"{meeting_key} is not a paired recording row")
    event_values = {column: row[column] for column in EVENT_COLUMNS}
    db.update_meeting(conn, meeting_key, {**{column: None for column in EVENT_COLUMNS}, "match_source": None})
    event_row = db.upsert_meeting(conn, {**event_values, "discover_status": "discovered"})
    return str(event_row["meeting_key"])


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
