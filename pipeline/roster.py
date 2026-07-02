from __future__ import annotations

import csv
import os
from datetime import date, datetime
from pathlib import Path

import httpx

from .config import DATA_DIR

ROSTER_URL = "https://data.cityofnewyork.us/resource/uvw5-9znb.csv?$limit=9999999"
ROSTER_CACHE = DATA_DIR / "council-members-1999-present.csv"


def roster_csv_for_prompt(meeting_date: str | None, force_refresh: bool = False) -> str:
    rows = current_roster(meeting_date, force_refresh=force_refresh)
    lines = ["name,district,party"]
    for row in rows:
        name = row.get("name", "").strip()
        district = row.get("district", "").strip()
        party = row.get("party", "").strip()
        lines.append(f"{_csv_cell(name)},{_csv_cell(district)},{_csv_cell(party)}")
    return "\n".join(lines)


def current_roster(meeting_date: str | None, force_refresh: bool = False) -> list[dict[str, str]]:
    cache = fetch_roster(force_refresh=force_refresh)
    rows = list(csv.DictReader(cache.read_text().splitlines()))
    target = _target_date(meeting_date)
    current = [
        row
        for row in rows
        if _date_from_socrata(row.get("term_start")) <= target <= _date_from_socrata(row.get("term_end"))
    ]
    return sorted(current, key=lambda row: int(row.get("district") or 999))


def fetch_roster(force_refresh: bool = False) -> Path:
    if ROSTER_CACHE.exists() and not force_refresh:
        return ROSTER_CACHE
    headers = {}
    token = os.environ.get("NYC_OPENDATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token
    response = httpx.get(ROSTER_URL, headers=headers, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"NYC Open Data roster fetch failed: {response.status_code} {response.text[:500]}")
    ROSTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ROSTER_CACHE.with_name(f".{ROSTER_CACHE.name}.tmp")
    tmp.write_text(response.text)
    tmp.replace(ROSTER_CACHE)
    return ROSTER_CACHE


def _target_date(value: str | None) -> date:
    if not value:
        return date.today()
    return datetime.fromisoformat(str(value)[:10]).date()


def _date_from_socrata(value: str | None) -> date:
    if not value:
        return date.min
    return datetime.fromisoformat(value[:10]).date()


def _csv_cell(value: str) -> str:
    if any(ch in value for ch in ',\"\n'):
        return '"' + value.replace('"', '""') + '"'
    return value

