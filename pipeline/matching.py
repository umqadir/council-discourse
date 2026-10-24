"""One-to-one pairing of Viebit recordings with Legistar events.

Discovery links a recording to an event only when the evidence is specific:
the clerk's own link (handled in discover), a body-name suffix in the
filename (NYCC-PV-CH-CHA_261022-CJ), or a start time close to the scheduled
time in a compatible room. Each recording and each event is used at most
once, closest first. Anything doubtful is held and surfaced instead of being
published under the wrong meeting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .legistar import (
    MATCH_LATE_MINUTES,
    body_suffix_fit,
    location_exact_room,
    location_room_prefixes,
    parse_event_datetime,
    time_match_cost,
)
from .viebit import filename_body_suffix, parse_filename_date, parse_filename_timestamp, room_prefix

# A committee vote can be a few minutes long, but an inferred match this short
# is more often mic checks or a stray clip than the meeting itself.
MIN_INFERRED_RECORDING_MINUTES = 5.0
# Recordings starting this far outside the window are reported, not linked.
REPORT_LATE_MINUTES = 150
HOLD_NOTE_PREFIX = "match held: "
VIEBIT_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class EventSlot:
    event_id: int
    body_name: str | None
    event_date: str | None
    event_time: str | None
    location: str | None
    linked_filename: str | None = None


@dataclass(frozen=True)
class RecordingSlot:
    filename: str
    pub_date: str | None = None
    duration_seconds: float | None = None
    linked_event_id: int | None = None


@dataclass
class MatchPlan:
    # (event_id, filename, reason)
    links: list[tuple[int, str, str]] = field(default_factory=list)
    # filename -> why it was not linked
    holds: dict[str, str] = field(default_factory=dict)


def recording_max_minutes(recording: RecordingSlot) -> float | None:
    """Known length, or an upper bound: Viebit posts a recording after it ends."""
    bounds: list[float] = []
    if recording.duration_seconds:
        bounds.append(float(recording.duration_seconds) / 60)
    start = parse_filename_timestamp(recording.filename)
    if start and recording.pub_date:
        try:
            posted = datetime.fromisoformat(recording.pub_date)
        except ValueError:
            posted = None
        if posted is not None and posted.tzinfo is not None:
            bounds.append((posted - start.replace(tzinfo=VIEBIT_TIMEZONE)).total_seconds() / 60)
    return min(bounds) if bounds else None


def _event_day(event: EventSlot) -> date | None:
    parsed = parse_event_datetime(event.event_date, event.event_time)
    return parsed.date() if parsed else None


def _label(event: EventSlot) -> str:
    return f"{event.body_name or 'event'} {event.event_time or ''} (event {event.event_id})".replace("  ", " ")


def plan_matches(events: list[EventSlot], recordings: list[RecordingSlot]) -> MatchPlan:
    plan = MatchPlan()
    by_day_events: dict[date, list[EventSlot]] = {}
    for event in events:
        day = _event_day(event)
        if day:
            by_day_events.setdefault(day, []).append(event)
    by_day_recordings: dict[date, list[RecordingSlot]] = {}
    for recording in recordings:
        day = parse_filename_date(recording.filename)
        if day:
            by_day_recordings.setdefault(day, []).append(recording)

    for day in sorted(by_day_recordings):
        day_events = sorted(by_day_events.get(day, []), key=lambda e: e.event_id)
        day_recordings = sorted(by_day_recordings[day], key=lambda r: r.filename)
        if day_events:
            _plan_day(day_events, day_recordings, plan)
    return plan


def _plan_day(events: list[EventSlot], recordings: list[RecordingSlot], plan: MatchPlan) -> None:
    taken_events = {e.event_id for e in events if e.linked_filename}
    taken_recordings = {r.filename for r in recordings if r.linked_event_id is not None}
    linked_to = {e.event_id: e.linked_filename for e in events if e.linked_filename}

    # Phase 1: the clerk named the body in the filename. Strongest inferred
    # evidence, so it claims its event before start times are considered;
    # the most specific names (whole body name) go first.
    suffix_fits = []
    for recording in recordings:
        suffix = filename_body_suffix(recording.filename)
        if not suffix or recording.filename in taken_recordings:
            continue
        fits = []
        for event in events:
            fit = body_suffix_fit(suffix, event.body_name)
            if fit is None:
                continue
            exact = location_exact_room(event.location)
            room_miss = 1 if exact and exact != room_prefix(recording.filename) else 0
            fits.append(((fit, room_miss), event))
        if fits:
            fits.sort(key=lambda item: (item[0], item[1].event_id))
            suffix_fits.append((fits[0][0], recording.filename, suffix, fits))
    for best_key, filename, suffix, fits in sorted(suffix_fits, key=lambda item: (item[0], item[1])):
        best = fits[0][1]
        free = [(key, event) for key, event in fits if event.event_id not in taken_events]
        if best.event_id in taken_events and (not free or free[0][0] > best_key):
            plan.holds[filename] = (
                f"filename names {_label(best)}, which is already linked to {linked_to.get(best.event_id)}"
            )
            continue
        if not free:
            continue
        key, event = free[0]
        if len(free) > 1 and free[1][0] == key:
            names = ", ".join(_label(e) for k, e in free if k == key)
            plan.holds[filename] = f"filename suffix '{suffix}' fits several events: {names}"
            continue
        plan.links.append((event.event_id, filename, f"filename suffix '{suffix}'"))
        taken_events.add(event.event_id)
        taken_recordings.add(filename)
        linked_to[event.event_id] = filename

    # Phase 2: start time. Closest pair first, each side used once.
    candidates: list[tuple[float, str, int]] = []
    short: dict[str, float] = {}
    for recording in recordings:
        if recording.filename in taken_recordings or not parse_filename_timestamp(recording.filename):
            continue
        max_minutes = recording_max_minutes(recording)
        for event in events:
            if event.event_id in taken_events:
                continue
            cost = time_match_cost(recording.filename, event.event_date, event.event_time, event.location)
            if cost is None:
                continue
            if max_minutes is not None and max_minutes < MIN_INFERRED_RECORDING_MINUTES:
                short[recording.filename] = max_minutes
                continue
            candidates.append((cost, recording.filename, event.event_id))
    for cost, filename, event_id in sorted(candidates):
        if filename in taken_recordings or event_id in taken_events:
            continue
        plan.links.append((event_id, filename, f"start time (cost {cost:.0f})"))
        taken_events.add(event_id)
        taken_recordings.add(filename)

    for filename, max_minutes in short.items():
        if filename not in taken_recordings:
            plan.holds[filename] = (
                f"recording is at most {max_minutes:.1f} min long, under the "
                f"{MIN_INFERRED_RECORDING_MINUTES:.0f}-minute floor for an inferred match"
            )

    # Recordings in the right room and day that start too far from any still
    # unmatched event: report the near miss rather than guess.
    for recording in recordings:
        start = parse_filename_timestamp(recording.filename)
        if not start or recording.filename in taken_recordings or recording.filename in plan.holds:
            continue
        for event in events:
            if event.event_id in taken_events:
                continue
            event_dt = parse_event_datetime(event.event_date, event.event_time)
            prefixes = location_room_prefixes(event.location)
            if not event_dt or not event.event_time or (prefixes and room_prefix(recording.filename) not in prefixes):
                continue
            delta = (start - event_dt).total_seconds() / 60
            if MATCH_LATE_MINUTES < delta <= REPORT_LATE_MINUTES:
                plan.holds[recording.filename] = (
                    f"starts {delta:.0f} min after {_label(event)}, past the "
                    f"{MATCH_LATE_MINUTES}-minute matching window"
                )
                break
