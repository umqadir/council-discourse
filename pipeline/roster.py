from __future__ import annotations

import csv
import os
import json
import re
import time
import warnings
import unicodedata
from datetime import date, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from .config import DATA_DIR, LEGISTAR_BASE_URL

ROSTER_URL = "https://data.cityofnewyork.us/resource/uvw5-9znb.csv?$limit=9999999"
ROSTER_CACHE = DATA_DIR / "council-members-1999-present.csv"

MEMBERSHIP_CACHE = DATA_DIR / "council-memberships.json"
CACHE_TTL_SECONDS = 24 * 3600


def _active(rows, target):
    return [row for row in rows if _date_from_socrata(row.get("term_start")) <= target
            <= _date_from_socrata(row.get("term_end"))]


def _parse_memberships(records):
    rows = []
    for record in records:
        if record.get("OfficeRecordBodyName") != "City Council":
            continue
        match = re.fullmatch(r"District\s+(\d+)", (record.get("OfficeRecordExtraText") or "").strip())
        if not match:  # Public Advocate and historical rows without a district.
            continue
        start, end = record.get("OfficeRecordStartDate"), record.get("OfficeRecordEndDate")
        name = (record.get("OfficeRecordFullName") or "").strip()
        district = int(match[1])
        if not name or not start or not end or not 1 <= district <= 51 or start > end:
            raise RuntimeError("Invalid dated Council membership record")
        rows.append({"name": name, "district": str(district), "party": "",
                     "term_start": start[:10], "term_end": end[:10],
                     "source_id": str(record["OfficeRecordId"])})
    return rows


def _validate_memberships(rows, target):
    active = _active(rows, target)
    districts = [row["district"] for row in active]
    if len(districts) != len(set(districts)):
        raise RuntimeError("Overlapping Council membership terms; roster needs reconciliation")
    # Vacancies are legitimate. Large gaps mean a partial response or stale source.
    if not 1 <= len(districts) <= 51:
        raise RuntimeError(f"Council membership source has {len(districts)} active districts")
    if len(districts) < 51:
        warnings.warn(f"Council roster has {len(districts)} active districts; possible vacancies or source gaps")
    return active


def _name_key(name):
    name = unicodedata.normalize("NFKD", name)
    words = re.findall(r"[a-z]+", "".join(c for c in name if not unicodedata.combining(c)).lower())
    words = [w for w in words if len(w) > 1 and w not in {"speaker", "majority", "minority", "leader", "deputy", "whip", "dr"}]
    return tuple(words)


def _check_directory(active):
    url = "https://council.nyc.gov/districts/"
    try:
        response = httpx.get(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        directory = {}
        for tr in soup.select("table tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.select("td")]
            if len(cells) >= 5 and cells[0].isdigit() and 1 <= int(cells[0]) <= 51:
                directory[str(int(cells[0]))] = {"name": cells[1], "party": cells[4]}
        if not directory:
            raise ValueError("empty directory")
    except (httpx.HTTPError, ValueError):
        if len(active) < 51:
            raise RuntimeError("Cannot confirm Council roster gaps against the official directory") from None
        warnings.warn("Council directory unavailable; using complete dated Legistar membership")
        return {}
    by_district = {row["district"]: row for row in active}
    for district, row in directory.items():
        other = by_district.get(district)
        if other is None or _name_key(other["name"]) != _name_key(row["name"]):
            raise RuntimeError(f"Council membership disagrees with official directory in district {district}")
    return directory


def fetch_memberships(force_refresh=False):
    if MEMBERSHIP_CACHE.exists() and not force_refresh and time.time() - MEMBERSHIP_CACHE.stat().st_mtime < CACHE_TTL_SECONDS:
        return json.loads(MEMBERSHIP_CACHE.read_text())["rows"]
    token = os.environ.get("LEGISTAR_TOKEN")
    if not token:
        raise RuntimeError("LEGISTAR_TOKEN is required to refresh the dated Council roster")
    records = []
    # The API defaults to 1,000 rows; deterministic pagination also protects future growth.
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        while True:
            response = client.get(LEGISTAR_BASE_URL + "/OfficeRecords", params={
                "token": token, "$filter": "OfficeRecordBodyName eq 'City Council'",
                "$orderby": "OfficeRecordId", "$top": 1000, "$skip": len(records)})
            if response.status_code != 200:
                raise RuntimeError(f"Legistar membership fetch failed: HTTP {response.status_code}")
            page = response.json()
            if not isinstance(page, list):
                raise RuntimeError("Legistar membership response is not a list")
            records.extend(page)
            if len(page) < 1000:
                break
    rows = _parse_memberships(records)
    active = _validate_memberships(rows, date.today())
    directory = _check_directory(active)
    for row in active:
        if row["district"] in directory:
            row["party"] = directory[row["district"]]["party"]
    payload = {"fetched_at": datetime.now().isoformat(), "source": LEGISTAR_BASE_URL + "/OfficeRecords",
               "filter": "OfficeRecordBodyName eq 'City Council'", "rows": rows}
    MEMBERSHIP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMBERSHIP_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(MEMBERSHIP_CACHE)
    return rows


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
    # Legistar supplies exact dated terms, including special-election successors.
    # It overrides the historical feed only during those terms, never by today's directory.
    memberships = fetch_memberships(force_refresh=force_refresh)
    live = _active(memberships, target)
    covered = {row["district"] for row in memberships
               if _date_from_socrata(row["term_start"]) <= target}
    by_district = {str(int(row["district"])): row for row in current if row.get("district")}
    # Respect ended terms/vacancies even if the historical feed has a stale end date.
    by_district = {district: row for district, row in by_district.items() if district not in covered}
    for row in sorted(live, key=lambda row: row["term_start"]):
        old = next((r for r in current if str(int(r.get("district") or 0)) == row["district"]), {})
        merged = dict(row)
        if not merged["party"] and old.get("name", "").strip() == row["name"]:
            merged["party"] = old.get("party", "")
        by_district[row["district"]] = merged
    current = list(by_district.values())
    return sorted(current, key=lambda row: int(row.get("district") or 999))


def fetch_roster(force_refresh: bool = False) -> Path:
    if ROSTER_CACHE.exists() and not force_refresh and time.time() - ROSTER_CACHE.stat().st_mtime < CACHE_TTL_SECONDS:
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

