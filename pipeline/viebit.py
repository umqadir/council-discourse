from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .config import HTTP_TIMEOUT_SECONDS, VIEBIT_CDN_URL, VIEBIT_RSS_URL, VIEBIT_VOD_URL

FILENAME_RE = re.compile(r"(?P<stem>[A-Za-z0-9_-]+_\d{6}-\d{6})(?:fix)?(?:\.mp4)?$")
PLAYER_HASH_RE = re.compile(r"player\.php\?hash=([A-Za-z0-9]+)")


@dataclass(frozen=True)
class RssItem:
    filename: str
    hash: str
    pub_date: str | None
    title: str


def normalize_filename(value: str) -> str:
    filename = value.strip().split("/")[-1]
    return filename.removesuffix(".mp4")


def parse_pub_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value.strip()
    return dt.isoformat()


def parse_rss(xml_text: str) -> list[RssItem]:
    root = ET.fromstring(xml_text)
    items: list[RssItem] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        pub_date = parse_pub_date(item.findtext("pubDate"))
        if not title or not guid:
            continue
        items.append(
            RssItem(
                filename=normalize_filename(title),
                hash=guid,
                pub_date=pub_date,
                title=title,
            )
        )
    return items


def fetch_rss(url: str = VIEBIT_RSS_URL) -> list[RssItem]:
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return parse_rss(response.text)


def parse_filename_timestamp(filename: str) -> datetime | None:
    normalized = normalize_filename(filename)
    m = re.search(r"_(\d{6})-(\d{6})(?:fix)?$", normalized)
    if not m:
        return None
    return datetime.strptime("".join(m.groups()), "%y%m%d%H%M%S")


def room_prefix(filename: str) -> str | None:
    normalized = normalize_filename(filename)
    if "_" not in normalized:
        return None
    return normalized.rsplit("_", 1)[0]


def cdn_url(viebit_hash: str, filename: str, ext: str) -> str:
    return VIEBIT_CDN_URL.format(hash=viebit_hash, filename=normalize_filename(filename), ext=ext)


def vod_url(filename: str) -> str:
    return VIEBIT_VOD_URL.format(filename=normalize_filename(filename))


def extract_player_hash(html: str) -> str | None:
    m = PLAYER_HASH_RE.search(html)
    return m.group(1) if m else None


def resolve_viebit_hash(filename: str) -> str:
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = client.get(vod_url(filename))
        response.raise_for_status()
        found = extract_player_hash(response.text)
    if not found:
        raise RuntimeError(f"could not resolve viebit hash for {filename}")
    return found


def fixture_path(filename: str) -> Path:
    return Path(__file__).resolve().parent.parent / "tests" / "fixtures" / filename
