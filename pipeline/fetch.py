from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import db
from .config import MEETINGS_DIR
from .utils import atomic_write_text, utc_now_iso
from .viebit import cdn_url, normalize_filename, resolve_viebit_hash


def _require_tool(name: str) -> None:
    if not shutil.which(name):
        raise RuntimeError(f"required command not found on PATH: {name}")


def _has_file(path: Path, min_size: int = 1) -> bool:
    return path.exists() and path.stat().st_size >= min_size


def _download(url: str, dest: Path, min_size: int = 1) -> bool:
    if _has_file(dest, min_size):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    cmd = [
        "curl",
        "--silent",
        "--show-error",
        "-L",
        "--fail",
        "--retry",
        "5",
        "--retry-delay",
        "2",
        "--output",
        str(tmp),
    ]
    if tmp.exists() and tmp.stat().st_size > 0:
        cmd.extend(["--continue-at", "-"])
    cmd.append(url)
    subprocess.run(cmd, check=True)
    if not _has_file(tmp, min_size):
        raise RuntimeError(f"downloaded file is empty: {dest}")
    os.replace(tmp, dest)
    return True


def _extract_audio(mp4: Path, audio: Path) -> bool:
    if _has_file(audio, 10_000):
        return False
    tmp = audio.with_name(f".{audio.stem}.tmp{audio.suffix}")
    if tmp.exists():
        tmp.unlink()
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-vn", "-acodec", "copy", str(tmp)],
        check=True,
    )
    os.replace(tmp, audio)
    return True


def _remux_faststart(mp4: Path, video_web: Path) -> bool:
    if _has_file(video_web, 10_000):
        return False
    tmp = video_web.with_name(f".{video_web.stem}.tmp{video_web.suffix}")
    if tmp.exists():
        tmp.unlink()
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-c", "copy", "-movflags", "+faststart", str(tmp)],
        check=True,
    )
    os.replace(tmp, video_web)
    return True


def _extract_wav(audio: Path, wav: Path) -> bool:
    if _has_file(wav, 10_000):
        return False
    tmp = wav.with_name(f".{wav.stem}.tmp{wav.suffix}")
    if tmp.exists():
        tmp.unlink()
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio), "-ar", "16000", "-ac", "1", str(tmp)],
        check=True,
    )
    os.replace(tmp, wav)
    return True


def _probe_duration(audio: Path) -> float | None:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
        check=True,
        capture_output=True,
        text=True,
    )
    value = proc.stdout.strip()
    return float(value) if value else None


def _write_meeting_json(row: sqlite3.Row, meeting_dir: Path) -> None:
    payload = {key: row[key] for key in row.keys()}
    atomic_write_text(meeting_dir / "meeting.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")


def fetch_meeting(conn: sqlite3.Connection, row: sqlite3.Row, meetings_dir: Path = MEETINGS_DIR) -> None:
    _require_tool("curl")
    _require_tool("ffmpeg")
    _require_tool("ffprobe")

    meeting = db.meeting_from_row(row, meetings_dir)
    if not meeting.viebit_filename:
        raise RuntimeError(f"{meeting.meeting_key} has no viebit filename")
    filename = normalize_filename(meeting.viebit_filename)
    viebit_hash = meeting.viebit_hash or resolve_viebit_hash(filename)
    if viebit_hash != meeting.viebit_hash:
        db.update_meeting(conn, meeting.meeting_key, {"viebit_hash": viebit_hash})
        row = db.get_meeting(conn, meeting.meeting_key)
        meeting = db.meeting_from_row(row, meetings_dir)

    meeting_dir = meeting.meeting_dir
    meeting_dir.mkdir(parents=True, exist_ok=True)

    captions = meeting_dir / "captions.vtt"
    thumbnail = meeting_dir / "thumbnail.jpg"
    agenda = meeting_dir / "agenda.pdf"
    mp4 = meeting_dir / "video.mp4"
    video_web = meeting_dir / "video-web.mp4"
    audio = meeting_dir / "audio.m4a"
    wav = meeting_dir / "audio-16k.wav"

    _download(cdn_url(viebit_hash, filename, "vtt"), captions)
    _download(cdn_url(viebit_hash, filename, "jpg"), thumbnail)
    if meeting.agenda_pdf_url:
        _download(meeting.agenda_pdf_url, agenda)

    if not _has_file(audio, 10_000) or not _has_file(wav, 10_000) or not _has_file(video_web, 10_000):
        _download(cdn_url(viebit_hash, filename, "mp4"), mp4, min_size=10_000)
    if not _has_file(video_web, 10_000):
        _remux_faststart(mp4, video_web)
    if not _has_file(audio, 10_000):
        _extract_audio(mp4, audio)
    if not _has_file(wav, 10_000):
        _extract_wav(audio, wav)
    duration = _probe_duration(audio)
    if mp4.exists() and _has_file(audio, 10_000) and _has_file(video_web, 10_000):
        mp4.unlink()

    db.update_meeting(
        conn,
        meeting.meeting_key,
        {
            "duration_seconds": duration,
            "fetch_status": "fetched",
            "fetched_at": utc_now_iso(),
            "last_error": None,
        },
    )
    _write_meeting_json(db.get_meeting(conn, meeting.meeting_key), meeting_dir)
