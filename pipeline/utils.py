from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Sequence


def utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    ensure_parent(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def safe_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return key.strip("-") or "meeting"


def run_checked(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)
