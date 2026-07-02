from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import db
from .config import MEETINGS_DIR
from .utils import atomic_write_text, utc_now_iso


def ts_to_sec(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_vtt(path: Path) -> list[dict]:
    raw = path.read_text(errors="replace")
    cues: list[dict] = []
    block: list[str] = []
    for line in raw.splitlines():
        if "-->" in line:
            m = re.match(r"([\d:.]+)\s+-->\s+([\d:.]+)", line.strip())
            if not m:
                continue
            block = []
            cues.append({"start": ts_to_sec(m.group(1)), "end": ts_to_sec(m.group(2)), "lines": block})
        elif line.strip() and cues and not line.startswith(("WEBVTT", "NOTE")):
            block.append(line.strip())
    return cues


def dedupe_rollup(cues: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen_tail: list[str] = []
    for cue in cues:
        for frag in cue["lines"]:
            norm = re.sub(r"\s+", " ", frag).strip()
            if not norm or norm in seen_tail:
                continue
            out.append({"t": round(float(cue["start"]), 2), "text": norm})
            seen_tail.append(norm)
            if len(seen_tail) > 8:
                seen_tail.pop(0)
    return out


def caption_text_lines(frags: list[dict]) -> list[str]:
    lines = []
    for frag in frags:
        t = float(frag["t"])
        lines.append(f"[{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}] {frag['text']}")
    return lines


def _pdftotext(pdf: Path, txt: Path) -> bool:
    if not pdf.exists() or txt.exists():
        return False
    if not shutil.which("pdftotext"):
        raise RuntimeError("required command not found on PATH: pdftotext")
    tmp = txt.with_name(f".{txt.name}.tmp")
    subprocess.run(["pdftotext", "-layout", str(pdf), str(tmp)], check=True)
    tmp.replace(txt)
    return True


def prepare_meeting(conn: sqlite3.Connection, row: sqlite3.Row, meetings_dir: Path = MEETINGS_DIR) -> None:
    meeting = db.meeting_from_row(row, meetings_dir)
    meeting_dir = meeting.meeting_dir
    vtt = meeting_dir / "captions.vtt"
    if not vtt.exists():
        raise RuntimeError(f"missing captions.vtt for {meeting.meeting_key}")

    jsonl = meeting_dir / "captions-clean.jsonl"
    txt = meeting_dir / "captions-clean.txt"
    if not jsonl.exists() or not txt.exists():
        frags = dedupe_rollup(parse_vtt(vtt))
        if not frags:
            raise RuntimeError(f"no caption fragments parsed for {meeting.meeting_key}")
        atomic_write_text(jsonl, "\n".join(json.dumps(f) for f in frags) + "\n")
        atomic_write_text(txt, "\n".join(caption_text_lines(frags)) + "\n")

    _pdftotext(meeting_dir / "agenda.pdf", meeting_dir / "agenda.txt")

    db.update_meeting(
        conn,
        meeting.meeting_key,
        {
            "prepare_status": "prepared",
            "prepared_at": utc_now_iso(),
            "last_error": None,
        },
    )
