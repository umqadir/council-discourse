from __future__ import annotations

import os
import re
import subprocess
import sys
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


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def safe_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return key.strip("-") or "meeting"


def run_checked(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def with_retries(fn, attempts: int = 3, base_delay: float = 5.0):
    """Call fn() with exponential backoff on any exception; re-raise the last."""
    import time

    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberate broad retry boundary
            last = exc
            if i < attempts - 1:
                delay = base_delay * (2**i)
                print(f"  retryable failure ({exc}); retrying in {delay:.0f}s", file=sys.stderr, flush=True)
                time.sleep(delay)
    raise last
