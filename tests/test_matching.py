"""Recording <-> event pairing, checked against the October 22 and September 24
2026 recordings that were published under the wrong meetings."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline import db
from pipeline.discover import _clerk_link_allowed, _closest_plausible_filename, detach_event, match_recordings
from pipeline.legistar import (
    LegistarEvent,
    body_suffix_fit,
    filename_matches_event,
    location_exact_room,
    time_match_cost,
)
from pipeline.matching import EventSlot, RecordingSlot, plan_matches, recording_max_minutes
from pipeline.viebit import filename_body_suffix, parse_filename_date, parse_filename_timestamp

CHAMBERS = "Council Chambers - City Hall"
COMMITTEE_ROOM = "Committee Room - City Hall"
OCT22 = "2026-10-22T00:00:00"
SEP24 = "2026-09-24T00:00:00"

# Legistar events on 2026-10-22 as stored in data/registry.db.
OCT22_EVENTS = [
    (22817, "Committee on Education", "9:00 AM", CHAMBERS),
    (22792, "Committee on Rules, Privileges, Elections, Standards and Ethics", "9:30 AM", COMMITTEE_ROOM),
    (22774, "Committee on Finance", "10:00 AM", CHAMBERS),
    (22800, "Committee on Parks and Recreation", "10:00 AM", COMMITTEE_ROOM),
    (22801, "Committee on Health", "10:15 AM", CHAMBERS),
    (22807, "Committee on Criminal Justice", "10:30 AM", CHAMBERS),
    (22802, "Committee on Cultural Affairs, Libraries and International Relations", "10:30 AM", COMMITTEE_ROOM),
    (22806, "Committee on Immigration", "10:45 AM", CHAMBERS),
    (22805, "Committee on Housing and Buildings", "11:00 AM", CHAMBERS),
    (22812, "City Council", "1:30 PM", CHAMBERS),
]
# Recordings posted on 2026-10-22 itself (time-stamped), with RSS pub dates.
OCT22_STAMPED = {
    "NYCC-PV-CH-CHA_261022-101209": "2026-10-22T14:22:46+00:00",  # 9-min Finance vote
    "NYCC-PV-CH-COM_261022-121338": "2026-10-22T16:17:57+00:00",  # 4 min of mic checks
    "NYCC-PV-CH-COM_261022-124320": "2026-10-22T17:34:24+00:00",  # pre-stated press conference
}
# Re-posted the next day with the body in place of the time.
OCT22_SUFFIXED = {
    "NYCC-PV-CH-CHA_261022-CJ": 22807,
    "NYCC-PV-CH-CHA_261022-Education": 22817,
    "NYCC-PV-CH-CHA_261022-Finance": 22774,
    "NYCC-PV-CH-CHA_261022-Health": 22801,
    "NYCC-PV-CH-CHA_261022-Housing": 22805,
    "NYCC-PV-CH-COM_261022-Cultural": 22802,
    "NYCC-PV-CH-COM_261022-Parks": 22800,
    "NYCC-PV-CH-COM_261022-Rules": 22792,
}


def _events(spec, day, linked=None):
    linked = linked or {}
    return [
        EventSlot(event_id, body, day, time, location, linked_filename=linked.get(event_id))
        for event_id, body, time, location in spec
    ]


def _oct22_events(linked=None):
    return _events(OCT22_EVENTS, OCT22, {22812: "NYCC-PV-CH-CHA_261022-141329", **(linked or {})})


def _links(plan):
    return {filename: event_id for event_id, filename, _ in plan.links}


# --- filename parsing -------------------------------------------------------


@pytest.mark.parametrize("filename,suffix", [(name, name.rsplit("-", 1)[1]) for name in OCT22_SUFFIXED])
def test_body_suffixed_filenames_parse_to_date_and_suffix(filename, suffix) -> None:
    assert parse_filename_timestamp(filename) is None
    assert parse_filename_date(filename) == date(2026, 10, 22)
    assert parse_filename_date(filename + ".mp4") == date(2026, 10, 22)
    assert filename_body_suffix(filename) == suffix


def test_time_stamped_filenames_have_no_body_suffix() -> None:
    assert filename_body_suffix("NYCC-PV-CH-CHA_261022-101209") is None
    assert parse_filename_date("NYCC-PV-CH-CHA_261022-101209") == date(2026, 10, 22)
    assert parse_filename_date("NYCC-OFFSITE_260928") == date(2026, 9, 28)
    assert parse_filename_date("not-a-recording") is None


def test_body_suffix_fit_is_derived_from_body_names() -> None:
    assert body_suffix_fit("Education", "Committee on Education") == 0
    assert body_suffix_fit("Finance", "Committee on Finance") == 0
    assert body_suffix_fit("CJ", "Committee on Criminal Justice") == 1
    assert body_suffix_fit("Parks", "Committee on Parks and Recreation") == 2
    assert body_suffix_fit("Cultural", "Committee on Cultural Affairs, Libraries and International Relations") == 2
    assert body_suffix_fit("Rules", "Committee on Rules, Privileges, Elections, Standards and Ethics") == 2
    assert body_suffix_fit("Housing", "Committee on Housing and Buildings") == 2
    # Another word of a longer name fits, but worse than the whole name.
    assert body_suffix_fit("Education", "Committee on Higher Education") == 3
    assert body_suffix_fit("Health", "Committee on Mental Health and Substance Use") == 3
    assert body_suffix_fit("CJ", "Committee on Civil and Human Rights") is None
    assert body_suffix_fit("Health", "Committee on Hospitals") is None
    assert body_suffix_fit("Stated", "City Council") == 0


def test_location_exact_room_reads_the_hearing_room_number() -> None:
    assert location_exact_room("250 Broadway - 8th Floor - Hearing Room 2") == "NYCC-250-8-2"
    assert location_exact_room(CHAMBERS) == "NYCC-PV-CH-CHA"
    assert location_exact_room(COMMITTEE_ROOM) == "NYCC-PV-CH-COM"
    assert location_exact_room("250 Broadway - 14th Floor") is None


def test_time_match_cost_window_is_tight() -> None:
    finance = (OCT22, "10:00 AM", CHAMBERS)
    education = (OCT22, "9:00 AM", CHAMBERS)
    assert time_match_cost("NYCC-PV-CH-CHA_261022-101209", *finance) == pytest.approx(12.15)
    # 72 minutes after the Education start: no longer a candidate (was within +-150).
    assert time_match_cost("NYCC-PV-CH-CHA_261022-101209", *education) is None
    # Wrong building is never a candidate.
    assert time_match_cost("NYCC-PV-CH-COM_261022-121338", *finance) is None


def test_recording_max_minutes_uses_pub_date_bound_and_duration() -> None:
    clip = RecordingSlot("NYCC-PV-CH-COM_261022-121338", pub_date=OCT22_STAMPED["NYCC-PV-CH-COM_261022-121338"])
    assert recording_max_minutes(clip) == pytest.approx(4.32, abs=0.01)
    fetched = RecordingSlot("NYCC-PV-CH-CHA_260924-134504", duration_seconds=60.0)
    assert recording_max_minutes(fetched) == pytest.approx(1.0)
    assert recording_max_minutes(RecordingSlot("NYCC-PV-CH-CHA_261022-Education")) is None


# --- assignment -------------------------------------------------------------


def test_oct22_morning_pairs_finance_vote_and_holds_clips() -> None:
    """The state on Oct 22 before the named re-posts existed."""
    recordings = [RecordingSlot(name, pub_date=pub) for name, pub in OCT22_STAMPED.items()]
    recordings.append(RecordingSlot("NYCC-PV-CH-CHA_261022-141329", linked_event_id=22812))

    plan = plan_matches(_oct22_events(), recordings)

    # 10:12 start: the 10:00 Finance vote, not Health (10:15, would be an early
    # start) and not Education (72 minutes late).
    assert _links(plan) == {"NYCC-PV-CH-CHA_261022-101209": 22774}
    # The mic-check clip and the press conference start over two hours after
    # the Committee Room events; the old +-150-minute window accepted both.
    assert "starts 134 min after Committee on Parks" in plan.holds["NYCC-PV-CH-COM_261022-121338"]
    assert "starts 133 min after Committee on Cultural Affairs" in plan.holds["NYCC-PV-CH-COM_261022-124320"]


def test_oct22_named_reposts_claim_their_bodies_one_to_one() -> None:
    recordings = [RecordingSlot(name, pub_date=pub) for name, pub in OCT22_STAMPED.items()]
    recordings += [RecordingSlot(name) for name in OCT22_SUFFIXED]

    plan = plan_matches(_oct22_events(), recordings)

    assert _links(plan) == OCT22_SUFFIXED
    assert len(set(_links(plan).values())) == len(plan.links)
    for name in OCT22_STAMPED:
        assert name not in _links(plan)


def test_named_repost_for_an_already_paired_event_is_surfaced() -> None:
    events = _oct22_events({22774: "NYCC-PV-CH-CHA_261022-101209"})
    recordings = [
        RecordingSlot("NYCC-PV-CH-CHA_261022-101209", linked_event_id=22774),
        RecordingSlot("NYCC-PV-CH-CHA_261022-Finance"),
    ]

    plan = plan_matches(events, recordings)

    assert plan.links == []
    assert "already linked to NYCC-PV-CH-CHA_261022-101209" in plan.holds["NYCC-PV-CH-CHA_261022-Finance"]


def test_ambiguous_suffix_is_held_not_guessed() -> None:
    events = [
        EventSlot(1, "Committee on Finance", OCT22, "10:00 AM", CHAMBERS),
        EventSlot(2, "Committee on Finance", OCT22, "1:00 PM", CHAMBERS),
    ]
    plan = plan_matches(events, [RecordingSlot("NYCC-PV-CH-CHA_261022-Finance")])
    assert plan.links == []
    assert "fits several events" in plan.holds["NYCC-PV-CH-CHA_261022-Finance"]


def test_whole_body_name_beats_a_looser_suffix_for_the_same_event() -> None:
    events = [
        EventSlot(1, "Committee on Education", OCT22, "9:00 AM", CHAMBERS),
        EventSlot(2, "Committee on Higher Education", OCT22, "1:00 PM", CHAMBERS),
    ]
    recordings = [RecordingSlot("NYCC-PV-CH-CHA_261022-Educ"), RecordingSlot("NYCC-PV-CH-CHA_261022-Education")]
    plan = plan_matches(events, recordings)
    assert _links(plan) == {"NYCC-PV-CH-CHA_261022-Education": 1}
    assert "already linked" in plan.holds["NYCC-PV-CH-CHA_261022-Educ"]


def test_sep24_same_time_hearings_follow_their_hearing_rooms() -> None:
    events = _events(
        [
            (22746, "Committee on Cultural Affairs, Libraries and International Relations", "11:00 AM",
             "250 Broadway - 8th Floor - Hearing Room 2"),
            (22755, "Committee on Finance", "11:00 AM", "250 Broadway - 8th Floor - Hearing Room 1"),
            (22747, "Committee on Housing and Buildings", "11:00 AM", "250 Broadway - 8th Floor - Hearing Room 3"),
        ],
        SEP24,
        {22747: "NYCC-250-8-3_260924-111825"},
    )
    recordings = [
        RecordingSlot("NYCC-250-8-1_260924-110956", pub_date="2026-09-24T15:40:32+00:00"),
        RecordingSlot("NYCC-250-8-2_260924-111403", pub_date="2026-09-24T15:26:55+00:00"),
        RecordingSlot("NYCC-250-8-3_260924-111825", linked_event_id=22747),
    ]

    plan = plan_matches(events, recordings)

    # Closest start alone gave Cultural Affairs the 11:09 Room 1 file (Finance's vote).
    assert _links(plan) == {"NYCC-250-8-1_260924-110956": 22755, "NYCC-250-8-2_260924-111403": 22746}


def test_sep24_stated_meeting_skips_one_minute_clips() -> None:
    events = _events([(22758, "City Council", "1:30 PM", CHAMBERS)], SEP24)
    recordings = [
        RecordingSlot("NYCC-PV-CH-CHA_260924-134504", pub_date="2026-09-24T17:46:44+00:00"),
        RecordingSlot("NYCC-PV-CH-CHA_260924-134606", pub_date="2026-09-24T17:47:50+00:00"),
        RecordingSlot("NYCC-PV-CH-CHA_260924-134718", pub_date="2026-09-24T17:48:56+00:00"),
        RecordingSlot("NYCC-PV-CH-CHA_260924-134843", pub_date="2026-09-24T18:56:32+00:00"),
    ]

    plan = plan_matches(events, recordings)

    assert _links(plan) == {"NYCC-PV-CH-CHA_260924-134843": 22758}
    assert set(plan.holds) == {
        "NYCC-PV-CH-CHA_260924-134504",
        "NYCC-PV-CH-CHA_260924-134606",
        "NYCC-PV-CH-CHA_260924-134718",
    }


# --- clerk links ------------------------------------------------------------


def _event(event_id, body, time, location, day=OCT22):
    return LegistarEvent(
        event_id=event_id, event_guid=None, last_modified_utc=None, body_name=body, event_date=day,
        event_time=time, location=location, agenda_pdf_url=None, minutes_pdf_url=None, insite_url=None,
        video_path=None, agenda_status_name=None, minutes_status_name=None,
    )


def test_clerk_link_plausibility_accepts_named_reposts_for_their_body_only() -> None:
    assert filename_matches_event(
        "NYCC-PV-CH-CHA_261022-CJ", OCT22, "10:30 AM", CHAMBERS, body_name="Committee on Criminal Justice"
    )
    assert not filename_matches_event(
        "NYCC-PV-CH-CHA_261022-CJ", OCT22, "10:00 AM", CHAMBERS, body_name="Committee on Finance"
    )
    assert not filename_matches_event(
        "NYCC-PV-CH-CHA_261022-CJ", "2026-10-23T00:00:00", "10:30 AM", CHAMBERS,
        body_name="Committee on Criminal Justice",
    )


def test_insite_page_with_several_videos_uses_the_closest() -> None:
    event = _event(22774, "Committee on Finance", "10:00 AM", CHAMBERS)
    filenames = ["NYCC-PV-CH-CHA_261022-141329", "NYCC-PV-CH-CHA_261022-101209"]
    assert _closest_plausible_filename(filenames, event) == "NYCC-PV-CH-CHA_261022-101209"


def test_clerk_link_never_steals_or_swaps_a_processed_recording(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    db.upsert_meeting(conn, {"legistar_event_id": 1, "viebit_filename": "NYCC-PV-CH-CHA_261022-101209",
                             "fetch_status": "fetched"})
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-CHA_261022-Finance"})

    assert not _clerk_link_allowed(conn, 2, "NYCC-PV-CH-CHA_261022-101209")
    assert not _clerk_link_allowed(conn, 1, "NYCC-PV-CH-CHA_261022-Finance")
    assert _clerk_link_allowed(conn, 1, "NYCC-PV-CH-CHA_261022-101209")
    assert _clerk_link_allowed(conn, 2, "NYCC-PV-CH-CHA_261022-Finance")


# --- registry integration ---------------------------------------------------


def _seed_event(conn, event_id, body, time, location, day=OCT22, **extra):
    return db.upsert_meeting(
        conn,
        {
            "legistar_event_id": event_id,
            "body_name": body,
            "event_date": day,
            "event_time": time,
            "event_location": location,
            "meeting_slug": f"{day[:10]}-{event_id}",
            **extra,
        },
    )


def test_match_recordings_links_rows_and_surfaces_holds(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    for event_id, body, time, location in OCT22_EVENTS:
        _seed_event(conn, event_id, body, time, location)
    for name, pub in OCT22_STAMPED.items():
        db.upsert_meeting(conn, {"viebit_filename": name, "viebit_pub_date": pub})

    plan = match_recordings(conn)

    assert _links(plan) == {"NYCC-PV-CH-CHA_261022-101209": 22774}
    linked = db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-101209")
    assert linked["legistar_event_id"] == 22774
    assert linked["body_name"] == "Committee on Finance"
    assert conn.execute("SELECT COUNT(*) FROM meetings WHERE legistar_event_id = 22774").fetchone()[0] == 1
    clip = db.get_meeting(conn, "NYCC-PV-CH-COM_261022-121338")
    assert clip["legistar_event_id"] is None
    assert clip["last_error"].startswith("match held: starts 134 min after Committee on Parks")

    # Idempotent: a second pass changes nothing.
    before = [dict(row) for row in db.iter_meetings(conn)]
    assert match_recordings(conn).links == []
    after = [dict(row) for row in db.iter_meetings(conn)]
    assert [{k: v for k, v in r.items() if k != "updated_at"} for r in before] == [
        {k: v for k, v in r.items() if k != "updated_at"} for r in after
    ]


def test_detach_then_match_repairs_the_oct22_education_page(tmp_path: Path) -> None:
    """Remediation path: the Finance vote was published as Education."""
    conn = db.connect(tmp_path / "registry.db")
    for event_id, body, time, location in OCT22_EVENTS:
        if event_id != 22817:
            _seed_event(conn, event_id, body, time, location)
    done = {column: value for column, value in db.STAGE_DONE_VALUES.items()}
    _seed_event(
        conn, 22817, "Committee on Education", "9:00 AM", CHAMBERS,
        viebit_filename="NYCC-PV-CH-CHA_261022-101209", duration_seconds=557.0, **done,
    )
    for name in OCT22_SUFFIXED:
        db.upsert_meeting(conn, {"viebit_filename": name})

    assert detach_event(conn, "NYCC-PV-CH-CHA_261022-101209") == "event-22817"
    recording = db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-101209")
    assert recording["legistar_event_id"] is None and recording["event_date"] is None
    assert recording["chapterize_status"] == "chapterized"  # statuses untouched by detach
    assert db.get_meeting(conn, "event-22817")["body_name"] == "Committee on Education"

    db.upsert_meeting(conn, {"legistar_event_id": 22774, "viebit_filename": "NYCC-PV-CH-CHA_261022-101209"})
    match_recordings(conn)

    def event_of(name):
        return db.get_meeting(conn, name)["legistar_event_id"]

    assert event_of("NYCC-PV-CH-CHA_261022-101209") == 22774
    assert event_of("NYCC-PV-CH-CHA_261022-Education") == 22817
    assert event_of("NYCC-PV-CH-COM_261022-Parks") == 22800
    assert event_of("NYCC-PV-CH-CHA_261022-Finance") is None
    assert "already linked" in db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-Finance")["last_error"]
    assert db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-101209")["meeting_slug"] == "2026-10-22-22774"


def test_detach_refuses_rows_that_are_not_pairs(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-COM_261022-121338"})
    with pytest.raises(ValueError):
        detach_event(conn, "NYCC-PV-CH-COM_261022-121338")


def test_match_recordings_ignores_dates_before_coverage(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    _seed_event(conn, 1, "Committee on Finance", "10:00 AM", CHAMBERS, day="2026-05-01T00:00:00")
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-CHA_260501-100500"})
    assert match_recordings(conn, coverage_start="2026-06-20").links == []
