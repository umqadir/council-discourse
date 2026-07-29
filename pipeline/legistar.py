from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from .config import HTTP_TIMEOUT_SECONDS, LEGISTAR_BASE_URL, LEGISTAR_INSITE_BASE_URL
from .viebit import normalize_filename, parse_filename_timestamp, room_prefix

VIDEO_LINK_RE = re.compile(r"Video\.aspx\?Mode=Auto&amp;URL=([^\"'&<]+)|Video\.aspx\?Mode=Auto&URL=([^\"'<]+)")


@dataclass(frozen=True)
class LegistarEvent:
    event_id: int
    event_guid: str | None
    last_modified_utc: str | None
    body_name: str | None
    event_date: str | None
    event_time: str | None
    location: str | None
    agenda_pdf_url: str | None
    minutes_pdf_url: str | None
    insite_url: str | None
    video_path: str | None
    agenda_status_name: str | None
    minutes_status_name: str | None


def _decode_base64_url(value: str) -> str:
    cleaned = unquote(html.unescape(value.strip()))
    padding = "=" * (-len(cleaned) % 4)
    decoded = base64.b64decode(cleaned + padding)
    return decoded.decode("utf-8", errors="replace")


def extract_video_urls_from_insite_html(html_text: str) -> list[str]:
    urls: list[str] = []
    for match in VIDEO_LINK_RE.finditer(html_text):
        encoded = match.group(1) or match.group(2)
        if not encoded:
            continue
        try:
            urls.append(_decode_base64_url(encoded))
        except Exception:
            continue
    return urls


def viebit_filename_from_url(url: str) -> str | None:
    parsed = urlparse(html.unescape(url))
    query = parse_qs(parsed.query)
    if "v" in query and query["v"]:
        return normalize_filename(query["v"][0])
    path_name = parsed.path.rsplit("/", 1)[-1]
    if path_name.endswith(".mp4"):
        return normalize_filename(path_name)
    return None


def extract_viebit_filename_from_insite_html(html_text: str) -> str | None:
    for url in extract_video_urls_from_insite_html(html_text):
        filename = viebit_filename_from_url(url)
        if filename:
            return filename
    return None


def event_from_api(raw: dict) -> LegistarEvent:
    return LegistarEvent(
        event_id=int(raw["EventId"]),
        event_guid=raw.get("EventGuid"),
        last_modified_utc=raw.get("EventLastModifiedUtc"),
        body_name=raw.get("EventBodyName"),
        event_date=raw.get("EventDate"),
        event_time=raw.get("EventTime"),
        location=raw.get("EventLocation"),
        agenda_pdf_url=raw.get("EventAgendaFile"),
        minutes_pdf_url=raw.get("EventMinutesFile"),
        insite_url=raw.get("EventInSiteURL"),
        video_path=raw.get("EventVideoPath"),
        agenda_status_name=raw.get("EventAgendaStatusName"),
        minutes_status_name=raw.get("EventMinutesStatusName"),
    )


class LegistarClient:
    def __init__(
        self,
        token: str,
        base_url: str = LEGISTAR_BASE_URL,
        insite_base_url: str = LEGISTAR_INSITE_BASE_URL,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.insite_base_url = insite_base_url.rstrip("/")
        self.client = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def get_event_items(self, event_id: int) -> list[dict]:
        response = self.client.get(
            f"{self.base_url}/events/{event_id}/eventitems",
            params={"token": self.token},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"unexpected Legistar eventitems response: {type(payload).__name__}")
        return payload

    def get_events_modified_since(self, cursor: str, page_size: int = 1000) -> list[LegistarEvent]:
        events: list[LegistarEvent] = []
        skip = 0
        while True:
            response = self.client.get(
                f"{self.base_url}/events",
                params={
                    "$filter": f"EventLastModifiedUtc gt datetime'{cursor}'",
                    "$orderby": "EventLastModifiedUtc asc",
                    "$top": str(page_size),
                    "$skip": str(skip),
                    "token": self.token,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"unexpected Legistar events response: {type(payload).__name__}")
            batch = [event_from_api(item) for item in payload]
            events.extend(batch)
            if len(batch) < page_size:
                return events
            skip += page_size

    def get_events_by_date_window(
        self,
        start_date: str,
        end_date: str,
        page_size: int = 1000,
    ) -> list[LegistarEvent]:
        events: list[LegistarEvent] = []
        skip = 0
        date_filter = (
            f"EventDate ge datetime'{start_date[:10]}' "
            f"and EventDate lt datetime'{end_date[:10]}'"
        )
        while True:
            response = self.client.get(
                f"{self.base_url}/events",
                params={
                    "$filter": date_filter,
                    "$orderby": "EventDate asc, EventTime asc",
                    "$top": str(page_size),
                    "$skip": str(skip),
                    "token": self.token,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"unexpected Legistar events response: {type(payload).__name__}")
            batch = [event_from_api(item) for item in payload]
            events.extend(batch)
            if len(batch) < page_size:
                return events
            skip += page_size

    def fetch_meeting_detail_html(self, event: LegistarEvent) -> str | None:
        url = event.insite_url
        if not url and event.event_guid:
            url = (
                f"{self.insite_base_url}/MeetingDetail.aspx?"
                f"ID={event.event_id}&GUID={event.event_guid}&Options=info|&Search="
            )
        if not url:
            return None
        response = self.client.get(urljoin(self.insite_base_url + "/", url))
        response.raise_for_status()
        return response.text


def parse_event_datetime(event_date: str | None, event_time: str | None) -> datetime | None:
    if not event_date:
        return None
    date_part = event_date.split("T", 1)[0]
    candidates = []
    if event_time:
        candidates.append(f"{date_part} {event_time}")
    candidates.append(date_part)
    for value in candidates:
        for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                pass
    return None


def infer_meeting_type(
    body_name: str | None,
    agenda_status_name: str | None = None,
    minutes_status_name: str | None = None,
) -> str:
    text = " ".join(
        value or ""
        for value in (body_name, agenda_status_name, minutes_status_name)
    ).lower()
    body = (body_name or "").strip().lower()
    if body == "city council" or "stated meeting" in text:
        return "STATED_MEETING"
    if any(term in text for term in ("land use", "zoning", "franchises", "landmarks", "public siting")):
        return "LAND_USE"
    if "vote" in text or "voting" in text:
        return "VOTE"
    return "HEARING"


# First-agenda-item strings that carry no information about what the meeting
# is about (procedural stubs, calendar pointers, bare times).
_TOPIC_JUNK_PATTERNS = (
    re.compile(r"^agenda\b", re.I),
    re.compile(r"^see\s", re.I),
    re.compile(r"^roll call", re.I),
    re.compile(r"^\d"),
    re.compile(r"meeting details to be determined", re.I),
    re.compile(r"^(communication|introduction)s?\s+(from|and)\b", re.I),
)


def extract_event_topic(items: list[dict], body_name: str | None = None) -> str | None:
    """Hearing topic from an event's first agenda item (legistar-monitor's method).

    Returns None when the first item is procedural boilerplate rather than a
    real subject, so callers can distinguish "no useful topic" from "not yet
    fetched".
    """
    if not items:
        return None
    ordered = sorted(
        items,
        key=lambda item: item.get("EventItemAgendaSequence")
        if item.get("EventItemAgendaSequence") is not None
        else float("inf"),
    )
    primary = ordered[0]
    topic = str(primary.get("EventItemMatterName") or "").strip()
    if not topic:
        title = str(primary.get("EventItemTitle") or "").strip()
        lines = [line.strip() for line in title.splitlines() if line.strip()]
        topic = " ".join(lines[:3]).strip()
    topic = re.sub(r"\s+", " ", topic).rstrip(".")
    if len(topic) < 8:
        return None
    if body_name and topic.lower() == str(body_name).strip().lower():
        return None
    if any(pattern.search(topic) for pattern in _TOPIC_JUNK_PATTERNS):
        return None
    return topic


def body_slug(body_name: str | None) -> str:
    return slugify(body_name or "new-york-city-council")


def meeting_slug(event_date: str | None, event_time: str | None, body_name: str | None) -> str | None:
    if not event_date:
        return None
    date_part = event_date[:10]
    parts = [date_part]
    time_part = time_slug(event_time or "")
    if time_part:
        parts.append(time_part)
    parts.append(body_slug(body_name))
    return "-".join(part for part in parts if part)


def time_slug(value: str) -> str:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AP]M)\s*$", value, re.I)
    if match:
        hour = int(match.group(1))
        return f"{hour:02d}{match.group(2)}-{match.group(3).lower()}"
    return slugify(value)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def location_room_prefixes(location: str | None) -> set[str]:
    if not location:
        return set()
    lowered = location.lower()
    prefixes: set[str] = set()
    if "council chambers" in lowered or "chambers" in lowered:
        prefixes.add("NYCC-PV-CH-CHA")
    if "committee room" in lowered and "city hall" in lowered:
        prefixes.add("NYCC-PV-CH-COM")
    if "250 broadway" in lowered:
        prefixes.update({"NYCC-250-8-1", "NYCC-250-8-2", "NYCC-250-8-3"})
    return prefixes


def filename_matches_event(
    filename: str,
    event_date: str | None,
    event_time: str | None,
    location: str | None = None,
    tolerance_minutes: int = 150,
) -> bool:
    file_dt = parse_filename_timestamp(filename)
    event_dt = parse_event_datetime(event_date, event_time)
    if not file_dt or not event_dt:
        return False
    prefixes = location_room_prefixes(location)
    prefix = room_prefix(filename)
    if prefixes and prefix not in prefixes:
        return False
    delta_minutes = abs((file_dt - event_dt).total_seconds()) / 60
    return delta_minutes <= tolerance_minutes
