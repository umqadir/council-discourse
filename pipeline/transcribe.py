from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .artifacts import clean_text, read_json, round_sec, write_json, write_jsonl
from .config import ROOT
from .models import Meeting
from .utils import load_dotenv

DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
DEFAULT_VOXTRAL_MODEL = "voxtral-mini-2602"
VOXTRAL_API_URL = "https://api.mistral.ai/v1/audio/transcriptions"
VOXTRAL_SPLIT_THRESHOLD_SEC = 10_800.0
VOXTRAL_SILENCE_WINDOW_SEC = 1_800.0
VOXTRAL_LABEL_LIMITATION = (
    "Voxtral diarization labels are request-local. For split audio, labels from part 2 "
    "and later are suffixed with _partN so the speaker-naming stage does not merge "
    "unrelated speakers across API requests."
)


def transcribe_meeting(
    meeting: Meeting,
    backend: str = "local",
    model: str | None = None,
) -> Path:
    if backend == "remote":
        raise NotImplementedError(
            "remote transcription backend is not implemented yet. The provider interface is "
            "reserved for the remote ASR backend selection."
        )
    if backend in {"voxtral", "mistral-voxtral"}:
        return transcribe_voxtral(meeting, model=model or DEFAULT_VOXTRAL_MODEL)
    if backend not in {"local", "local-mlx"}:
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


def transcribe_voxtral(meeting: Meeting, model: str = DEFAULT_VOXTRAL_MODEL) -> Path:
    audio = _voxtral_audio_path(meeting.meeting_dir)
    output = meeting.meeting_dir / "utterances-voxtral.jsonl"
    labeled_output = meeting.meeting_dir / "utterances-voxtral-labeled.jsonl"
    meta_path = meeting.meeting_dir / "transcribe-voxtral-meta.json"
    transcript_path = meeting.meeting_dir / "voxtral-transcript.json"
    meeting.meeting_dir.mkdir(parents=True, exist_ok=True)

    duration = _meeting_duration(meeting, audio)
    started = time.monotonic()
    parts: list[dict[str, Any]]
    split_meta: dict[str, Any]
    if duration > VOXTRAL_SPLIT_THRESHOLD_SEC:
        split_sec, split_reason = _find_silence_split(audio, duration)
        with tempfile.TemporaryDirectory(prefix=".voxtral-", dir=meeting.meeting_dir) as tmp_name:
            part_paths = _split_audio(audio, split_sec, Path(tmp_name))
            parts = [
                {
                    "index": 1,
                    "path": part_paths[0],
                    "source_audio_file": str(audio),
                    "temporary_audio": True,
                    "offset_sec": 0.0,
                    "speaker_suffix": "",
                    "split_end_sec": split_sec,
                },
                {
                    "index": 2,
                    "path": part_paths[1],
                    "source_audio_file": str(audio),
                    "temporary_audio": True,
                    "offset_sec": split_sec,
                    "speaker_suffix": "_part2",
                    "split_start_sec": split_sec,
                },
            ]
            utterances, labeled, merged_result, part_records = _transcribe_voxtral_parts(
                meeting.meeting_dir,
                parts,
                model,
            )
        split_meta = {
            "enabled": True,
            "threshold_sec": VOXTRAL_SPLIT_THRESHOLD_SEC,
            "split_sec": round_sec(split_sec),
            "split_reason": split_reason,
            "parts": part_records,
            "speaker_label_limitation": VOXTRAL_LABEL_LIMITATION,
        }
    else:
        parts = [{"index": 1, "path": audio, "offset_sec": 0.0, "speaker_suffix": ""}]
        utterances, labeled, merged_result, part_records = _transcribe_voxtral_parts(
            meeting.meeting_dir,
            parts,
            model,
        )
        split_meta = {
            "enabled": False,
            "threshold_sec": VOXTRAL_SPLIT_THRESHOLD_SEC,
            "parts": part_records,
        }

    wall_clock = time.monotonic() - started
    if not utterances:
        raise RuntimeError("Voxtral returned no timestamped segments")

    usage = merged_result.get("usage", {}) if isinstance(merged_result, dict) else {}
    write_jsonl(output, utterances)
    write_jsonl(labeled_output, labeled)
    write_json(transcript_path, merged_result)
    labels = sorted({str(row["label"]) for row in labeled if str(row.get("label") or "").strip()})
    meta = {
        "backend": "voxtral",
        "engine": "mistral-audio-transcriptions",
        "model": model,
        "audio_file": str(audio),
        "audio_duration_sec": round_sec(duration),
        "utterances_output": str(output),
        "labeled_output": str(labeled_output),
        "raw_transcript_output": str(transcript_path),
        "utterance_count": len(utterances),
        "label_count": len(labels),
        "labels": labels,
        "wall_clock_sec": round_sec(wall_clock),
        "rtf": round(wall_clock / duration, 4) if duration else None,
        "usage": usage,
        "split": split_meta,
    }
    write_json(meta_path, meta)
    _write_generic_transcribe_meta_if_safe(meeting.meeting_dir, meta)
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


def _voxtral_audio_path(meeting_dir: Path) -> Path:
    for name in ("audio.m4a", "audio-16k.wav", "audio.wav"):
        path = meeting_dir / name
        if path.exists() and path.stat().st_size > 10_000:
            return path
    raise RuntimeError(f"missing prepared audio in {meeting_dir}: expected audio.m4a")


def _meeting_duration(meeting: Meeting, audio: Path) -> float:
    if meeting.duration_seconds and float(meeting.duration_seconds) > 0:
        return float(meeting.duration_seconds)
    return _audio_duration(audio)


def _find_silence_split(audio: Path, duration: float) -> tuple[float, str]:
    _require_command("ffmpeg")
    target = duration / 2
    window = min(VOXTRAL_SILENCE_WINDOW_SEC, max(120.0, duration - 120.0))
    window_start = max(0.0, target - window / 2)
    window_duration = min(window, duration - window_start)
    silences = _detect_silences(audio, window_start, window_duration)
    if not silences:
        return target, "no_silence_detected_near_midpoint"

    candidates = [
        ((start + end) / 2, start, end) for start, end in silences if 0 < start < duration and end > start
    ]
    if not candidates:
        return target, "no_valid_silence_detected_near_midpoint"
    midpoint, start, end = min(candidates, key=lambda item: abs(item[0] - target))
    split = max(60.0, min(duration - 60.0, midpoint))
    return split, f"nearest_detected_silence_{round_sec(start)}_{round_sec(end)}"


def _detect_silences(audio: Path, window_start: float, window_duration: float) -> list[tuple[float, float]]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{window_start:.3f}",
        "-t",
        f"{window_duration:.3f}",
        "-i",
        str(audio),
        "-af",
        "silencedetect=noise=-35dB:d=0.4",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg silencedetect failed: {proc.stderr.strip()[-2000:]}")

    silences: list[tuple[float, float]] = []
    active_start: float | None = None
    for line in proc.stderr.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            active_start = _window_time_to_absolute(float(start_match.group(1)), window_start, window_duration)
            continue
        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if not end_match:
            continue
        end = _window_time_to_absolute(float(end_match.group(1)), window_start, window_duration)
        if active_start is not None and end > active_start:
            silences.append((active_start, end))
        active_start = None
    if active_start is not None:
        silences.append((active_start, window_start + window_duration))
    return silences


def _window_time_to_absolute(value: float, window_start: float, window_duration: float) -> float:
    if value <= window_duration + 5.0:
        return value + window_start
    return value


def _split_audio(audio: Path, split_sec: float, tmp_dir: Path) -> tuple[Path, Path]:
    _require_command("ffmpeg")
    part1 = tmp_dir / "voxtral-part-1.m4a"
    part2 = tmp_dir / "voxtral-part-2.m4a"
    common = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    encode_args = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k"]
    _run_ffmpeg(common + ["-i", str(audio), "-t", f"{split_sec:.3f}"] + encode_args + [str(part1)])
    _run_ffmpeg(common + ["-ss", f"{split_sec:.3f}", "-i", str(audio)] + encode_args + [str(part2)])
    return part1, part2


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with code {proc.returncode}: {proc.stderr.strip()[-2000:]}")


def _transcribe_voxtral_parts(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    merged_text: list[str] = []
    usage_totals: dict[str, int | float] = {}
    part_records: list[dict[str, Any]] = []
    languages: list[str] = []

    for part in parts:
        result, request_meta = _request_voxtral_transcription(Path(part["path"]), model)
        part_index = int(part["index"])
        raw_path = meeting_dir / f"voxtral-transcript-part-{part_index}.json"
        write_json(raw_path, result)
        offset = float(part.get("offset_sec") or 0)
        speaker_suffix = str(part.get("speaker_suffix") or "")
        part_utterances, part_labeled, part_segments = _voxtral_result_to_rows(
            result,
            offset_sec=offset,
            speaker_suffix=speaker_suffix,
            part_index=part_index,
        )
        utterances.extend(part_utterances)
        labeled.extend(part_labeled)
        merged_segments.extend(part_segments)
        text = clean_text(result.get("text"))
        if text:
            merged_text.append(text)
        usage = result.get("usage", {})
        if isinstance(usage, dict):
            _add_numeric_usage(usage_totals, usage)
        language = str(result.get("language") or "").strip()
        if language and language not in languages:
            languages.append(language)
        speakers = sorted({str(row["label"]) for row in part_labeled if str(row.get("label") or "").strip()})
        part_record = {
            "index": part_index,
            "offset_sec": round_sec(offset),
            "speaker_suffix": speaker_suffix,
            "request_wall_clock_sec": request_meta["wall_clock_sec"],
            "attempts": request_meta.get("attempts", 1),
            "segment_count": len(part_segments),
            "utterance_count": len(part_utterances),
            "speaker_count": len(speakers),
            "raw_transcript_output": str(raw_path),
        }
        if part.get("source_audio_file"):
            part_record["source_audio_file"] = str(part["source_audio_file"])
        if part.get("temporary_audio"):
            part_record["temporary_audio_name"] = Path(part["path"]).name
        else:
            part_record["audio_file"] = str(part["path"])
        if "split_start_sec" in part:
            part_record["split_start_sec"] = round_sec(float(part["split_start_sec"]))
        if "split_end_sec" in part:
            part_record["split_end_sec"] = round_sec(float(part["split_end_sec"]))
        part_records.append(part_record)

    utterances.sort(key=lambda row: (float(row["t0"]), float(row["t1"])))
    labeled.sort(key=lambda row: (float(row["t0"]), float(row["t1"])))
    merged_segments.sort(key=lambda row: (float(row.get("start", 0)), float(row.get("end", 0))))
    merged_result: dict[str, Any] = {
        "model": model,
        "text": "\n".join(merged_text),
        "segments": merged_segments,
        "usage": usage_totals,
    }
    if languages:
        merged_result["language"] = languages[0] if len(languages) == 1 else languages
    return utterances, labeled, merged_result, part_records


def _request_voxtral_transcription(
    audio: Path,
    model: str,
    *,
    max_attempts: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv()
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise RuntimeError("MISTRAL_API_KEY is required for Voxtral transcription")

    timeout = httpx.Timeout(connect=30.0, read=1_800.0, write=1_800.0, pool=30.0)
    started = time.monotonic()
    response: httpx.Response | None = None
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with audio.open("rb") as file_handle:
                response = httpx.post(
                    VOXTRAL_API_URL,
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    data={
                        "model": model,
                        "diarize": "true",
                        "timestamp_granularities": "segment",
                    },
                    files={"file": (audio.name, file_handle, _mime_type(audio))},
                    timeout=timeout,
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_error = exc
            if attempt >= max_attempts:
                raise
            time.sleep(min(60, 5 * 2 ** (attempt - 1)))
            continue
        if response.status_code < 400:
            wall_clock = time.monotonic() - started
            return response.json(), {
                "wall_clock_sec": round_sec(wall_clock),
                "http_status": response.status_code,
                "attempts": attempt,
            }
        if response.status_code not in {408, 409, 425, 429} and response.status_code < 500:
            break
        if attempt >= max_attempts:
            break
        time.sleep(min(60, 5 * 2 ** (attempt - 1)))

    wall_clock = time.monotonic() - started
    if response is not None:
        raise RuntimeError(
            f"Voxtral failed with HTTP {response.status_code} after {round_sec(wall_clock)}s "
            f"and {max_attempts} attempts: {response.text[:2000]}"
        )
    raise RuntimeError(f"Voxtral failed after {round_sec(wall_clock)}s: {last_error}")


def _voxtral_result_to_rows(
    result: dict[str, Any],
    *,
    offset_sec: float,
    speaker_suffix: str,
    part_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_segments = result.get("segments")
    if not isinstance(raw_segments, list):
        raise RuntimeError(f"Voxtral response lacks segments list: {result.keys()}")

    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        text = clean_text(segment.get("text"))
        if not text:
            continue
        start = float(segment.get("start", 0)) + offset_sec
        end = float(segment.get("end", 0)) + offset_sec
        if end <= start:
            continue
        row = {
            "t0": round_sec(start),
            "t1": round_sec(end),
            "text": text,
        }
        raw_speaker = clean_text(segment.get("speaker_id")) or "UNKNOWN"
        label = f"{raw_speaker}{speaker_suffix}" if speaker_suffix else raw_speaker
        utterances.append(row)
        labeled_row = dict(row)
        labeled_row["label"] = label
        labeled_row["speaker_id"] = raw_speaker
        labeled_row["voxtral_part"] = part_index
        labeled.append(labeled_row)

        merged_segment = dict(segment)
        merged_segment["text"] = text
        merged_segment["start"] = row["t0"]
        merged_segment["end"] = row["t1"]
        merged_segment["speaker_id"] = label
        if label != raw_speaker:
            merged_segment["raw_speaker_id"] = raw_speaker
        merged_segment["part"] = part_index
        merged_segments.append(merged_segment)
    return utterances, labeled, merged_segments


def _add_numeric_usage(total: dict[str, int | float], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        total[key] = total.get(key, 0) + value


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".m4a":
        return "audio/mp4"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".mp4":
        return "video/mp4"
    return "application/octet-stream"


def _require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"required command not found on PATH: {name}")


def _write_generic_transcribe_meta_if_safe(meeting_dir: Path, meta: dict[str, Any]) -> None:
    generic_path = meeting_dir / "transcribe-meta.json"
    if generic_path.exists():
        try:
            existing = read_json(generic_path)
        except Exception:
            return
        if existing.get("backend") != "voxtral":
            return
    write_json(generic_path, meta)


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
