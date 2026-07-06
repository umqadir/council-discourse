from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import transcribe as transcribe_mod
from .artifacts import clean_text, read_json, round_sec, write_json, write_jsonl
from .config import voxtral_mode, voxtral_usd_per_audio_hour
from .models import Meeting
from .utils import load_dotenv, utc_now_iso

DEFAULT_PROD_CHUNK_SEC = 1_800.0
DEFAULT_INTER_CHUNK_DELAY_SEC = 8.0
DEFAULT_BACKOFF_BASE_SEC = 10.0
DEFAULT_BACKOFF_MAX_SEC = 300.0
DEFAULT_MAX_ATTEMPTS = 7
DEFAULT_BATCH_POLL_BUDGET_SEC = 4_500.0
DEFAULT_BATCH_POLL_INTERVAL_SEC = 30.0
MISTRAL_API_BASE_URL = "https://api.mistral.ai/v1"
MISTRAL_FILES_URL = f"{MISTRAL_API_BASE_URL}/files"
MISTRAL_BATCH_JOBS_URL = f"{MISTRAL_API_BASE_URL}/batch/jobs"
VOXTRAL_BATCH_ENDPOINT = "/v1/audio/transcriptions"
VOXTRAL_BATCH_JOB_FILENAME = "voxtral-batch-job.json"

RequestFunc = Callable[[Path, str, list[str]], tuple[dict[str, Any], dict[str, Any]]]


class VoxtralBatchPending(RuntimeError):
    def __init__(self, job_id: str, status: str, budget_sec: float) -> None:
        self.job_id = job_id
        self.status = status
        self.budget_sec = budget_sec
        super().__init__(
            f"Voxtral batch job {job_id} is still {status} after {round_sec(budget_sec)}s poll budget"
        )


class _BatchJobNotFound(RuntimeError):
    pass


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
    mode = "sync" if request_func is not None else voxtral_mode()
    if mode == "sync":
        request = request_func or _request_voxtral_transcription_with_backoff
        utterances, labeled, merged_result, part_records = _transcribe_voxtral_parts_resumable(
            meeting.meeting_dir,
            parts,
            model,
            context_bias,
            request,
        )
    else:
        utterances, labeled, merged_result, part_records = _transcribe_voxtral_parts_batch(
            meeting.meeting_dir,
            parts,
            model,
            context_bias,
            meeting_key=meeting.meeting_key,
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
        "mode": mode,
        "engine": "mistral-audio-transcriptions",
        "model": model,
        "pricing_usd_per_audio_hour": voxtral_usd_per_audio_hour(mode),
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
        result = _read_valid_voxtral_part(raw_path)
        if result is not None:
            request_meta = {"reused": True, "attempts": 0, "wall_clock_sec": 0.0}
        elif raw_path.exists():
            raw_path.unlink(missing_ok=True)

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


def _transcribe_voxtral_parts_batch(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
    context_bias: list[str],
    *,
    meeting_key: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    missing_parts = _missing_voxtral_parts(meeting_dir, parts)
    if missing_parts:
        _run_voxtral_batch_job(
            meeting_dir,
            missing_parts,
            model,
            context_bias,
            meeting_key=meeting_key,
        )
    still_missing = _missing_voxtral_parts(meeting_dir, parts)
    if still_missing:
        indexes = ", ".join(str(int(part["index"])) for part in still_missing)
        raise RuntimeError(
            "Voxtral batch completed without usable transcripts for "
            f"part(s) {indexes}; completed parts were preserved for retry"
        )

    def no_sync_request(_audio: Path, _model: str, _bias: list[str]):
        raise RuntimeError("internal error: batch merge attempted a sync Voxtral request")

    return _transcribe_voxtral_parts_resumable(
        meeting_dir,
        parts,
        model,
        context_bias,
        no_sync_request,
    )


def _missing_voxtral_parts(meeting_dir: Path, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing = []
    for part in parts:
        raw_path = meeting_dir / f"voxtral-transcript-part-{int(part['index'])}.json"
        if _read_valid_voxtral_part(raw_path) is None:
            missing.append(part)
    return missing


def _read_valid_voxtral_part(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return None
        result = read_json(path)
    except Exception:
        return None
    return result if isinstance(result.get("segments"), list) else None


def _run_voxtral_batch_job(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
    context_bias: list[str],
    *,
    meeting_key: str,
) -> None:
    load_dotenv()
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise RuntimeError("MISTRAL_API_KEY is required for Voxtral batch transcription")

    params_hash = _batch_request_params_hash(parts, model, context_bias)
    state_path = meeting_dir / VOXTRAL_BATCH_JOB_FILENAME
    state, job = _reattach_voxtral_batch_job(state_path, params_hash, key)
    if state is None:
        state, job = _create_voxtral_batch_job(
            meeting_dir,
            parts,
            model,
            context_bias,
            meeting_key=meeting_key,
            params_hash=params_hash,
            key=key,
            state_path=state_path,
        )

    completed_job = _poll_voxtral_batch_job(state, job, key)
    _write_completed_batch_parts(meeting_dir, parts, completed_job, key)


def _reattach_voxtral_batch_job(
    state_path: Path,
    params_hash: str,
    key: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not state_path.exists():
        return None, None
    try:
        state = read_json(state_path)
    except Exception:
        state_path.unlink(missing_ok=True)
        return None, None
    if state.get("request_params_hash") != params_hash:
        state_path.unlink(missing_ok=True)
        return None, None
    job_id = str(state.get("job_id") or "").strip()
    if not job_id:
        state_path.unlink(missing_ok=True)
        return None, None
    try:
        return state, _get_batch_job(job_id, key)
    except _BatchJobNotFound:
        state_path.unlink(missing_ok=True)
        return None, None


def _create_voxtral_batch_job(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
    context_bias: list[str],
    *,
    meeting_key: str,
    params_hash: str,
    key: str,
    state_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    audio_file_ids: dict[str, str] = {}
    for part in parts:
        custom_id = str(int(part["index"]))
        audio_file_ids[custom_id] = _upload_mistral_file(
            Path(part["path"]),
            key,
            purpose="batch",
            mimetype=transcribe_mod._mime_type(Path(part["path"])),
        )

    input_bytes = _batch_input_jsonl(parts, context_bias, audio_file_ids)
    batch_input_file_id = _upload_mistral_file_bytes(
        f"{meeting_key}-voxtral-batch.jsonl",
        input_bytes,
        key,
        purpose="batch",
        mimetype="application/jsonl",
    )
    create_payload = {
        "input_files": [batch_input_file_id],
        "endpoint": VOXTRAL_BATCH_ENDPOINT,
        "model": model,
        "metadata": {
            "meeting_key": meeting_key,
            "stage": "voxtral_transcription",
        },
    }
    job = _post_json(MISTRAL_BATCH_JOBS_URL, create_payload, key)
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise RuntimeError(f"Mistral batch job create response lacks id: {job}")

    chunk_map: dict[str, dict[str, Any]] = {}
    for part in parts:
        custom_id = str(int(part["index"]))
        chunk_map[custom_id] = {
            "part_index": int(part["index"]),
            "audio_file_id": audio_file_ids[custom_id],
            "raw_transcript_output": str(meeting_dir / f"voxtral-transcript-part-{int(part['index'])}.json"),
            "offset_sec": round_sec(float(part.get("offset_sec") or 0)),
            "speaker_suffix": str(part.get("speaker_suffix") or ""),
        }

    state = {
        "job_id": job_id,
        "input_file_ids": [batch_input_file_id],
        "submitted_at": utc_now_iso(),
        "endpoint": VOXTRAL_BATCH_ENDPOINT,
        "model": model,
        "request_params_hash": params_hash,
        "chunk_custom_id_map": chunk_map,
        "job": _batch_job_summary(job),
    }
    write_json(state_path, state)
    return state, job


def _batch_input_jsonl(
    parts: list[dict[str, Any]],
    context_bias: list[str],
    audio_file_ids: dict[str, str],
) -> bytes:
    lines = []
    params = _voxtral_batch_body_params(context_bias)
    for part in parts:
        custom_id = str(int(part["index"]))
        body = dict(params)
        body["file_id"] = audio_file_ids[custom_id]
        lines.append(json.dumps({"custom_id": custom_id, "body": body}, sort_keys=True))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _voxtral_batch_body_params(context_bias: list[str]) -> dict[str, Any]:
    form = transcribe_mod._voxtral_request_form_data("_", context_bias)
    params: dict[str, Any] = {
        "diarize": str(form.get("diarize", "")).lower() == "true",
        "timestamp_granularities": list(form.get("timestamp_granularities") or ["segment"]),
    }
    bias = str(form.get(transcribe_mod.VOXTRAL_CONTEXT_BIAS_PARAM) or "").strip()
    if bias:
        params[transcribe_mod.VOXTRAL_CONTEXT_BIAS_PARAM] = [item for item in bias.split(",") if item]
    return params


def _batch_request_params_hash(
    parts: list[dict[str, Any]],
    model: str,
    context_bias: list[str],
) -> str:
    payload = {
        "endpoint": VOXTRAL_BATCH_ENDPOINT,
        "model": model,
        "request_body": _voxtral_batch_body_params(context_bias),
        "parts": [_batch_part_hash_record(part) for part in parts],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _batch_part_hash_record(part: dict[str, Any]) -> dict[str, Any]:
    path = Path(part["path"])
    record: dict[str, Any] = {
        "index": int(part["index"]),
        "path_name": path.name,
        "offset_sec": round_sec(float(part.get("offset_sec") or 0)),
        "speaker_suffix": str(part.get("speaker_suffix") or ""),
    }
    try:
        record["size_bytes"] = path.stat().st_size
    except OSError:
        record["size_bytes"] = None
    if "split_start_sec" in part:
        record["split_start_sec"] = round_sec(float(part["split_start_sec"]))
    if "split_end_sec" in part:
        record["split_end_sec"] = round_sec(float(part["split_end_sec"]))
    return record


def _poll_voxtral_batch_job(
    state: dict[str, Any],
    job: dict[str, Any] | None,
    key: str,
) -> dict[str, Any]:
    job_id = str(state["job_id"])
    budget = _env_float("VOXTRAL_BATCH_POLL_BUDGET_SEC", DEFAULT_BATCH_POLL_BUDGET_SEC)
    interval = _env_float("VOXTRAL_BATCH_POLL_INTERVAL_SEC", DEFAULT_BATCH_POLL_INTERVAL_SEC)
    started = time.monotonic()
    current = job or _get_batch_job(job_id, key)
    while True:
        status = _batch_job_status(current)
        if status == "SUCCESS":
            return current
        if status in {"FAILED", "TIMEOUT_EXCEEDED", "CANCELLED"}:
            raise RuntimeError(f"Voxtral batch job {job_id} ended with status {status}: {current.get('errors')}")
        elapsed = time.monotonic() - started
        if elapsed >= budget:
            raise VoxtralBatchPending(job_id, status or "UNKNOWN", budget)
        delay = min(max(0.0, budget - elapsed), max(1.0, interval))
        time.sleep(delay)
        current = _get_batch_job(job_id, key)


def _write_completed_batch_parts(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    job: dict[str, Any],
    key: str,
) -> None:
    rows = _batch_output_rows(job, key)
    parts_by_id = {str(int(part["index"])): part for part in parts}
    for row in rows:
        custom_id = str(row.get("custom_id") or "").strip()
        if custom_id not in parts_by_id:
            continue
        result = _batch_row_transcription_result(row)
        if result is None:
            continue
        raw_path = meeting_dir / f"voxtral-transcript-part-{int(parts_by_id[custom_id]['index'])}.json"
        write_json(raw_path, result)


def _batch_output_rows(job: dict[str, Any], key: str) -> list[dict[str, Any]]:
    inline = job.get("outputs")
    if isinstance(inline, list):
        return [row for row in inline if isinstance(row, dict)]
    output_file = str(job.get("output_file") or "").strip()
    if not output_file:
        return []
    content = _download_mistral_file(output_file, key).decode("utf-8")
    rows = []
    for line in content.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _batch_row_transcription_result(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("error"):
        return None
    response = row.get("response")
    if isinstance(response, dict):
        status = response.get("status_code", response.get("status"))
        if status is not None and int(status) >= 400:
            return None
        body = response.get("body") or response.get("data") or response.get("json")
        return body if isinstance(body, dict) else None
    body = row.get("body") or row.get("output")
    if isinstance(body, dict):
        return body
    if isinstance(row.get("segments"), list):
        return row
    return None


def _upload_mistral_file(path: Path, key: str, *, purpose: str, mimetype: str) -> str:
    with path.open("rb") as file_handle:
        response = httpx.post(
            MISTRAL_FILES_URL,
            headers=_mistral_headers(key),
            data={"purpose": purpose},
            files={"file": (path.name, file_handle, mimetype)},
            timeout=httpx.Timeout(connect=30.0, read=1_800.0, write=1_800.0, pool=30.0),
        )
    return _file_id_from_response(response)


def _upload_mistral_file_bytes(
    filename: str,
    content: bytes,
    key: str,
    *,
    purpose: str,
    mimetype: str,
) -> str:
    response = httpx.post(
        MISTRAL_FILES_URL,
        headers=_mistral_headers(key),
        data={"purpose": purpose},
        files={"file": (filename, content, mimetype)},
        timeout=httpx.Timeout(connect=30.0, read=1_800.0, write=1_800.0, pool=30.0),
    )
    return _file_id_from_response(response)


def _file_id_from_response(response: httpx.Response) -> str:
    _raise_for_mistral_response(response, "Mistral file upload")
    payload = response.json()
    file_id = str(payload.get("id") or "").strip()
    if not file_id:
        raise RuntimeError(f"Mistral file upload response lacks id: {payload}")
    return file_id


def _post_json(url: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    response = httpx.post(
        url,
        headers={**_mistral_headers(key), "Content-Type": "application/json"},
        json=payload,
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0),
    )
    _raise_for_mistral_response(response, "Mistral JSON POST")
    return response.json()


def _get_batch_job(job_id: str, key: str) -> dict[str, Any]:
    response = httpx.get(
        f"{MISTRAL_BATCH_JOBS_URL}/{job_id}",
        headers=_mistral_headers(key),
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0),
    )
    if response.status_code == 404:
        raise _BatchJobNotFound(job_id)
    _raise_for_mistral_response(response, "Mistral batch job retrieve")
    return response.json()


def _download_mistral_file(file_id: str, key: str) -> bytes:
    response = httpx.get(
        f"{MISTRAL_FILES_URL}/{file_id}/content",
        headers=_mistral_headers(key),
        timeout=httpx.Timeout(connect=30.0, read=1_800.0, write=300.0, pool=30.0),
    )
    _raise_for_mistral_response(response, "Mistral file download")
    return response.content


def _mistral_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _raise_for_mistral_response(response: httpx.Response, context: str) -> None:
    if response.status_code < 400:
        return
    raise RuntimeError(f"{context} failed with HTTP {response.status_code}: {response.text[:2000]}")


def _batch_job_status(job: dict[str, Any]) -> str:
    return str(job.get("status") or "").strip().upper()


def _batch_job_summary(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "status",
        "created_at",
        "started_at",
        "completed_at",
        "total_requests",
        "completed_requests",
        "succeeded_requests",
        "failed_requests",
        "output_file",
        "error_file",
    )
    return {key: job.get(key) for key in keys if key in job}


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
