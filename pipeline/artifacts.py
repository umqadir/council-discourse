from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .utils import atomic_write_text


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_text(path, "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")


def round_sec(value: float | int | str | None) -> float:
    if value is None:
        return 0.0
    return round(float(value), 3)


def parse_clock(value: str) -> float:
    parts = [float(part) for part in str(value).strip().split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"unsupported clock value: {value}")


def sec_to_clock(value: float | int) -> str:
    seconds = max(0, int(float(value)))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def utterance_start(row: dict[str, Any]) -> float:
    return round_sec(row.get("t0", row.get("t", row.get("start", row.get("start_sec", 0)))))


def utterance_end(row: dict[str, Any], fallback: float | None = None) -> float:
    if "t1" in row:
        return round_sec(row["t1"])
    if "end" in row:
        return round_sec(row["end"])
    if "end_sec" in row:
        return round_sec(row["end_sec"])
    if fallback is not None:
        return round_sec(fallback)
    return utterance_start(row)


def normalize_utterances(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    starts = [utterance_start(row) for row in rows]
    for index, row in enumerate(rows):
        next_start = starts[index + 1] if index + 1 < len(starts) else starts[index] + 4.0
        t0 = starts[index]
        t1 = utterance_end(row, next_start)
        if t1 <= t0:
            t1 = max(t0 + 0.5, next_start)
        text = clean_text(row.get("text"))
        if not text:
            continue
        out = dict(row)
        out["t0"] = round_sec(t0)
        out["t1"] = round_sec(t1)
        out["text"] = text
        normalized.append(out)
    return normalized


def captions_to_utterances(captions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return normalize_utterances(captions)

