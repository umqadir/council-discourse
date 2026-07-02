from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import db
from .config import REGISTRY_DB
from .discover import discover_legistar, discover_viebit_rss
from .export_site import export_site
from .fetch import fetch_meeting
from .models import Meeting
from .prepare import prepare_meeting
from .stages import chapterize, diarize, name_speakers, transcribe
from .utils import utc_now_iso


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=REGISTRY_DB, help="SQLite registry path")


def cmd_discover(args: argparse.Namespace) -> int:
    if bool(args.legistar_start) != bool(args.legistar_end):
        print("--legistar-start and --legistar-end must be supplied together", file=sys.stderr)
        return 2
    conn = db.connect(args.db)
    rss_count = 0 if args.no_rss else discover_viebit_rss(conn, args.rss_url)
    event_count, skipped_legistar = discover_legistar(
        conn,
        start_date=args.legistar_start,
        end_date=args.legistar_end,
    )
    if skipped_legistar:
        print(f"viebit_rss={rss_count}; legistar=skipped (LEGISTAR_TOKEN unset)")
    else:
        print(f"viebit_rss={rss_count}; legistar_events={event_count}")
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
    if args.meeting_dir:
        return _run_meeting_dir_stage(
            args,
            "name-speakers",
            lambda meeting: name_speakers(meeting, model=args.model),
        )

    conn = db.connect(args.db)
    rows = db.select_name_speakers_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no name-speakers candidates")
        return 0

    def run_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        meeting = db.meeting_from_row(row)
        name_speakers(meeting, model=args.model)
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
    if args.meeting_dir:
        return _run_meeting_dir_stage(
            args,
            "chapterize",
            lambda meeting: chapterize(meeting, model=args.model),
        )

    conn = db.connect(args.db)
    rows = db.select_chapterize_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no chapterize candidates")
        return 0

    def run_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        meeting = db.meeting_from_row(row)
        chapterize(meeting, model=args.model)
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
    written = export_site(args.db, include_benchmark=not args.no_benchmark)
    for path in written:
        print(f"wrote {path}", flush=True)
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
                ]
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="poll Viebit RSS and Legistar events")
    add_common(discover)
    discover.add_argument("--rss-url", help="alternate RSS URL, primarily for testing")
    discover.add_argument("--no-rss", action="store_true", help="skip Viebit RSS polling")
    discover.add_argument("--legistar-start", help="date-window start for Legistar backfill, YYYY-MM-DD")
    discover.add_argument("--legistar-end", help="exclusive date-window end for Legistar backfill, YYYY-MM-DD")
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

    speakers_cmd = subparsers.add_parser("name-speakers", help="assign speaker names with Gemini")
    add_common(speakers_cmd)
    speakers_cmd.add_argument("--meeting-key", help="name speakers for one registry meeting")
    speakers_cmd.add_argument("--meeting-dir", type=Path, help="name speakers in an artifact directory without registry updates")
    speakers_cmd.add_argument("--limit", type=int, help="limit registry meetings, oldest first")
    speakers_cmd.add_argument("--model", default="gemini-3.5-flash")
    speakers_cmd.set_defaults(func=cmd_name_speakers)

    chapterize_cmd = subparsers.add_parser("chapterize", help="chapterize a named transcript with Gemini")
    add_common(chapterize_cmd)
    chapterize_cmd.add_argument("--meeting-key", help="chapterize one registry meeting")
    chapterize_cmd.add_argument("--meeting-dir", type=Path, help="chapterize an artifact directory without registry updates")
    chapterize_cmd.add_argument("--limit", type=int, help="limit registry meetings, oldest first")
    chapterize_cmd.add_argument("--model", default="gemini-3.5-flash")
    chapterize_cmd.set_defaults(func=cmd_chapterize)

    export_cmd = subparsers.add_parser("export-site", help="write Astro meeting JSON from completed artifacts")
    add_common(export_cmd)
    export_cmd.add_argument("--no-benchmark", action="store_true", help="exclude data/benchmark meetings")
    export_cmd.set_defaults(func=cmd_export_site)

    status = subparsers.add_parser("status", help="print registry stage states")
    add_common(status)
    status.add_argument("--limit", type=int, default=40)
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
