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
from .config import MEETINGS_DIR, REGISTRY_DB, chaptering_llm_config, naming_llm_config, voxtral_usd_per_audio_hour
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
from .voxtral_prod import VoxtralBatchPending

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
    # Forward-only coverage: judge by the best-known date, so viebit-only rows
    # with no Legistar event_date can't leak the pre-floor backlog into runs.
    # Require a Legistar match (event_date) before spending on processing —
    # an unmatched recording has no title/date to publish under and stays
    # pending until discover links it to its event.
    sql = """
        SELECT * FROM meetings
        WHERE viebit_filename IS NOT NULL
          AND event_date IS NOT NULL
          AND process_attempts < 5
          AND COALESCE(substr(event_date, 1, 10), substr(viebit_pub_date, 1, 10), substr(discovered_at, 1, 10)) >= ?
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
    rows = list(conn.execute(sql, params))
    excluded = _process_attempts_cap_rows(conn, coverage_start)
    if excluded:
        summary = ", ".join(f"{row['meeting_key']}({row['process_attempts']})" for row in excluded)
        print(f"warning: excluding meetings at process_attempts cap: {summary}", file=sys.stderr, flush=True)
    return rows


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
        # Provenance stamps (observability only; merge logic ignores them).
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
    }
    try:
        row = db.get_meeting(conn, meeting_key)
        if not dry_run:
            _reconcile_artifacts(conn, meeting_key, MEETINGS_DIR)
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
        if not dry_run:
            cost_usd = _capture_meeting_cost(conn, meeting_key)
            update: dict[str, Any] = {"process_attempts": 0}
            if cost_usd is not None:
                update["cost_usd"] = cost_usd
            db.update_meeting(conn, meeting_key, update)
    except VoxtralBatchPending as exc:
        result["status"] = "pending"
        result["note"] = str(exc)
        print(f"process-one {meeting_key} pending: {exc}", file=sys.stderr, flush=True)
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            row = db.get_meeting(conn, meeting_key)
            attempts = int(row["process_attempts"] or 0) + 1
            cost_usd = None if dry_run else _capture_meeting_cost(conn, meeting_key)
            db.update_meeting(
                conn,
                meeting_key,
                {
                    "last_error": str(exc)[:2000],
                    "process_attempts": attempts,
                    **({"cost_usd": cost_usd} if cost_usd is not None else {}),
                },
            )
        except Exception:
            pass
        print(f"process-one {meeting_key} failed: {exc}", file=sys.stderr, flush=True)
    finally:
        result["finished_at"] = utc_now_iso()
        try:
            result["row"] = dict(db.get_meeting(conn, meeting_key))
        except Exception:
            result["row"] = None
        row_cost = (result["row"] or {}).get("cost_usd") if isinstance(result.get("row"), dict) else None
        if row_cost is None:
            print(f"process-one {meeting_key} status={result['status']}", flush=True)
        else:
            print(f"process-one {meeting_key} status={result['status']} cost_usd={float(row_cost):.6f}", flush=True)
        if result_json:
            result_json.parent.mkdir(parents=True, exist_ok=True)
            write_json(result_json, result)
            if not dry_run and not _persist_result_to_r2(meeting_key, result_json):
                result["persist_error"] = True
    return 1 if fail_on_error and (result["status"] == "failed" or result.get("persist_error")) else 0


def _persist_result_to_r2(meeting_key: str, result_json: Path) -> bool:
    """Durably record this run's outcome at artifacts/<key>/result.json on R2.

    The registry only advances when export-site merges results, so the result
    record must outlive this runner. R2 is the single artifact store; results
    live beside the meeting's stage outputs at a path WE author — no artifact
    upload/download layout inference anywhere. Local runs (no R2 config) skip
    quietly; in CI a persist failure is a hard failure, because a completed
    meeting whose result is lost is exactly the silent re-spend bug this
    design removes.
    """
    remote = os.environ.get("R2_RCLONE_REMOTE", R2_REMOTE).strip() or R2_REMOTE
    if not (
        os.environ.get(f"RCLONE_CONFIG_{remote.upper()}_TYPE")
        or os.environ.get("R2_ACCESS_KEY_ID")
    ):
        return True  # local run without R2: results stay on disk only
    bucket = os.environ.get("R2_BUCKET", R2_BUCKET).strip() or R2_BUCKET
    dest = f"{remote}:{bucket}/artifacts/{meeting_key}/result.json"
    try:
        subprocess.run(
            [
                "rclone",
                "copyto",
                str(result_json),
                dest,
                "--s3-no-check-bucket",
                "--retries",
                "5",
                "--low-level-retries",
                "10",
            ],
            check=True,
            env=_rclone_env(remote),
        )
        return True
    except Exception as exc:
        print(f"::error::failed to persist result for {meeting_key} to R2: {exc}", file=sys.stderr, flush=True)
        return False


def _capture_meeting_cost(conn: sqlite3.Connection, meeting_key: str) -> float | None:
    try:
        row = db.get_meeting(conn, meeting_key)
        return _calculate_meeting_cost(row)
    except Exception:
        return None


def _calculate_meeting_cost(row: sqlite3.Row) -> float:
    meeting = db.meeting_from_row(row, MEETINGS_DIR)
    total = 0.0
    total += _voxtral_cost_usd(meeting.meeting_dir / "transcribe-meta.json")
    total += _name_speakers_cost_usd(meeting.meeting_dir)
    total += _json_number(meeting.meeting_dir / "chapters.json", "exact_cost_usd") or 0.0
    return round(total, 6)


def _voxtral_cost_usd(meta_path: Path) -> float:
    duration = _json_number(meta_path, "audio_duration_sec")
    if duration is None:
        return 0.0
    meta = _read_json_or_none(meta_path)
    mode = str(meta.get("mode") or "batch").strip().lower() if isinstance(meta, dict) else "batch"
    return float(duration) / 3600.0 * voxtral_usd_per_audio_hour(mode)


def _name_speakers_cost_usd(meeting_dir: Path) -> float:
    meta_path = meeting_dir / "name-speakers-meta.json"
    meta = _read_json_or_none(meta_path)
    if isinstance(meta, dict):
        for key in ("exact_cost_total", "exact_cost_usd"):
            value = meta.get(key)
            if isinstance(value, int | float):
                return float(value)
        chunk_records = meta.get("chunk_records")
        if isinstance(chunk_records, list):
            total = _sum_exact_costs(chunk_records)
            if total:
                return total
    return _sum_exact_costs(_read_chunk_cost_records(meeting_dir))


def _read_chunk_cost_records(meeting_dir: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(meeting_dir.glob("name-speakers-chunk-*.json")):
        payload = _read_json_or_none(path)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _sum_exact_costs(records: list[Any]) -> float:
    total = 0.0
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("exact_cost_usd"), int | float):
            total += float(record["exact_cost_usd"])
    return total


def _json_number(path: Path, key: str) -> float | None:
    payload = _read_json_or_none(path)
    if isinstance(payload, dict) and isinstance(payload.get(key), int | float):
        return float(payload[key])
    return None


def _read_json_or_none(path: Path) -> Any:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return None
        return read_json(path)
    except Exception:
        return None


def _process_attempts_cap_rows(conn: sqlite3.Connection, coverage_start: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT meeting_key, process_attempts FROM meetings
            WHERE viebit_filename IS NOT NULL
              AND event_date IS NOT NULL
              AND process_attempts >= 5
              AND COALESCE(substr(event_date, 1, 10), substr(viebit_pub_date, 1, 10), substr(discovered_at, 1, 10)) >= ?
              AND (
                fetch_status != 'fetched'
                OR prepare_status != 'prepared'
                OR transcribe_status != 'transcribed'
                OR diarize_status != 'diarized'
                OR name_speakers_status != 'named'
                OR chapterize_status != 'chapterized'
              )
            ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
            """,
            (coverage_start,),
        )
    )


def _reconcile_artifacts(conn: sqlite3.Connection, meeting_key: str, meetings_dir: Path) -> None:
    """Downgrade stage statuses whose output artifacts are missing on THIS machine.

    Registry state persists across runs (committed to git) but artifacts live on
    ephemeral runners — a stage marked done elsewhere must re-run here if its
    outputs are absent. Ordered so an early downgrade cascades naturally: each
    later stage re-checks its own inputs when it runs.
    """
    d = meetings_dir / meeting_key
    row = db.get_meeting(conn, meeting_key)
    checks = [
        ("fetch_status", "fetched", ["audio.m4a"]),
        ("prepare_status", "prepared", ["captions-clean.jsonl"]),
        ("transcribe_status", "transcribed", ["utterances-labeled.jsonl", "utterances.jsonl"]),
        ("name_speakers_status", "named", ["utterances-named.jsonl"]),
        ("chapterize_status", "chapterized", ["chapters.json"]),
    ]
    downgrades = {}
    for column, done_value, artifacts in checks:
        if row[column] == done_value and not all((d / a).exists() for a in artifacts):
            downgrades[column] = "pending"
    if downgrades:
        print(f"  reconcile: re-running stages with missing artifacts: {sorted(downgrades)}", flush=True)
        db.update_meeting(conn, meeting_key, downgrades)


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
    # Recurse: upload-artifact@v4 roots each artifact at the least-common
    # ancestor of its paths (data/), so on download the result JSON lands at
    # data/processed-results/processed-results/<key>.json — one level below a
    # top-level glob. The "row" check below skips the meeting-artifact JSONs
    # (meeting.json, chapters.json, *-meta.json) that recursion also sweeps up.
    for path in sorted(results_dir.rglob("*.json")):
        payload = read_json(path)
        row = payload.get("row")
        if not isinstance(row, dict) or not row.get("meeting_key"):
            continue
        meeting_key = str(row["meeting_key"])
        values = {key: row.get(key) for key in db.MEETING_COLUMNS if key in row}
        try:
            current = db.get_meeting(conn, meeting_key)
        except KeyError:
            # Discover is the registry's only row creator. A result for an
            # unknown key is stale (e.g. the row was merged away by dedupe);
            # resurrecting it would recreate duplicates.
            print(f"merge-results: skipping result for unknown meeting {meeting_key}", file=sys.stderr, flush=True)
            continue
        db.update_meeting(conn, meeting_key, _merge_result_values(current, values))
        count += 1
    checkpoint_db(db_path)
    return count


def verify_run_results(results_dir: Path, matrix_json: str) -> list[str]:
    """Account for every meeting this run was dispatched to process.

    One check, same file discovery as merge_results: each matrix key must have
    a parseable result whose status is complete, pending (batch wait), or
    failed (the process job is already red). A MISSING result is the silent
    failure mode — processed work that never reached the registry.
    """
    keys = [str(item["meeting_key"]) for item in json.loads(matrix_json).get("include", [])]
    found: dict[str, str] = {}
    if results_dir.exists():
        for path in sorted(results_dir.rglob("*.json")):
            payload = read_json(path)
            row = payload.get("row")
            if not isinstance(row, dict) or not row.get("meeting_key"):
                continue
            found[str(payload.get("meeting_key") or row["meeting_key"])] = str(payload.get("status"))
    problems = []
    for key in keys:
        status = found.get(key)
        if status is None:
            problems.append(f"{key}: no result record found")
        elif status not in {"complete", "pending", "failed"}:
            problems.append(f"{key}: unrecognized result status '{status}'")
    return problems


# Meeting-dir files export_site reads to build a meeting's page.
EXPORT_INPUT_FILES = (
    "meeting.json",
    "chapters.json",
    "meeting-derived.json",
    "utterances-named.jsonl",
    "utterances.jsonl",
    "captions-clean.jsonl",
)


def pull_export_inputs(db_path: Path = REGISTRY_DB, meetings_dir: Path = MEETINGS_DIR) -> list[str]:
    """Fetch from R2 the meeting dirs export_site needs but the runner lacks.

    Normally that is just the meetings completed this run; after a failed
    export it also covers earlier completions whose site JSON never got
    committed, so a lost export self-heals instead of silently dropping pages.
    """
    from . import export_site

    conn = db.connect(db_path)
    # Same publishability predicate as export itself — one definition.
    rows = export_site.publishable_registry_rows(conn)
    remote = os.environ.get("R2_RCLONE_REMOTE", R2_REMOTE).strip() or R2_REMOTE
    bucket = os.environ.get("R2_BUCKET", R2_BUCKET).strip() or R2_BUCKET
    pulled: list[str] = []
    blocked: list[str] = []
    for row in rows:
        meeting_key = str(row["meeting_key"])
        meeting_dir = meetings_dir / meeting_key
        if (meeting_dir / "chapters.json").exists():
            continue
        payload = dict(row)
        meeting = db.meeting_from_row(row, meetings_dir)
        date = str(meeting.event_date or "")[:10]
        title = export_site._meeting_title(meeting, payload)
        slug = export_site._meeting_slug(date, str(meeting.event_time or ""), title)
        if (export_site.SITE_DATA_DIR / f"{slug}.json").exists():
            continue  # already published and committed; nothing to rebuild
        args = ["rclone", "copy", f"{remote}:{bucket}/artifacts/{meeting_key}", str(meeting_dir), "--s3-no-check-bucket"]
        for name in EXPORT_INPUT_FILES:
            args += ["--include", name]
        subprocess.run(args, check=True, env=_rclone_env(remote))
        # Invariant: complete implies publishable — but one meeting whose
        # artifacts never reached R2 must not stall the whole pipeline.
        # Skip it (export ignores dirs without artifacts, so its previously
        # committed page keeps serving) and surface it for a human via the
        # blocked file the workflow turns into an assigned issue.
        if not (meeting_dir / "chapters.json").exists():
            blocked.append(meeting_key)
            print(
                f"::warning::{meeting_key} is chapterized in the registry but its export "
                f"inputs are missing from R2 (artifacts/{meeting_key}); skipping its "
                "(re)publish — the existing page, if any, keeps serving",
                flush=True,
            )
            continue
        pulled.append(meeting_key)
    blocked_path = meetings_dir.parent / "export-blocked.txt"
    if blocked:
        blocked_path.write_text("\n".join(blocked) + "\n")
    elif blocked_path.exists():
        blocked_path.unlink()
    if pulled:
        print(f"pull-export-inputs: fetched {len(pulled)} meeting dir(s): {', '.join(pulled)}", flush=True)
    else:
        print("pull-export-inputs: nothing to fetch", flush=True)
    return pulled


def reset_meeting(meeting_key: str, db_path: Path = REGISTRY_DB, *, wipe_artifacts: bool = False) -> None:
    """The one sanctioned way to make the pipeline redo a meeting.

    Results on R2 are durable and re-merged every run, so editing the registry
    alone is always undone by the next cycle. Retracting work means retracting
    the durable record too — and dropping the committed site JSON so the next
    export republishes the page instead of skipping it as already published.
    """
    from . import export_site

    conn = db.connect(db_path)
    row = db.get_meeting(conn, meeting_key)

    remote = os.environ.get("R2_RCLONE_REMOTE", R2_REMOTE).strip() or R2_REMOTE
    bucket = os.environ.get("R2_BUCKET", R2_BUCKET).strip() or R2_BUCKET
    prefix = f"{remote}:{bucket}/artifacts/{meeting_key}"
    target = prefix if wipe_artifacts else f"{prefix}/result.json"
    subprocess.run(
        ["rclone", "delete" if wipe_artifacts else "deletefile", target, "--s3-no-check-bucket"],
        check=True,
        env=_rclone_env(remote),
    )

    payload = dict(row)
    meeting = db.meeting_from_row(row, MEETINGS_DIR)
    date = str(meeting.event_date or "")[:10]
    title = export_site._meeting_title(meeting, payload)
    slug = export_site._meeting_slug(date, str(meeting.event_time or ""), title)
    site_json = export_site.SITE_DATA_DIR / f"{slug}.json"
    if site_json.exists():
        site_json.unlink()
        print(f"removed committed site JSON {site_json} (commit this deletion)", flush=True)

    db.update_meeting(
        conn,
        meeting_key,
        {
            "fetch_status": "pending",
            "prepare_status": "pending",
            "transcribe_status": "stubbed",
            "diarize_status": "stubbed",
            "name_speakers_status": "stubbed",
            "chapterize_status": "stubbed",
            "process_attempts": 0,
            "last_error": None,
        },
    )
    print(
        f"reset {meeting_key}: statuses cleared, R2 {'artifacts wiped' if wipe_artifacts else 'result record deleted'}; "
        "commit and push data/registry.db (and any site JSON deletion) — the next run reprocesses it",
        flush=True,
    )


def _merge_result_values(current: sqlite3.Row, incoming: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if key == "meeting_key":
            continue
        if key in db.STAGE_DONE_VALUES:
            value = db.merge_stage_status(key, current[key], value)
        elif key == "process_attempts":
            value = _merge_process_attempts(current[key], value)
        elif key == "cost_usd" and value is None:
            value = current[key]
        merged[key] = value
    return merged


def _merge_process_attempts(current: Any, incoming: Any) -> int:
    current_value = int(current or 0)
    if incoming is None:
        return current_value
    incoming_value = int(incoming or 0)
    if incoming_value == 0:
        return 0
    return max(current_value, incoming_value)


def checkpoint_db(db_path: Path = REGISTRY_DB) -> None:
    conn = db.connect(db_path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
