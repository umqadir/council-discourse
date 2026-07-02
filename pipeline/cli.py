from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import db
from .config import REGISTRY_DB
from .discover import discover_legistar, discover_viebit_rss
from .fetch import fetch_meeting
from .prepare import prepare_meeting


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=REGISTRY_DB, help="SQLite registry path")


def cmd_discover(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    rss_count = discover_viebit_rss(conn, args.rss_url)
    event_count, skipped_legistar = discover_legistar(conn)
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
) -> int:
    failures = 0
    for row in rows:
        key = str(row["meeting_key"])
        if row[stage_col] == complete_status:
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
    return _run_rows(conn, rows, fetch_meeting, "fetch_status", "fetched", "fetch")


def cmd_prepare(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    if args.all_pending:
        rows = db.select_prepare_candidates(conn, args.meeting_key, args.limit)
    else:
        rows = db.select_prepare_candidates(conn, args.meeting_key, args.limit or 1)
    if not rows:
        print("no prepare candidates")
        return 0
    return _run_rows(conn, rows, prepare_meeting, "prepare_status", "prepared", "prepare")


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
    discover.set_defaults(func=cmd_discover)

    fetch = subparsers.add_parser("fetch", help="download media artifacts and extract audio")
    add_common(fetch)
    fetch.add_argument("--meeting-key", help="fetch one meeting")
    fetch.add_argument("--all-pending", action="store_true", help="fetch pending meetings instead of oldest RSS rows")
    fetch.add_argument("--limit", type=int, help="limit meetings, oldest first")
    fetch.set_defaults(func=cmd_fetch)

    prepare = subparsers.add_parser("prepare", help="prepare caption and agenda text artifacts")
    add_common(prepare)
    prepare.add_argument("--meeting-key", help="prepare one meeting")
    prepare.add_argument("--all-pending", action="store_true", help="prepare pending fetched meetings")
    prepare.add_argument("--limit", type=int, help="limit meetings, oldest first")
    prepare.set_defaults(func=cmd_prepare)

    status = subparsers.add_parser("status", help="print registry stage states")
    add_common(status)
    status.add_argument("--limit", type=int, default=40)
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
