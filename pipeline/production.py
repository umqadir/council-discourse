from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import db
from .artifacts import read_json, write_json
from .config import MEETINGS_DIR, REGISTRY_DB, chaptering_llm_config, naming_llm_config
from .fetch import (
    _download,
    _extract_audio,
    _extract_wav,
    _has_file,
    _probe_duration,
    _require_tool,
    _write_meeting_json,
)
from .prepare import prepare_meeting
from .stages import chapterize, name_speakers, transcribe
from .utils import utc_now_iso
from .viebit import cdn_url, normalize_filename, resolve_viebit_hash

R2_BUCKET = "council-discourse-videos"
R2_REMOTE = "r2"


def pending_matrix_json(conn: sqlite3.Connection, limit: int | None = None) -> str:
    rows = select_process_candidates(conn, limit=limit)
    include = [
        {
            "meeting_key": str(row["meeting_key"]),
            "event_date": row["event_date"],
            "body_name": row["body_name"],
        }
        for row in rows
    ]
    return json.dumps({"include": include}, separators=(",", ":"))


def select_process_candidates(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    coverage_start = os.environ.get("COUNCIL_COVERAGE_START", "2026-06-20")[:10]
    sql = """
        SELECT * FROM meetings
        WHERE viebit_filename IS NOT NULL
          AND (event_date IS NULL OR event_date >= ?)
          AND (
            fetch_status != 'fetched'
            OR prepare_status != 'prepared'
            OR transcribe_status != 'transcribed'
            OR diarize_status != 'diarized'
            OR name_speakers_status != 'named'
            OR chapterize_status != 'chapterized'
          )
        ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
    """
    params: tuple[Any, ...] = (coverage_start,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (coverage_start, limit)
    return list(conn.execute(sql, params))


def process_one(
    db_path: Path,
    meeting_key: str,
    *,
    result_json: Path | None = None,
    dry_run: bool = False,
    fail_on_error: bool = False,
) -> int:
    conn = db.connect(db_path)
    result: dict[str, Any] = {
        "meeting_key": meeting_key,
        "status": "complete",
        "stages": [],
        "dry_run": dry_run,
        "finished_at": None,
        "row": None,
    }
    try:
        row = db.get_meeting(conn, meeting_key)
        _stage_fetch(conn, row, result, dry_run=dry_run)
        row = db.get_meeting(conn, meeting_key)
        _stage_prepare(conn, row, result, dry_run=dry_run)
        row = db.get_meeting(conn, meeting_key)
        _stage_transcribe_voxtral(conn, row, result, dry_run=dry_run)
        row = db.get_meeting(conn, meeting_key)
        _stage_name_speakers(conn, row, result, dry_run=dry_run)
        row = db.get_meeting(conn, meeting_key)
        _stage_chapterize(conn, row, result, dry_run=dry_run)
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            db.update_meeting(conn, meeting_key, {"last_error": str(exc)[:2000]})
        except Exception:
            pass
        print(f"process-one {meeting_key} failed: {exc}", file=sys.stderr, flush=True)
    finally:
        result["finished_at"] = utc_now_iso()
        try:
            result["row"] = dict(db.get_meeting(conn, meeting_key))
        except Exception:
            result["row"] = None
        if result_json:
            result_json.parent.mkdir(parents=True, exist_ok=True)
            write_json(result_json, result)
    return 1 if result["status"] == "failed" and fail_on_error else 0


def _stage_fetch(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    if row["fetch_status"] == "fetched" and row["video_web_url"]:
        _record_stage(result, "fetch-upload", "skipped")
        return
    if dry_run:
        _record_stage(result, "fetch-upload", "would_run")
        return
    fetch_meeting_prod(conn, row)
    _record_stage(result, "fetch-upload", "completed")


def _stage_prepare(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    if row["prepare_status"] == "prepared":
        _record_stage(result, "prepare", "skipped")
        return
    if dry_run:
        _record_stage(result, "prepare", "would_run")
        return
    prepare_meeting(conn, row)
    _record_stage(result, "prepare", "completed")


def _stage_transcribe_voxtral(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    meeting = db.meeting_from_row(row)
    canonical_labeled = meeting.meeting_dir / "utterances-labeled.jsonl"
    done = (
        row["transcribe_status"] == "transcribed"
        and row["diarize_status"] == "diarized"
        and canonical_labeled.exists()
    )
    if done:
        _record_stage(result, "transcribe-voxtral", "skipped")
        return
    if dry_run:
        _record_stage(result, "transcribe-voxtral", "would_run")
        return
    transcribe(meeting, backend="voxtral")
    db.update_meeting(
        conn,
        meeting.meeting_key,
        {
            "transcribe_status": "transcribed",
            "diarize_status": "diarized",
            "last_error": None,
            "updated_at": utc_now_iso(),
        },
    )
    _record_stage(result, "transcribe-voxtral", "completed")


def _stage_name_speakers(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    if row["name_speakers_status"] == "named":
        _record_stage(result, "name-speakers", "skipped")
        return
    if dry_run:
        _record_stage(result, "name-speakers", "would_run")
        return
    meeting = db.meeting_from_row(row)
    name_speakers(meeting, **_production_llm_kwargs(naming_llm_config()))
    db.update_meeting(
        conn,
        meeting.meeting_key,
        {
            "name_speakers_status": "named",
            "last_error": None,
            "updated_at": utc_now_iso(),
        },
    )
    _record_stage(result, "name-speakers", "completed")


def _stage_chapterize(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    if row["chapterize_status"] == "chapterized":
        _record_stage(result, "chapterize", "skipped")
        return
    if dry_run:
        _record_stage(result, "chapterize", "would_run")
        return
    meeting = db.meeting_from_row(row)
    chapterize(meeting, **_production_llm_kwargs(chaptering_llm_config()))
    db.update_meeting(
        conn,
        meeting.meeting_key,
        {
            "chapterize_status": "chapterized",
            "last_error": None,
            "updated_at": utc_now_iso(),
        },
    )
    _record_stage(result, "chapterize", "completed")


def _record_stage(result: dict[str, Any], stage: str, status: str) -> None:
    result["stages"].append({"stage": stage, "status": status, "at": utc_now_iso()})
    print(f"{stage}: {status}", flush=True)


def _production_llm_kwargs(config: dict[str, str | None]) -> dict[str, str | None]:
    base_url = config["base_url"] or None
    return {
        "model": config["model"],
        "llm_base_url": base_url,
        "llm_api_key_env": config["api_key_env"] if base_url else None,
    }


def fetch_meeting_prod(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    meetings_dir: Path = MEETINGS_DIR,
) -> None:
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
        _transcode_video_web(mp4, video_web)
    if not _has_file(audio, 10_000):
        _extract_audio(mp4, audio)
    if not _has_file(wav, 10_000):
        _extract_wav(audio, wav)

    video_web_url = upload_video_web(meeting.meeting_key, video_web)
    duration = _probe_duration(audio)
    if mp4.exists() and _has_file(audio, 10_000) and _has_file(video_web, 10_000):
        mp4.unlink()

    update: dict[str, Any] = {
        "duration_seconds": duration,
        "fetch_status": "fetched",
        "fetched_at": utc_now_iso(),
        "last_error": None,
    }
    if video_web_url:
        update["video_web_url"] = video_web_url
    db.update_meeting(conn, meeting.meeting_key, update)
    _write_meeting_json(db.get_meeting(conn, meeting.meeting_key), meeting_dir)


def _transcode_video_web(mp4: Path, video_web: Path) -> bool:
    if _has_file(video_web, 10_000):
        return False
    tmp = video_web.with_name(f".{video_web.stem}.tmp{video_web.suffix}")
    if tmp.exists():
        tmp.unlink()
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(mp4),
        "-vf",
        "scale=-2:480",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "32",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    subprocess.run(cmd, check=True)
    os.replace(tmp, video_web)
    return True


def upload_video_web(meeting_key: str, video_web: Path) -> str | None:
    if not _has_file(video_web, 10_000):
        raise RuntimeError(f"missing video-web.mp4 for upload: {video_web}")
    if shutil.which("rclone") is None:
        raise RuntimeError("required command not found on PATH: rclone")

    remote = os.environ.get("R2_RCLONE_REMOTE", R2_REMOTE).strip() or R2_REMOTE
    bucket = os.environ.get("R2_BUCKET", R2_BUCKET).strip() or R2_BUCKET
    dest = f"{remote}:{bucket}/{meeting_key}/video-web.mp4"
    env = _rclone_env(remote)
    subprocess.run(
        [
            "rclone",
            "copyto",
            str(video_web),
            dest,
            "--s3-no-check-bucket",
            "--transfers",
            "1",
            "--checkers",
            "4",
            "--retries",
            "5",
            "--low-level-retries",
            "10",
        ],
        check=True,
        env=env,
    )
    return public_video_url(meeting_key)


def _rclone_env(remote: str) -> dict[str, str]:
    env = os.environ.copy()
    prefix = f"RCLONE_CONFIG_{remote.upper()}_"
    if env.get(prefix + "TYPE"):
        return env
    required = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT"]
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise RuntimeError(f"missing required R2 env vars: {', '.join(missing)}")
    env[prefix + "TYPE"] = "s3"
    env[prefix + "PROVIDER"] = "Cloudflare"
    env[prefix + "ACCESS_KEY_ID"] = env["R2_ACCESS_KEY_ID"]
    env[prefix + "SECRET_ACCESS_KEY"] = env["R2_SECRET_ACCESS_KEY"]
    env[prefix + "ENDPOINT"] = env["R2_ENDPOINT"]
    return env


def public_video_url(meeting_key: str) -> str | None:
    base = (
        os.environ.get("VIDEO_BASE_URL")
        or os.environ.get("R2_PUBLIC_BASE_URL")
        or os.environ.get("PUBLIC_VIDEO_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if not base:
        return None
    return f"{base}/{meeting_key}/video-web.mp4"


def merge_results(db_path: Path, results_dir: Path) -> int:
    conn = db.connect(db_path)
    if not results_dir.exists():
        return 0
    count = 0
    for path in sorted(results_dir.glob("*.json")):
        payload = read_json(path)
        row = payload.get("row")
        if not isinstance(row, dict) or not row.get("meeting_key"):
            continue
        meeting_key = str(row["meeting_key"])
        values = {key: row.get(key) for key in db.MEETING_COLUMNS if key in row}
        try:
            db.get_meeting(conn, meeting_key)
        except KeyError:
            db.upsert_meeting(conn, values)
        else:
            db.update_meeting(conn, meeting_key, {key: value for key, value in values.items() if key != "meeting_key"})
        count += 1
    checkpoint_db(db_path)
    return count


def checkpoint_db(db_path: Path = REGISTRY_DB) -> None:
    conn = db.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
