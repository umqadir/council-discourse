from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pipeline import db
from pipeline.coverage import missing_meetings

CHAMBERS = "Council Chambers - City Hall"
NOW = datetime(2026, 10, 24, 12, 0)


def _event(conn, event_id, body, day, time, location=CHAMBERS, *, recording=None, done=False, status="Final", **extra):
    db.upsert_meeting(
        conn,
        {
            "meeting_key": recording or f"event-{event_id}",
            "legistar_event_id": event_id,
            "viebit_filename": recording,
            "body_name": body,
            "event_date": f"{day}T00:00:00",
            "event_time": time,
            "event_location": location,
            "agenda_status_name": status,
            **extra,
        },
    )
    if done:
        db.update_meeting(conn, recording or f"event-{event_id}", {"chapterize_status": "chapterized"})


def _ids(conn):
    return [m["event_id"] for m in missing_meetings(conn, NOW)]


def test_joint_hearing_is_covered_by_its_published_sibling(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    _event(conn, 1, "Committee on Transportation and Infrastructure", "2026-10-21", "10:00 AM",
           recording="NYCC-PV-CH-CHA_261021-100340", done=True)
    _event(conn, 2, "Committee on Public Safety", "2026-10-21", "10:00 AM")
    assert _ids(conn) == []


def test_back_to_back_votes_in_one_room_are_not_hidden(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    _event(conn, 1, "Committee on Finance", "2026-10-22", "10:00 AM", recording="NYCC-PV-CH-CHA_261022-101209", done=True)
    _event(conn, 2, "Committee on Health", "2026-10-22", "10:15 AM")
    assert _ids(conn) == [2]


def test_deferred_recent_and_old_events_are_skipped(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    _event(conn, 1, "Committee on Immigration", "2026-10-22", "10:45 AM", status="Deferred")
    _event(conn, 2, "Committee on Aging", "2026-10-24", "10:00 AM")  # within the grace period
    _event(conn, 3, "Committee on Land Use", "2026-10-01", "1:00 PM")  # outside the window
    assert _ids(conn) == []


def test_unmatched_recordings_that_day_are_named(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    _event(conn, 1, "Committee on Health", "2026-10-22", "10:15 AM")
    db.upsert_meeting(conn, {"meeting_key": "NYCC-PV-CH-CHA_261022-Health", "viebit_filename": "NYCC-PV-CH-CHA_261022-Health"})
    (missing,) = missing_meetings(conn, NOW)
    assert "NYCC-PV-CH-CHA_261022-Health" in missing["cause"]
