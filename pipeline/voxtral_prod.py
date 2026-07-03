from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import transcribe as transcribe_mod
from .artifacts import clean_text, read_json, round_sec, write_json, write_jsonl
from .models import Meeting
from .utils import load_dotenv

DEFAULT_PROD_CHUNK_SEC = 1_800.0
DEFAULT_INTER_CHUNK_DELAY_SEC = 8.0
DEFAULT_BACKOFF_BASE_SEC = 10.0
DEFAULT_BACKOFF_MAX_SEC = 300.0
DEFAULT_MAX_ATTEMPTS = 7

RequestFunc = Callable[[Path, str, list[str]], tuple[dict[str, Any], dict[str, Any]]]


def transcribe_voxtral_production(
    meeting: Meeting,
    model: str = transcribe_mod.DEFAULT_VOXTRAL_MODEL,
    *,
    request_func: RequestFunc | None = None,
) -> Path:
    """Run Voxtral as the production ASR+diarization backend.

    Production writes the canonical downstream artifacts:
    utterances.jsonl, utterances-labeled.jsonl, and transcribe-meta.json.
    The benchmark/eval path in pipeline.transcribe intentionally keeps the
    utterances-voxtral*.jsonl names for side-by-side comparisons.
    """
    meeting.meeting_dir.mkdir(parents=True, exist_ok=True)
    output = meeting.meeting_dir / "utterances.jsonl"
    labeled_output = meeting.meeting_dir / "utterances-labeled.jsonl"
    meta_path = meeting.meeting_dir / "transcribe-meta.json"
    transcript_path = meeting.meeting_dir / "voxtral-transcript.json"

    if _canonical_voxtral_complete(output, labeled_output, meta_path):
        return output

    audio = transcribe_mod._voxtral_audio_path(meeting.meeting_dir)
    duration = transcribe_mod._meeting_duration(meeting, audio)
    context_bias, context_bias_meta = transcribe_mod._voxtral_context_bias_for_meeting(meeting)
    started = time.monotonic()

    parts, split_meta = _production_parts(meeting.meeting_dir, audio, duration)
    request = request_func or _request_voxtral_transcription_with_backoff
    utterances, labeled, merged_result, part_records = _transcribe_voxtral_parts_resumable(
        meeting.meeting_dir,
        parts,
        model,
        context_bias,
        request,
    )
    wall_clock = time.monotonic() - started
    if not utterances:
        raise RuntimeError("Voxtral returned no timestamped segments")

    labels = sorted({str(row["label"]) for row in labeled if str(row.get("label") or "").strip()})
    split_meta["parts"] = part_records
    usage = merged_result.get("usage", {}) if isinstance(merged_result, dict) else {}
    meta = {
        "backend": "voxtral",
        "profile": "production",
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
        "context_bias": context_bias_meta,
    }
    write_jsonl(output, utterances)
    write_jsonl(labeled_output, labeled)
    write_json(transcript_path, merged_result)
    write_json(meta_path, meta)
    _cleanup_part_audio(meeting.meeting_dir / "voxtral-parts")
    return output


def _canonical_voxtral_complete(output: Path, labeled_output: Path, meta_path: Path) -> bool:
    if not (output.exists() and labeled_output.exists() and meta_path.exists()):
        return False
    if output.stat().st_size <= 0 or labeled_output.stat().st_size <= 0:
        return False
    try:
        meta = read_json(meta_path)
    except Exception:
        return False
    return meta.get("backend") == "voxtral"


def _production_parts(meeting_dir: Path, audio: Path, duration: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chunk_sec = _env_float("VOXTRAL_PROD_CHUNK_SEC", DEFAULT_PROD_CHUNK_SEC)
    if chunk_sec and duration > chunk_sec + 30.0:
        parts_dir = meeting_dir / "voxtral-parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts, split_reason = transcribe_mod._fixed_duration_audio_parts(
            audio,
            duration,
            chunk_sec,
            parts_dir,
            prefix="voxtral",
        )
        return parts, {
            "enabled": True,
            "strategy": "fixed_duration",
            "max_part_sec": round_sec(chunk_sec),
            "split_reason": split_reason,
            "speaker_label_limitation": transcribe_mod.VOXTRAL_LABEL_LIMITATION,
            "resume_partial": True,
            "inter_chunk_delay_sec": _env_float(
                "VOXTRAL_INTER_CHUNK_DELAY_SEC",
                DEFAULT_INTER_CHUNK_DELAY_SEC,
            ),
        }

    return (
        [{"index": 1, "path": audio, "offset_sec": 0.0, "speaker_suffix": ""}],
        {
            "enabled": False,
            "strategy": "single_request",
            "threshold_sec": round_sec(chunk_sec),
            "resume_partial": True,
            "inter_chunk_delay_sec": 0.0,
        },
    )


def _transcribe_voxtral_parts_resumable(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
    context_bias: list[str],
    request_func: RequestFunc,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    merged_text: list[str] = []
    usage_totals: dict[str, int | float] = {}
    part_records: list[dict[str, Any]] = []
    languages: list[str] = []
    fresh_requests = 0
    inter_chunk_delay = _env_float("VOXTRAL_INTER_CHUNK_DELAY_SEC", DEFAULT_INTER_CHUNK_DELAY_SEC)

    for part in parts:
        part_index = int(part["index"])
        raw_path = meeting_dir / f"voxtral-transcript-part-{part_index}.json"
        result: dict[str, Any] | None = None
        request_meta: dict[str, Any]
        if raw_path.exists():
            try:
                result = read_json(raw_path)
                request_meta = {"reused": True, "attempts": 0, "wall_clock_sec": 0.0}
            except Exception:
                raw_path.unlink(missing_ok=True)
                result = None

        if result is None:
            if fresh_requests and inter_chunk_delay > 0:
                time.sleep(inter_chunk_delay)
            result, request_meta = request_func(Path(part["path"]), model, context_bias)
            request_meta["reused"] = False
            write_json(raw_path, result)
            fresh_requests += 1

        offset = float(part.get("offset_sec") or 0)
        speaker_suffix = str(part.get("speaker_suffix") or "")
        part_utterances, part_labeled, part_segments = transcribe_mod._voxtral_result_to_rows(
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
            transcribe_mod._add_numeric_usage(usage_totals, usage)
        language = str(result.get("language") or "").strip()
        if language and language not in languages:
            languages.append(language)
        speakers = sorted({str(row["label"]) for row in part_labeled if str(row.get("label") or "").strip()})
        part_records.append(
            _part_record(
                part,
                raw_path,
                request_meta,
                segment_count=len(part_segments),
                utterance_count=len(part_utterances),
                speaker_count=len(speakers),
            )
        )

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


def _request_voxtral_transcription_with_backoff(
    audio: Path,
    model: str,
    context_bias: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv()
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise RuntimeError("MISTRAL_API_KEY is required for Voxtral transcription")

    max_attempts = int(_env_float("VOXTRAL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    base_delay = _env_float("VOXTRAL_BACKOFF_BASE_SEC", DEFAULT_BACKOFF_BASE_SEC)
    max_delay = _env_float("VOXTRAL_BACKOFF_MAX_SEC", DEFAULT_BACKOFF_MAX_SEC)
    timeout = httpx.Timeout(connect=30.0, read=1_800.0, write=1_800.0, pool=30.0)
    started = time.monotonic()
    response: httpx.Response | None = None
    retry_statuses: list[int | str] = []
    retry_delays: list[float] = []
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            with audio.open("rb") as file_handle:
                response = httpx.post(
                    transcribe_mod.VOXTRAL_API_URL,
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    data=transcribe_mod._voxtral_request_form_data(model, context_bias),
                    files={"file": (audio.name, file_handle, transcribe_mod._mime_type(audio))},
                    timeout=timeout,
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            last_error = exc
            retry_statuses.append(type(exc).__name__)
            if attempt >= max_attempts:
                break
            delay = _retry_delay_seconds(attempt, base_delay, max_delay, None)
            retry_delays.append(round_sec(delay))
            time.sleep(delay)
            continue

        if response.status_code < 400:
            wall_clock = time.monotonic() - started
            return response.json(), {
                "wall_clock_sec": round_sec(wall_clock),
                "http_status": response.status_code,
                "attempts": attempt,
                "retry_statuses": retry_statuses,
                "retry_delays_sec": retry_delays,
                "context_bias_param": transcribe_mod.VOXTRAL_CONTEXT_BIAS_PARAM,
                "context_bias_count": len(context_bias),
            }

        retry_statuses.append(response.status_code)
        if not _retryable_status(response.status_code):
            break
        if attempt >= max_attempts:
            break
        delay = _retry_delay_seconds(attempt, base_delay, max_delay, response)
        retry_delays.append(round_sec(delay))
        time.sleep(delay)

    wall_clock = time.monotonic() - started
    if response is not None:
        raise RuntimeError(
            f"Voxtral failed with HTTP {response.status_code} after {round_sec(wall_clock)}s "
            f"and {max_attempts} attempts: {response.text[:2000]}"
        )
    raise RuntimeError(f"Voxtral failed after {round_sec(wall_clock)}s: {last_error}")


def _retryable_status(status: int) -> bool:
    return status in {408, 409, 425, 429} or status >= 500


def _retry_delay_seconds(
    attempt: int,
    base_delay: float,
    max_delay: float,
    response: httpx.Response | None,
) -> float:
    retry_after = _retry_after_seconds(response)
    exponential = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    return max(exponential, retry_after) if retry_after is not None else exponential


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _part_record(
    part: dict[str, Any],
    raw_path: Path,
    request_meta: dict[str, Any],
    *,
    segment_count: int,
    utterance_count: int,
    speaker_count: int,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "index": int(part["index"]),
        "offset_sec": round_sec(float(part.get("offset_sec") or 0)),
        "speaker_suffix": str(part.get("speaker_suffix") or ""),
        "request_wall_clock_sec": request_meta.get("wall_clock_sec", 0.0),
        "attempts": request_meta.get("attempts", 1),
        "http_status": request_meta.get("http_status"),
        "reused_partial": bool(request_meta.get("reused")),
        "retry_statuses": request_meta.get("retry_statuses", []),
        "retry_delays_sec": request_meta.get("retry_delays_sec", []),
        "segment_count": segment_count,
        "utterance_count": utterance_count,
        "speaker_count": speaker_count,
        "raw_transcript_output": str(raw_path),
    }
    if part.get("source_audio_file"):
        record["source_audio_file"] = str(part["source_audio_file"])
    if part.get("temporary_audio"):
        record["temporary_audio_name"] = Path(part["path"]).name
    else:
        record["audio_file"] = str(part["path"])
    if "split_start_sec" in part:
        record["split_start_sec"] = round_sec(float(part["split_start_sec"]))
    if "split_end_sec" in part:
        record["split_end_sec"] = round_sec(float(part["split_end_sec"]))
    return {key: value for key, value in record.items() if value is not None}


def _cleanup_part_audio(parts_dir: Path) -> None:
    if os.environ.get("VOXTRAL_KEEP_PART_AUDIO", "").strip().lower() in {"1", "true", "yes"}:
        return
    if parts_dir.exists():
        shutil.rmtree(parts_dir)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default
