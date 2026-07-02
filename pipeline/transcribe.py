from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .artifacts import round_sec, write_json, write_jsonl
from .config import ROOT
from .models import Meeting

DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"


def transcribe_meeting(
    meeting: Meeting,
    backend: str = "local-mlx",
    model: str | None = None,
) -> Path:
    if backend == "api":
        raise NotImplementedError(
            "api transcription backend is not implemented yet. It will require a provider "
            "selection plus one of MISTRAL_API_KEY, ELEVENLABS_API_KEY, or ASSEMBLYAI_API_KEY."
        )
    if backend != "local-mlx":
        raise ValueError(f"unsupported transcription backend: {backend}")
    return transcribe_local_mlx(meeting, model=model or DEFAULT_PARAKEET_MODEL)


def transcribe_local_mlx(meeting: Meeting, model: str = DEFAULT_PARAKEET_MODEL) -> Path:
    audio = _audio_path(meeting.meeting_dir)
    output = meeting.meeting_dir / "utterances.jsonl"
    meta_path = meeting.meeting_dir / "transcribe-meta.json"
    meeting.meeting_dir.mkdir(parents=True, exist_ok=True)

    if shutil.which("uv") is None:
        raise RuntimeError("required command not found on PATH: uv")

    with tempfile.TemporaryDirectory(prefix=".parakeet-", dir=meeting.meeting_dir) as tmp_name:
        tmp_dir = Path(tmp_name)
        template = "parakeet"
        json_path = tmp_dir / f"{template}.json"
        cmd = [
            "uv",
            "run",
            "parakeet-mlx",
            "--model",
            model,
            "--output-format",
            "json",
            "--output-dir",
            str(tmp_dir),
            "--output-template",
            template,
            str(audio),
        ]
        started = time.monotonic()
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
        wall_clock = time.monotonic() - started
        if proc.returncode != 0:
            stderr = proc.stderr.strip()[-4000:]
            stdout = proc.stdout.strip()[-1000:]
            raise RuntimeError(f"parakeet-mlx failed with code {proc.returncode}: {stderr or stdout}")
        if not json_path.exists():
            raise RuntimeError(f"parakeet-mlx did not write expected JSON output: {json_path}")
        result = json.loads(json_path.read_text())

    utterances = _parakeet_sentences_to_utterances(result)
    if not utterances:
        raise RuntimeError("parakeet-mlx returned no timestamped sentence segments")

    duration = _audio_duration(audio)
    write_jsonl(output, utterances)
    write_json(
        meta_path,
        {
            "backend": "local-mlx",
            "engine": "parakeet-mlx",
            "engine_version": _package_version("parakeet-mlx"),
            "model": model,
            "audio_file": str(audio),
            "audio_duration_sec": round_sec(duration),
            "utterance_count": len(utterances),
            "wall_clock_sec": round_sec(wall_clock),
            "rtf": round(wall_clock / duration, 4) if duration else None,
        },
    )
    return output


def _audio_path(meeting_dir: Path) -> Path:
    for name in ("audio-16k.wav", "audio.wav", "audio.m4a"):
        path = meeting_dir / name
        if path.exists() and path.stat().st_size > 10_000:
            return path
    raise RuntimeError(f"missing prepared audio in {meeting_dir}: expected audio-16k.wav")


def _audio_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(proc.stdout.strip())


def _parakeet_sentences_to_utterances(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sentence in result.get("sentences", []):
        text = str(sentence.get("text", "")).strip()
        if not text:
            continue
        start = float(sentence["start"])
        end = float(sentence["end"])
        if end <= start:
            continue
        row = {
            "t0": round_sec(start),
            "t1": round_sec(end),
            "text": text,
        }
        if "confidence" in sentence:
            row["confidence"] = round(float(sentence["confidence"]), 4)
        rows.append(row)
    return rows


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None

