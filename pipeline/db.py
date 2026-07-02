from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import MEETINGS_DIR, REGISTRY_DB
from .models import Meeting
from .utils import safe_key, utc_now_iso

SCHEMA_VERSION = "1"

MEETING_COLUMNS = [
    "meeting_key",
    "legistar_event_id",
    "legistar_event_guid",
    "viebit_filename",
    "viebit_hash",
    "viebit_pub_date",
    "body_name",
    "event_date",
    "event_time",
    "event_location",
    "event_last_modified_utc",
    "agenda_pdf_url",
    "insite_url",
    "duration_seconds",
    "discover_status",
    "fetch_status",
    "prepare_status",
    "transcribe_status",
    "name_speakers_status",
    "chapterize_status",
    "discovered_at",
    "fetched_at",
    "prepared_at",
    "created_at",
    "updated_at",
    "last_error",
]


def connect(db_path: Path = REGISTRY_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meetings (
            meeting_key TEXT PRIMARY KEY,
            legistar_event_id INTEGER,
            legistar_event_guid TEXT,
            viebit_filename TEXT,
            viebit_hash TEXT,
            viebit_pub_date TEXT,
            body_name TEXT,
            event_date TEXT,
            event_time TEXT,
            event_location TEXT,
            event_last_modified_utc TEXT,
            agenda_pdf_url TEXT,
            insite_url TEXT,
            duration_seconds REAL,
            discover_status TEXT NOT NULL DEFAULT 'pending',
            fetch_status TEXT NOT NULL DEFAULT 'pending',
            prepare_status TEXT NOT NULL DEFAULT 'pending',
            transcribe_status TEXT NOT NULL DEFAULT 'stubbed',
            name_speakers_status TEXT NOT NULL DEFAULT 'stubbed',
            chapterize_status TEXT NOT NULL DEFAULT 'stubbed',
            discovered_at TEXT,
            fetched_at TEXT,
            prepared_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_event_id
            ON meetings(legistar_event_id)
            WHERE legistar_event_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_viebit_filename
            ON meetings(viebit_filename)
            WHERE viebit_filename IS NOT NULL;
        """
    )
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def meeting_key_for(values: dict[str, Any]) -> str:
    filename = values.get("viebit_filename")
    if filename:
        return safe_key(str(filename).removesuffix(".mp4"))
    event_id = values.get("legistar_event_id")
    if event_id is not None:
        return f"event-{event_id}"
    raise ValueError("meeting needs either viebit_filename or legistar_event_id")


def _find_existing_row(conn: sqlite3.Connection, values: dict[str, Any]) -> sqlite3.Row | None:
    event_id = values.get("legistar_event_id")
    filename = values.get("viebit_filename")
    if event_id is not None:
        row = conn.execute(
            "SELECT * FROM meetings WHERE legistar_event_id = ?", (event_id,)
        ).fetchone()
        if row:
            return row
    if filename:
        row = conn.execute(
            "SELECT * FROM meetings WHERE viebit_filename = ?", (filename,)
        ).fetchone()
        if row:
            return row
    return None


def upsert_meeting(conn: sqlite3.Connection, values: dict[str, Any]) -> sqlite3.Row:
    now = utc_now_iso()
    values = {k: v for k, v in values.items() if k in MEETING_COLUMNS and v is not None}
    values.setdefault("discover_status", "discovered")
    values["updated_at"] = now
    existing = _find_existing_row(conn, values)

    if existing:
        merged = dict(existing)
        for key, value in values.items():
            if key == "discovered_at" and merged.get("discovered_at"):
                continue
            if value is not None:
                merged[key] = value
        merged["updated_at"] = now
        assignments = ", ".join(f"{col} = :{col}" for col in MEETING_COLUMNS if col != "meeting_key")
        conn.execute(
            f"UPDATE meetings SET {assignments} WHERE meeting_key = :meeting_key",
            {col: merged.get(col) for col in MEETING_COLUMNS},
        )
        conn.commit()
        return get_meeting(conn, str(merged["meeting_key"]))

    values.setdefault("meeting_key", meeting_key_for(values))
    values.setdefault("created_at", now)
    values.setdefault("discovered_at", now)
    for status_col, status in [
        ("fetch_status", "pending"),
        ("prepare_status", "pending"),
        ("transcribe_status", "stubbed"),
        ("name_speakers_status", "stubbed"),
        ("chapterize_status", "stubbed"),
    ]:
        values.setdefault(status_col, status)

    columns = [col for col in MEETING_COLUMNS if col in values]
    placeholders = ", ".join(f":{col}" for col in columns)
    conn.execute(
        f"INSERT INTO meetings ({', '.join(columns)}) VALUES ({placeholders})",
        {col: values[col] for col in columns},
    )
    conn.commit()
    return get_meeting(conn, str(values["meeting_key"]))


def update_meeting(conn: sqlite3.Connection, meeting_key: str, values: dict[str, Any]) -> None:
    values = {k: v for k, v in values.items() if k in MEETING_COLUMNS}
    values["updated_at"] = utc_now_iso()
    assignments = ", ".join(f"{col} = :{col}" for col in values)
    values["meeting_key"] = meeting_key
    conn.execute(
        f"UPDATE meetings SET {assignments} WHERE meeting_key = :meeting_key",
        values,
    )
    conn.commit()


def get_meeting(conn: sqlite3.Connection, meeting_key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM meetings WHERE meeting_key = ?", (meeting_key,)).fetchone()
    if row is None:
        raise KeyError(f"unknown meeting: {meeting_key}")
    return row


def iter_meetings(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT * FROM meetings
        ORDER BY COALESCE(event_date, viebit_pub_date, discovered_at) DESC,
                 COALESCE(event_time, '') DESC,
                 meeting_key
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def select_fetch_candidates(
    conn: sqlite3.Connection,
    meeting_key: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    if meeting_key:
        return [get_meeting(conn, meeting_key)]
    sql = """
        SELECT * FROM meetings
        WHERE viebit_filename IS NOT NULL
          AND fetch_status != 'fetched'
        ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def select_oldest_viebit_meetings(
    conn: sqlite3.Connection,
    meeting_key: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    if meeting_key:
        return [get_meeting(conn, meeting_key)]
    sql = """
        SELECT * FROM meetings
        WHERE viebit_filename IS NOT NULL
        ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def select_prepare_candidates(
    conn: sqlite3.Connection,
    meeting_key: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    if meeting_key:
        return [get_meeting(conn, meeting_key)]
    sql = """
        SELECT * FROM meetings
        WHERE fetch_status = 'fetched'
          AND prepare_status != 'prepared'
        ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def select_transcribe_candidates(
    conn: sqlite3.Connection,
    meeting_key: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    if meeting_key:
        return [get_meeting(conn, meeting_key)]
    sql = """
        SELECT * FROM meetings
        WHERE prepare_status = 'prepared'
          AND transcribe_status != 'transcribed'
        ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def select_name_speakers_candidates(
    conn: sqlite3.Connection,
    meeting_key: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    if meeting_key:
        return [get_meeting(conn, meeting_key)]
    sql = """
        SELECT * FROM meetings
        WHERE transcribe_status = 'transcribed'
          AND name_speakers_status != 'named'
        ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def select_chapterize_candidates(
    conn: sqlite3.Connection,
    meeting_key: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    if meeting_key:
        return [get_meeting(conn, meeting_key)]
    sql = """
        SELECT * FROM meetings
        WHERE name_speakers_status = 'named'
          AND chapterize_status != 'chapterized'
        ORDER BY COALESCE(viebit_pub_date, discovered_at) ASC, meeting_key ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return list(conn.execute(sql, params))


def meeting_from_row(row: sqlite3.Row, meetings_dir: Path = MEETINGS_DIR) -> Meeting:
    return Meeting(
        meeting_key=str(row["meeting_key"]),
        meeting_dir=meetings_dir / str(row["meeting_key"]),
        legistar_event_id=row["legistar_event_id"],
        legistar_event_guid=row["legistar_event_guid"],
        viebit_filename=row["viebit_filename"],
        viebit_hash=row["viebit_hash"],
        body_name=row["body_name"],
        event_date=row["event_date"],
        event_time=row["event_time"],
        duration_seconds=row["duration_seconds"],
        agenda_pdf_url=row["agenda_pdf_url"],
        insite_url=row["insite_url"],
    )


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
