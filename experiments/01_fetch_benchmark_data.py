"""Fetch benchmark meeting data: viebit video/captions, Legistar transcript PDFs.

Meetings:
  1. Committee on Transportation & Infrastructure (joint w/ Consumer & Worker
     Protection), 2025-04-23 10:00 AM — "Oversight - Dining Out NYC program"
  2. City Council Stated Meeting, 2025-04-24 1:30 PM

Both were covered by citymeetings.nyc (chapter data scraped separately) and have
official hearing transcripts on Legistar (ground truth for WER/cpWER).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "benchmark"

VIEBIT_VOD = "https://councilnyc.viebit.com/vod/?s=true&v={file}.mp4"
VIEBIT_CDN = "https://vbfast-vod.viebit.com/counciln/{hash}/{file}.{ext}"
LEGISTAR = "https://legistar.council.nyc.gov"

MEETINGS = [
    {
        "slug": "2025-04-23-transportation",
        "body": "Committee on Transportation and Infrastructure (joint with Consumer and Worker Protection)",
        "date": "2025-04-23",
        "time": "10:00 AM",
        "legistar_event_id": 1283804,
        "legistar_event_guid": "5F14FA65-A2EF-4DFC-AB50-047ADCCFB9B0",
        "viebit_file": "NYCC-PV-CH-CHA_250423-100921",
        "transcript_url": f"{LEGISTAR}/View.ashx?M=F&ID=14231072&GUID=2A53B92B-C2F2-4B56-9DE5-3292437C67DB",
        "citymeetings_url": "https://citymeetings.nyc/meetings/new-york-city-council/2025-04-23-1000-am-committee-on-transportation-and-infrastructure/",
    },
    {
        "slug": "2025-04-24-stated",
        "body": "City Council Stated Meeting",
        "date": "2025-04-24",
        "time": "1:30 PM",
        "legistar_event_id": 1302749,
        "legistar_event_guid": "2D2F826D-449A-4F9D-86BF-2D9F4982BE65",
        "viebit_file": "NYCC-PV-CH-CHA_250424-144301",
        "transcript_url": None,  # resolved below from stated-meeting matters
        "citymeetings_url": "https://citymeetings.nyc/meetings/new-york-city-council/2025-04-24-0130-pm-stated-meeting/",
    },
]

client = httpx.Client(timeout=60, follow_redirects=True)


def resolve_viebit_hash(file: str) -> str:
    html = client.get(VIEBIT_VOD.format(file=file)).text
    m = re.search(r"player\.php\?hash=([A-Za-z0-9]+)", html)
    if not m:
        raise RuntimeError(f"no hash for {file}")
    return m.group(1)


def find_stated_transcript(event_id: int, guid: str, date_tags: list[str]) -> str | None:
    """Stated-meeting transcripts are attached to matters on the agenda."""
    md = client.get(
        f"{LEGISTAR}/MeetingDetail.aspx?ID={event_id}&GUID={guid}&Options=info|&Search="
    ).text
    links = sorted(set(re.findall(r"LegislationDetail\.aspx\?ID=\d+&amp;GUID=[A-F0-9-]+", md)))
    for link in links[:15]:
        page = client.get(f"{LEGISTAR}/{link.replace('&amp;', '&')}").text
        for m in re.finditer(r'<a[^>]+href="(View\.ashx\?M=F[^"]+)"[^>]*>([^<]*)</a>', page):
            text = m.group(2)
            if "transcript" in text.lower() and any(t in text for t in date_tags):
                return f"{LEGISTAR}/{m.group(1).replace('&amp;', '&')}"
    return None


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  exists: {dest.name}", flush=True)
        return
    print(f"  fetching {dest.name} ...", flush=True)
    subprocess.run(
        ["curl", "-sS", "--retry", "3", "--fail", "-o", str(dest), url],
        check=True,
    )
    print(f"  done: {dest.name} ({dest.stat().st_size:,} bytes)", flush=True)


for mtg in MEETINGS:
    d = DATA / mtg["slug"]
    d.mkdir(parents=True, exist_ok=True)
    print(f"== {mtg['slug']}", flush=True)

    vhash = resolve_viebit_hash(mtg["viebit_file"])
    mtg["viebit_hash"] = vhash

    if mtg["transcript_url"] is None:
        mtg["transcript_url"] = find_stated_transcript(
            mtg["legistar_event_id"], mtg["legistar_event_guid"], ["4-24-25", "4/24/25"]
        )
        print(f"  stated transcript: {mtg['transcript_url']}", flush=True)

    download(
        VIEBIT_CDN.format(hash=vhash, file=mtg["viebit_file"], ext="vtt"),
        d / "captions.vtt",
    )
    if mtg["transcript_url"]:
        download(mtg["transcript_url"], d / "official-transcript.pdf")

    mp4 = d / "video.mp4"
    audio = d / "audio.m4a"
    wav = d / "audio-16k.wav"
    if not audio.exists():
        download(
            VIEBIT_CDN.format(hash=vhash, file=mtg["viebit_file"], ext="mp4"), mp4
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-vn", "-acodec", "copy", str(audio)],
            check=True,
        )
    if not wav.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio), "-ar", "16000", "-ac", "1", str(wav)],
            check=True,
        )
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
        capture_output=True, text=True,
    ).stdout.strip()
    mtg["duration_sec"] = float(dur) if dur else None
    if mp4.exists() and audio.exists() and audio.stat().st_size > 10_000_000:
        mp4.unlink()
        print("  removed video.mp4 (audio extracted)", flush=True)

    (d / "meeting.json").write_text(json.dumps(mtg, indent=2))
    print(f"  meeting.json written; duration={mtg['duration_sec']}", flush=True)

print("ALL DONE", flush=True)
