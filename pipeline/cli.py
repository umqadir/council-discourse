from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import GEMINI_LLM, REGISTRY_DB, chaptering_llm_config, naming_llm_config
from .discover import discover_legistar, discover_viebit_rss
from .export_site import export_site
from .fetch import fetch_meeting
from .models import Meeting
from .production import (
    checkpoint_db,
    merge_results,
    pending_matrix_json,
    process_one,
    pull_export_inputs,
    verify_run_results,
)
from .prepare import prepare_meeting
from .stages import chapterize, diarize, name_speakers, transcribe
from .utils import utc_now_iso


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=REGISTRY_DB, help="SQLite registry path")


def _add_llm_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm-base-url",
        default=None,
        help="OpenAI-compatible base URL override for the naming/chaptering LLM",
    )
    parser.add_argument(
        "--llm-api-key-env",
        default=None,
        help="env var holding the OpenAI-compatible API key (e.g. OPENROUTER_API_KEY)",
    )


def _resolve_llm(args: argparse.Namespace, stage: str = "naming") -> dict[str, str | None]:
    """Merge CLI flags over the production LLM config for naming/chaptering stages."""
    config = dict(naming_llm_config() if stage == "naming" else chaptering_llm_config())
    # A bare `--model gemini-*` (a native Gemini id, no provider slug) without a base_url
    # override means the user wants Gemini's own API path, not the OpenRouter default.
    if (
        args.model
        and args.llm_base_url is None
        and "/" not in args.model
        and args.model.lower().startswith("gemini")
    ):
        config = dict(GEMINI_LLM)
    model = args.model or config["model"]
    base_url = args.llm_base_url if args.llm_base_url is not None else config["base_url"]
    api_key_env = args.llm_api_key_env or config["api_key_env"]
    # Gemini uses its own key path (GOOGLE_API_KEY) with base_url=None; only pass an
    # OpenAI-compatible key env when a base_url is actually in play.
    return {
        "model": model,
        "llm_base_url": base_url or None,
        "llm_api_key_env": api_key_env if base_url else None,
    }


def cmd_discover(args: argparse.Namespace) -> int:
    if bool(args.legistar_start) != bool(args.legistar_end):
        print("--legistar-start and --legistar-end must be supplied together", file=sys.stderr)
        return 2
    conn = db.connect(args.db)
    log = sys.stderr if args.emit_pending_json else sys.stdout

    # Discovery only ADDS newly-seen meetings; the pending matrix comes from the
    # persisted registry. A transient upstream blip (Legistar/viebit timing out
    # past its retries) must not abort the whole pipeline and skip processing of
    # already-pending meetings. Log the source failure loudly (GitHub annotation)
    # and continue; a PERSISTENT outage surfaces via the export-site staleness
    # guard (ci-health newest_event_date going stale), which is the real backstop.
    def _run_source(label: str, fn):
        try:
            return fn(), True
        except Exception as exc:  # noqa: BLE001 - deliberately source-isolating
            print(f"::warning::discovery source '{label}' failed this cycle: {exc}", file=log, flush=True)
            return None, False

    rss_count = 0
    rss_ok = True
    if not args.no_rss:
        rss_count, rss_ok = _run_source("viebit_rss", lambda: discover_viebit_rss(conn, args.rss_url))

    legistar_result, legistar_ok = _run_source(
        "legistar",
        lambda: discover_legistar(conn, start_date=args.legistar_start, end_date=args.legistar_end),
    )
    event_count, skipped_legistar = legistar_result if legistar_result is not None else (0, True)

    if skipped_legistar and legistar_ok:
        print(f"viebit_rss={rss_count}; legistar=skipped (LEGISTAR_TOKEN unset)", file=log)
    else:
        print(f"viebit_rss={rss_count} (ok={rss_ok}); legistar_events={event_count} (ok={legistar_ok})", file=log)

    if args.emit_pending_json:
        print(pending_matrix_json(conn, limit=args.pending_limit))
    return 0


def _run_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    handler,
    stage_col: str,
    complete_status: str,
    meeting_label: str,
    *,
    skip_completed: bool = True,
) -> int:
    failures = 0
    for row in rows:
        key = str(row["meeting_key"])
        if skip_completed and row[stage_col] == complete_status:
            print(f"{meeting_label} {key} (already {complete_status})", flush=True)
            continue
        print(f"{meeting_label} {key}", flush=True)
        try:
            handler(conn, row)
        except Exception as exc:
            failures += 1
            db.update_meeting(conn, key, {stage_col: "error", "last_error": str(exc)})
            print(f"  ERROR: {exc}", file=sys.stderr, flush=True)
    return 1 if failures else 0


def cmd_fetch(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    if args.all_pending:
        rows = db.select_fetch_candidates(conn, args.meeting_key, args.limit)
    else:
        rows = db.select_oldest_viebit_meetings(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no fetch candidates")
        return 0
    return _run_rows(conn, rows, fetch_meeting, "fetch_status", "fetched", "fetch", skip_completed=not args.force)


def cmd_prepare(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    if args.all_pending:
        rows = db.select_prepare_candidates(conn, args.meeting_key, args.limit)
    else:
        rows = db.select_prepare_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no prepare candidates")
        return 0
    return _run_rows(
        conn,
        rows,
        prepare_meeting,
        "prepare_status",
        "prepared",
        "prepare",
        skip_completed=not args.force,
    )


def _meeting_from_dir(meeting_dir: Path) -> Meeting:
    payload = {}
    meeting_json = meeting_dir / "meeting.json"
    if meeting_json.exists():
        payload = json.loads(meeting_json.read_text())
    return Meeting(
        meeting_key=str(payload.get("meeting_key") or payload.get("slug") or meeting_dir.name),
        meeting_dir=meeting_dir,
        legistar_event_id=payload.get("legistar_event_id"),
        legistar_event_guid=payload.get("legistar_event_guid"),
        viebit_filename=payload.get("viebit_filename") or payload.get("viebit_file"),
        viebit_hash=payload.get("viebit_hash"),
        body_name=payload.get("body_name") or payload.get("body"),
        event_date=payload.get("event_date") or payload.get("date"),
        event_time=payload.get("event_time") or payload.get("time"),
        event_location=payload.get("event_location"),
        duration_seconds=payload.get("duration_seconds") or payload.get("duration_sec"),
        agenda_pdf_url=payload.get("agenda_pdf_url"),
        minutes_pdf_url=payload.get("minutes_pdf_url"),
        insite_url=payload.get("insite_url"),
        event_video_path=payload.get("event_video_path"),
        agenda_status_name=payload.get("agenda_status_name"),
        minutes_status_name=payload.get("minutes_status_name"),
        meeting_type=payload.get("meeting_type"),
        meeting_slug=payload.get("meeting_slug") or payload.get("slug"),
        video_web_url=payload.get("video_web_url"),
    )


def _run_meeting_dir_stage(args: argparse.Namespace, stage_name: str, handler) -> int:
    meeting = _meeting_from_dir(args.meeting_dir)
    print(f"{stage_name} {meeting.meeting_key}", flush=True)
    output = handler(meeting)
    print(f"  wrote {output}", flush=True)
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    if args.meeting_dir:
        return _run_meeting_dir_stage(
            args,
            "transcribe",
            lambda meeting: transcribe(meeting, backend=args.backend, model=args.model),
        )

    conn = db.connect(args.db)
    rows = db.select_transcribe_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no transcribe candidates")
        return 0

    def run_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        meeting = db.meeting_from_row(row)
        transcribe(meeting, backend=args.backend, model=args.model)
        db.update_meeting(
            conn,
            meeting.meeting_key,
            {
                "transcribe_status": "transcribed",
                "last_error": None,
                "updated_at": utc_now_iso(),
            },
        )

    return _run_rows(conn, rows, run_one, "transcribe_status", "transcribed", "transcribe")


def cmd_diarize(args: argparse.Namespace) -> int:
    if args.meeting_dir:
        return _run_meeting_dir_stage(
            args,
            "diarize",
            lambda meeting: diarize(meeting, model=args.model, device=args.device),
        )

    conn = db.connect(args.db)
    rows = db.select_diarize_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no diarize candidates")
        return 0

    def run_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        meeting = db.meeting_from_row(row)
        diarize(meeting, model=args.model, device=args.device)
        db.update_meeting(
            conn,
            meeting.meeting_key,
            {
                "diarize_status": "diarized",
                "last_error": None,
                "updated_at": utc_now_iso(),
            },
        )

    return _run_rows(conn, rows, run_one, "diarize_status", "diarized", "diarize")


def cmd_name_speakers(args: argparse.Namespace) -> int:
    llm = _resolve_llm(args)
    if args.meeting_dir:
        return _run_meeting_dir_stage(
            args,
            "name-speakers",
            lambda meeting: name_speakers(meeting, **llm),
        )

    conn = db.connect(args.db)
    rows = db.select_name_speakers_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no name-speakers candidates")
        return 0

    def run_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        meeting = db.meeting_from_row(row)
        name_speakers(meeting, **llm)
        db.update_meeting(
            conn,
            meeting.meeting_key,
            {
                "name_speakers_status": "named",
                "last_error": None,
                "updated_at": utc_now_iso(),
            },
        )

    return _run_rows(conn, rows, run_one, "name_speakers_status", "named", "name-speakers")


def cmd_chapterize(args: argparse.Namespace) -> int:
    llm = _resolve_llm(args, stage="chaptering")
    if args.meeting_dir:
        return _run_meeting_dir_stage(
            args,
            "chapterize",
            lambda meeting: chapterize(meeting, **llm),
        )

    conn = db.connect(args.db)
    rows = db.select_chapterize_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no chapterize candidates")
        return 0

    def run_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        meeting = db.meeting_from_row(row)
        chapterize(meeting, **llm)
        db.update_meeting(
            conn,
            meeting.meeting_key,
            {
                "chapterize_status": "chapterized",
                "last_error": None,
                "updated_at": utc_now_iso(),
            },
        )

    return _run_rows(conn, rows, run_one, "chapterize_status", "chapterized", "chapterize")


def cmd_export_site(args: argparse.Namespace) -> int:
    written = export_site(args.db, include_benchmark=not args.no_benchmark, allow_empty=args.allow_empty)
    for path in written:
        print(f"wrote {path}", flush=True)
    return 0


def cmd_process_one(args: argparse.Namespace) -> int:
    return process_one(
        args.db,
        args.meeting_key,
        result_json=args.result_json,
        dry_run=args.dry_run,
        fail_on_error=args.fail_on_error,
    )


def cmd_pending_matrix(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    print(pending_matrix_json(conn, limit=args.limit))
    return 0


def cmd_merge_results(args: argparse.Namespace) -> int:
    count = merge_results(args.db, args.results_dir)
    print(f"merged_results={count}")
    return 0


def cmd_verify_run_results(args: argparse.Namespace) -> int:
    problems = verify_run_results(args.results_dir, args.matrix_json)
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    print("all dispatched meetings accounted for")
    return 0


def cmd_pull_export_inputs(args: argparse.Namespace) -> int:
    pull_export_inputs(args.db)
    return 0


def cmd_checkpoint_db(args: argparse.Namespace) -> int:
    checkpoint_db(args.db)
    print(f"checkpointed {args.db}")
    return 0


def _fmt(value, width: int) -> str:
    text = "" if value is None else str(value)
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


def cmd_status(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    rows = db.iter_meetings(conn, args.limit)
    headers = [
        ("meeting_key", 34),
        ("date", 10),
        ("time", 8),
        ("body", 30),
        ("viebit", 34),
        ("fetch", 8),
        ("prep", 8),
        ("asr", 8),
        ("diar", 8),
        ("spk", 8),
        ("chap", 8),
        ("error", 48),
    ]
    print(" ".join(_fmt(name, width) for name, width in headers))
    print(" ".join("-" * width for _, width in headers))
    for row in rows:
        print(
            " ".join(
                [
                    _fmt(row["meeting_key"], 34),
                    _fmt((row["event_date"] or "")[:10], 10),
                    _fmt(row["event_time"], 8),
                    _fmt(row["body_name"], 30),
                    _fmt(row["viebit_filename"], 34),
                    _fmt(row["fetch_status"], 8),
                    _fmt(row["prepare_status"], 8),
                    _fmt(row["transcribe_status"], 8),
                    _fmt(row["diarize_status"], 8),
                    _fmt(row["name_speakers_status"], 8),
                    _fmt(row["chapterize_status"], 8),
                    _fmt(row["last_error"], 48),
                ]
            )
        )
    return 0


def cmd_ci_health(args: argparse.Namespace) -> int:
    try:
        conn = db.connect(args.db)
        # Scope to coverage: pre-floor backlog rows are permanently out of
        # scope, and their historical errors would train readers to skim past
        # this section.
        coverage_start = os.environ.get("COUNCIL_COVERAGE_START", "2026-06-20")[:10]
        print("last_error rows:")
        error_rows = conn.execute(
            """
            SELECT meeting_key, last_error FROM meetings
            WHERE last_error IS NOT NULL AND TRIM(last_error) != ''
              AND COALESCE(substr(event_date, 1, 10), substr(viebit_pub_date, 1, 10), substr(discovered_at, 1, 10)) >= ?
            ORDER BY updated_at DESC, meeting_key ASC
            """,
            (coverage_start,),
        ).fetchall()
        if not error_rows:
            print("none")
        for row in error_rows:
            print(f"{row['meeting_key']}: {_truncate_one_line(row['last_error'], 200)}")
        print(f"stale_unmatched_viebit_rows_older_than_7d={_stale_unmatched_viebit_count(conn, coverage_start)}")
        newest = conn.execute("SELECT MAX(event_date) AS newest FROM meetings WHERE event_date IS NOT NULL").fetchone()
        print(f"newest_event_date={newest['newest'] if newest and newest['newest'] else 'none'}")
    except Exception as exc:
        print(f"ci-health error: {exc}", file=sys.stderr, flush=True)
    return 0


def _truncate_one_line(value, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > width:
        return text[: width - 1] + "…"
    return text


def _stale_unmatched_viebit_count(conn: sqlite3.Connection, coverage_start: str) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    rows = conn.execute(
        """
        SELECT viebit_pub_date, discovered_at FROM meetings
        WHERE viebit_filename IS NOT NULL
          AND legistar_event_id IS NULL
          AND COALESCE(substr(viebit_pub_date, 1, 10), substr(discovered_at, 1, 10)) >= ?
        """,
        (coverage_start,),
    ).fetchall()
    count = 0
    for row in rows:
        timestamp = _parse_registry_timestamp(row["viebit_pub_date"] or row["discovered_at"])
        if timestamp is not None and timestamp < cutoff:
            count += 1
    return count


def _parse_registry_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="poll Viebit RSS and Legistar events")
    add_common(discover)
    discover.add_argument("--rss-url", help="alternate RSS URL, primarily for testing")
    discover.add_argument("--no-rss", action="store_true", help="skip Viebit RSS polling")
    discover.add_argument("--legistar-start", help="date-window start for Legistar backfill, YYYY-MM-DD")
    discover.add_argument("--legistar-end", help="exclusive date-window end for Legistar backfill, YYYY-MM-DD")
    discover.add_argument("--emit-pending-json", action="store_true", help="print a GitHub Actions matrix of pending meetings")
    discover.add_argument("--pending-limit", type=int, help="limit emitted pending meetings, oldest first")
    discover.set_defaults(func=cmd_discover)

    fetch = subparsers.add_parser("fetch", help="download media artifacts and extract audio")
    add_common(fetch)
    fetch.add_argument("--meeting-key", help="fetch one meeting")
    fetch.add_argument("--all-pending", action="store_true", help="fetch pending meetings instead of oldest RSS rows")
    fetch.add_argument("--limit", type=int, help="limit meetings, oldest first")
    fetch.add_argument("--force", action="store_true", help="run even if fetch_status is already fetched")
    fetch.set_defaults(func=cmd_fetch)

    prepare = subparsers.add_parser("prepare", help="prepare caption and agenda text artifacts")
    add_common(prepare)
    prepare.add_argument("--meeting-key", help="prepare one meeting")
    prepare.add_argument("--all-pending", action="store_true", help="prepare pending fetched meetings")
    prepare.add_argument("--limit", type=int, help="limit meetings, oldest first")
    prepare.add_argument("--force", action="store_true", help="run even if prepare_status is already prepared")
    prepare.set_defaults(func=cmd_prepare)

    transcribe_cmd = subparsers.add_parser("transcribe", help="run ASR over prepared audio")
    add_common(transcribe_cmd)
    transcribe_cmd.add_argument("--meeting-key", help="transcribe one registry meeting")
    transcribe_cmd.add_argument("--meeting-dir", type=Path, help="transcribe an artifact directory without registry updates")
    transcribe_cmd.add_argument("--limit", type=int, help="limit registry meetings, oldest first")
    transcribe_cmd.add_argument(
        "--backend",
        default="voxtral",
        choices=["local", "local-mlx", "remote", "voxtral", "scribe", "assemblyai"],
    )
    transcribe_cmd.add_argument("--model", help="backend model override")
    transcribe_cmd.set_defaults(func=cmd_transcribe)

    diarize_cmd = subparsers.add_parser("diarize", help="run speaker diarization and label ASR utterances")
    add_common(diarize_cmd)
    diarize_cmd.add_argument("--meeting-key", help="diarize one registry meeting")
    diarize_cmd.add_argument("--meeting-dir", type=Path, help="diarize an artifact directory without registry updates")
    diarize_cmd.add_argument("--limit", type=int, help="limit registry meetings, oldest first")
    diarize_cmd.add_argument("--model", default="pyannote/speaker-diarization-community-1")
    diarize_cmd.add_argument("--device", choices=["mps", "cpu"], help="torch device override; default prefers MPS")
    diarize_cmd.set_defaults(func=cmd_diarize)

    speakers_cmd = subparsers.add_parser("name-speakers", help="assign speaker names (glm-5.2 via OpenRouter by default)")
    add_common(speakers_cmd)
    speakers_cmd.add_argument("--meeting-key", help="name speakers for one registry meeting")
    speakers_cmd.add_argument("--meeting-dir", type=Path, help="name speakers in an artifact directory without registry updates")
    speakers_cmd.add_argument("--limit", type=int, help="limit registry meetings, oldest first")
    speakers_cmd.add_argument("--model", default=None, help="naming LLM override; defaults to the production config")
    _add_llm_override_args(speakers_cmd)
    speakers_cmd.set_defaults(func=cmd_name_speakers)

    chapterize_cmd = subparsers.add_parser("chapterize", help="chapterize a named transcript (glm-5.2 via OpenRouter by default)")
    add_common(chapterize_cmd)
    chapterize_cmd.add_argument("--meeting-key", help="chapterize one registry meeting")
    chapterize_cmd.add_argument("--meeting-dir", type=Path, help="chapterize an artifact directory without registry updates")
    chapterize_cmd.add_argument("--limit", type=int, help="limit registry meetings, oldest first")
    chapterize_cmd.add_argument("--model", default=None, help="chaptering LLM override; defaults to the production config")
    _add_llm_override_args(chapterize_cmd)
    chapterize_cmd.set_defaults(func=cmd_chapterize)

    export_cmd = subparsers.add_parser("export-site", help="write Astro meeting JSON from completed artifacts")
    add_common(export_cmd)
    export_cmd.add_argument("--no-benchmark", action="store_true", help="exclude data/benchmark meetings")
    export_cmd.add_argument("--allow-empty", action="store_true", help="keep existing site data if no completed meetings exist")
    export_cmd.set_defaults(func=cmd_export_site)

    pending_cmd = subparsers.add_parser("pending-matrix", help="emit a GitHub Actions matrix of pending prod meetings")
    add_common(pending_cmd)
    pending_cmd.add_argument("--limit", type=int, help="limit pending meetings, oldest first")
    pending_cmd.set_defaults(func=cmd_pending_matrix)

    process_cmd = subparsers.add_parser("process-one", help="run one registry meeting through the prod cloud profile")
    add_common(process_cmd)
    process_cmd.add_argument("--meeting-key", required=True, help="registry meeting key to process")
    process_cmd.add_argument("--result-json", type=Path, help="write mergeable per-meeting result JSON")
    process_cmd.add_argument("--dry-run", action="store_true", help="exercise registry gates without network/API/upload work")
    process_cmd.add_argument("--fail-on-error", action="store_true", help="exit nonzero on meeting failure")
    process_cmd.set_defaults(func=cmd_process_one)

    merge_cmd = subparsers.add_parser("merge-results", help="merge process-one result JSON files into the registry")
    add_common(merge_cmd)
    merge_cmd.add_argument("--results-dir", type=Path, required=True, help="directory containing process-one result JSON files")
    merge_cmd.set_defaults(func=cmd_merge_results)

    verify_cmd = subparsers.add_parser(
        "verify-run-results",
        help="fail unless every meeting in this run's matrix has a result record",
    )
    verify_cmd.add_argument("--results-dir", type=Path, required=True, help="directory containing synced result JSON files")
    verify_cmd.add_argument("--matrix-json", required=True, help="the run's pending matrix JSON ({\"include\": [...]})")
    verify_cmd.set_defaults(func=cmd_verify_run_results)

    pull_cmd = subparsers.add_parser(
        "pull-export-inputs",
        help="fetch from R2 the meeting dirs export-site needs but are missing locally",
    )
    add_common(pull_cmd)
    pull_cmd.set_defaults(func=cmd_pull_export_inputs)

    checkpoint_cmd = subparsers.add_parser("checkpoint-db", help="checkpoint SQLite WAL state into the registry DB file")
    add_common(checkpoint_cmd)
    checkpoint_cmd.set_defaults(func=cmd_checkpoint_db)

    status = subparsers.add_parser("status", help="print registry stage states")
    add_common(status)
    status.add_argument("--limit", type=int, default=40)
    status.set_defaults(func=cmd_status)

    ci_health = subparsers.add_parser("ci-health", help="print informational CI registry health checks")
    add_common(ci_health)
    ci_health.set_defaults(func=cmd_ci_health)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
