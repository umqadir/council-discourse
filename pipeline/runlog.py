from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import read_jsonl, write_jsonl
from .utils import utc_now_iso


def append_gemini_runlog(
    meeting_dir: Path,
    stage: str,
    model: str,
    meta: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> Path:
    path = meeting_dir / "gemini-runlog.jsonl"
    rows = read_jsonl(path)
    row: dict[str, Any] = {
        "created_at": utc_now_iso(),
        "stage": stage,
        "model": model,
        "elapsed_sec": meta.get("elapsed_sec"),
        "usage": meta.get("usage", {}),
    }
    if meta.get("estimated_cost_usd") is not None:
        row["estimated_cost_usd"] = meta["estimated_cost_usd"]
    if meta.get("pricing") is not None:
        row["pricing"] = meta["pricing"]
    if extra:
        row.update(extra)
    rows.append(row)
    write_jsonl(path, rows)
    return path
