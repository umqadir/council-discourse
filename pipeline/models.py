from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Meeting:
    meeting_key: str
    meeting_dir: Path
    legistar_event_id: int | None = None
    legistar_event_guid: str | None = None
    viebit_filename: str | None = None
    viebit_hash: str | None = None
    body_name: str | None = None
    event_date: str | None = None
    event_time: str | None = None
    agenda_pdf_url: str | None = None
    insite_url: str | None = None
