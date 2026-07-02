"""Scrape citymeetings.nyc output for the benchmark meetings.

Captures their chapter lists (titles, types, timestamps, summaries) and a few
full chapter transcripts (speaker names + per-utterance seek timestamps) as the
quality reference we benchmark against.
"""

import json
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "benchmark"

MEETINGS = {
    "2025-04-23-transportation": "https://citymeetings.nyc/meetings/new-york-city-council/2025-04-23-1000-am-committee-on-transportation-and-infrastructure/",
    "2025-04-24-stated": "https://citymeetings.nyc/meetings/new-york-city-council/2025-04-24-0130-pm-stated-meeting/",
}
N_SAMPLE_CHAPTERS = 4  # full transcripts to pull per meeting

client = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})


def parse_chapters(soup: BeautifulSoup, base: str) -> list[dict]:
    chapters = []
    for card in soup.select("[data-chapter-id]"):
        a = card if card.name == "a" and card.get("href") else card.find("a", href=True)
        badge = card.find(class_=re.compile("badge|tag|type"))
        text = card.get_text(" ", strip=True)
        ts = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", text)
        chapters.append(
            {
                "chapter_id": card["data-chapter-id"],
                "url": str(httpx.URL(base).join(a["href"])) if a else None,
                "badge": badge.get_text(strip=True) if badge else None,
                "card_text": text[:500],
                "start_ts": ts.group(1) if ts else None,
            }
        )
    return chapters


def parse_chapter_page(url: str) -> dict:
    html = client.get(url).text
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("h1")
    # utterances: elements with @click="player.seekTo(...)"
    utterances = []
    for el in soup.find_all(attrs={"@click": re.compile(r"seekTo")}):
        m = re.search(r"seekTo\(([\d.]+)\)", el.get("@click", ""))
        utterances.append({"seek": float(m.group(1)) if m else None, "text": el.get_text(" ", strip=True)[:100]})
    # speaker blocks: crude — grab the transcript tab container text
    body_text = soup.get_text("\n", strip=True)
    return {
        "url": url,
        "title": title.get_text(strip=True) if title else None,
        "n_seek_elements": len(utterances),
        "utterance_sample": utterances[:60],
        "page_text_head": body_text[:3000],
        "raw_html_saved": True,
        "html": html,
    }


for slug, url in MEETINGS.items():
    d = DATA / slug
    d.mkdir(parents=True, exist_ok=True)
    print(f"== {slug}", flush=True)
    html = client.get(url).text
    (d / "citymeetings-meeting-page.html").write_text(html)
    soup = BeautifulSoup(html, "html.parser")
    chapters = parse_chapters(soup, url)
    print(f"  {len(chapters)} chapters parsed", flush=True)
    (d / "citymeetings-chapters.json").write_text(json.dumps(chapters, indent=2))

    samples = []
    step = max(1, len(chapters) // N_SAMPLE_CHAPTERS) if chapters else 1
    for ch in chapters[::step][:N_SAMPLE_CHAPTERS]:
        if not ch["url"]:
            continue
        info = parse_chapter_page(ch["url"])
        (d / f"citymeetings-chapter-{ch['chapter_id']}.html").write_text(info.pop("html"))
        samples.append(info)
        print(f"  sampled chapter {ch['chapter_id']}: {info['title']!r} ({info['n_seek_elements']} seeks)", flush=True)
    (d / "citymeetings-chapter-samples.json").write_text(json.dumps(samples, indent=2))

print("ALL DONE", flush=True)
