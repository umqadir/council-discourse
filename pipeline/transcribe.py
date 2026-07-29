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
from .roster import current_roster
from .utils import load_dotenv

DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
DEFAULT_VOXTRAL_MODEL = "voxtral-mini-2602"
DEFAULT_SCRIBE_MODEL = "scribe_v2"
DEFAULT_ASSEMBLYAI_MODEL = "universal-3-pro"
VOXTRAL_API_URL = "https://api.mistral.ai/v1/audio/transcriptions"
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com/v2"
VOXTRAL_CONTEXT_BIAS_PARAM = "context_bias"
VOXTRAL_CONTEXT_BIAS_LIMIT = 100
VOXTRAL_CONTEXT_BIAS_ENV = "VOXTRAL_CONTEXT_BIAS"
VOXTRAL_SPLIT_THRESHOLD_SEC = 10_800.0
SCRIBE_SPLIT_THRESHOLD_SEC = 10_800.0
ASSEMBLYAI_SPLIT_THRESHOLD_SEC = 0.0
VOXTRAL_SILENCE_WINDOW_SEC = 1_800.0
REMOTE_SILENCE_WINDOW_SEC = 1_800.0
SCRIBE_ESTIMATED_USD_PER_HOUR = 0.40
ASSEMBLYAI_ESTIMATED_USD_PER_HOUR = 0.37
ASR_VENDOR_MAX_COST_USD = 5.0
ASSEMBLYAI_POLL_INTERVAL_SEC = 10.0
ASSEMBLYAI_POLL_TIMEOUT_SEC = 21_600.0
VOXTRAL_LABEL_LIMITATION = (
    "Voxtral diarization labels are request-local. For split audio, labels from part 2 "
    "and later are suffixed with _partN so the speaker-naming stage does not merge "
    "unrelated speakers across API requests."
)
REQUEST_LOCAL_LABEL_LIMITATION = (
    "Diarization labels are request-local. For split audio, labels from part 2 and later "
    "are suffixed with _partN so the speaker-naming stage does not merge unrelated "
    "speakers across API requests."
)
COMMON_NYC_AGENCY_BIAS_TERMS = (
    "NYCHA",
    "New York City Housing Authority",
    "DCWP",
    "Department of Consumer and Worker Protection",
    "DOT",
    "Department of Transportation",
    "SBS",
    "Department of Small Business Services",
    "DCP",
    "Department of City Planning",
    "HPD",
    "Department of Housing Preservation and Development",
    "NYPD",
    "FDNY",
    "DSNY",
    "Department of Sanitation",
    "DOB",
    "Department of Buildings",
    "DEP",
    "Department of Environmental Protection",
    "DOHMH",
    "Department of Health and Mental Hygiene",
    "HRA",
    "Human Resources Administration",
    "ACS",
    "Administration for Children's Services",
    "TLC",
    "Taxi and Limousine Commission",
    "MTA",
    "Metropolitan Transportation Authority",
)
COMMITTEE_AGENCY_BIAS_TERMS = (
    (
        ("transportation", "infrastructure"),
        (
            "DOT",
            "NYC DOT",
            "Department of Transportation",
            "MTA",
            "Metropolitan Transportation Authority",
            "TLC",
            "Taxi and Limousine Commission",
        ),
    ),
    (
        ("consumer", "worker protection"),
        (
            "DCWP",
            "Department of Consumer and Worker Protection",
            "SBS",
            "Department of Small Business Services",
            "Dining Out NYC",
            "revocable consent",
        ),
    ),
    (
        ("housing", "buildings"),
        (
            "NYCHA",
            "New York City Housing Authority",
            "HPD",
            "Department of Housing Preservation and Development",
            "DOB",
            "Department of Buildings",
        ),
    ),
    (
        ("land use", "zoning", "franchises", "planning"),
        (
            "DCP",
            "Department of City Planning",
            "CPC",
            "City Planning Commission",
            "ULURP",
            "Board of Standards and Appeals",
        ),
    ),
    (
        ("finance", "budget"),
        (
            "OMB",
            "Office of Management and Budget",
            "IBO",
            "Independent Budget Office",
            "DOF",
            "Department of Finance",
        ),
    ),
    (
        ("health", "mental hygiene", "hospitals"),
        (
            "DOHMH",
            "Department of Health and Mental Hygiene",
            "H+H",
            "NYC Health and Hospitals",
        ),
    ),
)


def transcribe_meeting(
    meeting: Meeting,
    backend: str = "voxtral",
    model: str | None = None,
) -> Path:
    if backend == "remote":
        raise NotImplementedError(
            "remote transcription backend is not implemented yet. The provider interface is "
            "reserved for the remote ASR backend selection."
        )
    if backend in {"voxtral", "mistral-voxtral"}:
        return transcribe_voxtral(meeting, model=model or DEFAULT_VOXTRAL_MODEL)
    if backend in {"scribe", "elevenlabs", "elevenlabs-scribe"}:
        return transcribe_scribe(meeting, model=model or DEFAULT_SCRIBE_MODEL)
    if backend in {"assemblyai", "assembly-ai"}:
        return transcribe_assemblyai(meeting, model=model or DEFAULT_ASSEMBLYAI_MODEL)
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
    context_bias, context_bias_meta = _voxtral_context_bias_for_meeting(meeting)
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
                context_bias,
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
            context_bias,
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
        "context_bias": context_bias_meta,
    }
    write_json(meta_path, meta)
    _write_generic_transcribe_meta_if_safe(meeting.meeting_dir, meta)
    return output


def transcribe_scribe(meeting: Meeting, model: str = DEFAULT_SCRIBE_MODEL) -> Path:
    audio = _remote_audio_path(meeting.meeting_dir)
    output = meeting.meeting_dir / "utterances-scribe.jsonl"
    labeled_output = meeting.meeting_dir / "utterances-scribe-labeled.jsonl"
    meta_path = meeting.meeting_dir / "transcribe-scribe-meta.json"
    transcript_path = meeting.meeting_dir / "scribe-transcript.json"
    meeting.meeting_dir.mkdir(parents=True, exist_ok=True)

    duration = _meeting_duration(meeting, audio)
    cost_meta = _vendor_cost_meta(
        duration,
        rate_env="SCRIBE_ESTIMATED_USD_PER_HOUR",
        default_rate=SCRIBE_ESTIMATED_USD_PER_HOUR,
    )
    _enforce_vendor_budget("ElevenLabs Scribe", cost_meta)

    split_threshold = _env_float("SCRIBE_SPLIT_THRESHOLD_SEC", SCRIBE_SPLIT_THRESHOLD_SEC)
    started = time.monotonic()
    if split_threshold and duration > split_threshold:
        split_sec, split_reason = _find_silence_split(audio, duration, window_sec=REMOTE_SILENCE_WINDOW_SEC)
        with tempfile.TemporaryDirectory(prefix=".scribe-", dir=meeting.meeting_dir) as tmp_name:
            part_paths = _split_audio(audio, split_sec, Path(tmp_name), prefix="scribe")
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
            utterances, labeled, merged_result, part_records = _transcribe_scribe_parts(
                meeting.meeting_dir,
                parts,
                model,
            )
        split_meta = {
            "enabled": True,
            "threshold_sec": split_threshold,
            "split_sec": round_sec(split_sec),
            "split_reason": split_reason,
            "parts": part_records,
            "speaker_label_limitation": REQUEST_LOCAL_LABEL_LIMITATION,
        }
    else:
        parts = [{"index": 1, "path": audio, "offset_sec": 0.0, "speaker_suffix": ""}]
        utterances, labeled, merged_result, part_records = _transcribe_scribe_parts(
            meeting.meeting_dir,
            parts,
            model,
        )
        split_meta = {
            "enabled": False,
            "threshold_sec": split_threshold,
            "parts": part_records,
        }

    wall_clock = time.monotonic() - started
    if not utterances:
        raise RuntimeError("ElevenLabs Scribe returned no timestamped utterances")

    labels = sorted({str(row["label"]) for row in labeled if str(row.get("label") or "").strip()})
    write_jsonl(output, utterances)
    write_jsonl(labeled_output, labeled)
    write_json(transcript_path, merged_result)
    meta = {
        "backend": "scribe",
        "engine": "elevenlabs-speech-to-text",
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
        "usage": {
            "prompt_audio_seconds": round_sec(duration),
            "audio_seconds": round_sec(duration),
            "request_count": len(part_records),
        },
        "estimated_cost_usd": cost_meta["estimated_cost_usd"],
        "cost_estimate": cost_meta,
        "split": split_meta,
        "schema": merged_result.get("schema", {}),
    }
    write_json(meta_path, meta)
    return output


def transcribe_assemblyai(meeting: Meeting, model: str = DEFAULT_ASSEMBLYAI_MODEL) -> Path:
    load_dotenv()
    model = _assemblyai_model_from_env(model)
    speech_models = _assemblyai_speech_models(model)
    audio = _remote_audio_path(meeting.meeting_dir)
    output = meeting.meeting_dir / "utterances-assemblyai.jsonl"
    labeled_output = meeting.meeting_dir / "utterances-assemblyai-labeled.jsonl"
    meta_path = meeting.meeting_dir / "transcribe-assemblyai-meta.json"
    transcript_path = meeting.meeting_dir / "assemblyai-transcript.json"
    meeting.meeting_dir.mkdir(parents=True, exist_ok=True)

    duration = _meeting_duration(meeting, audio)
    cost_meta = _vendor_cost_meta(
        duration,
        rate_env="ASSEMBLYAI_ESTIMATED_USD_PER_HOUR",
        default_rate=ASSEMBLYAI_ESTIMATED_USD_PER_HOUR,
    )
    _enforce_vendor_budget("AssemblyAI", cost_meta)

    split_threshold = _env_float("ASSEMBLYAI_SPLIT_THRESHOLD_SEC", ASSEMBLYAI_SPLIT_THRESHOLD_SEC)
    started = time.monotonic()
    if split_threshold and duration > split_threshold:
        with tempfile.TemporaryDirectory(prefix=".assemblyai-", dir=meeting.meeting_dir) as tmp_name:
            if duration > split_threshold * 2:
                parts, split_reason = _fixed_duration_audio_parts(
                    audio,
                    duration,
                    split_threshold,
                    Path(tmp_name),
                    prefix="assemblyai",
                )
                split_sec = None
            else:
                split_sec, split_reason = _find_silence_split(audio, duration, window_sec=REMOTE_SILENCE_WINDOW_SEC)
                part_paths = _split_audio(audio, split_sec, Path(tmp_name), prefix="assemblyai")
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
            utterances, labeled, merged_result, part_records = _transcribe_assemblyai_parts(
                meeting.meeting_dir,
                parts,
                model,
            )
        split_meta = {
            "enabled": True,
            "threshold_sec": split_threshold,
            "split_reason": split_reason,
            "parts": part_records,
            "speaker_label_limitation": REQUEST_LOCAL_LABEL_LIMITATION,
        }
        if split_sec is not None:
            split_meta["split_sec"] = round_sec(split_sec)
    else:
        parts = [{"index": 1, "path": audio, "offset_sec": 0.0, "speaker_suffix": ""}]
        utterances, labeled, merged_result, part_records = _transcribe_assemblyai_parts(
            meeting.meeting_dir,
            parts,
            model,
        )
        split_meta = {
            "enabled": False,
            "threshold_sec": split_threshold,
            "parts": part_records,
        }

    wall_clock = time.monotonic() - started
    if not utterances:
        raise RuntimeError("AssemblyAI returned no timestamped utterances")

    labels = sorted({str(row["label"]) for row in labeled if str(row.get("label") or "").strip()})
    write_jsonl(output, utterances)
    write_jsonl(labeled_output, labeled)
    write_json(transcript_path, merged_result)
    meta = {
        "backend": "assemblyai",
        "engine": "assemblyai-transcript",
        "model": model,
        "speech_models": speech_models,
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
        "usage": {
            "prompt_audio_seconds": round_sec(duration),
            "audio_seconds": round_sec(duration),
            "request_count": len(part_records),
        },
        "estimated_cost_usd": cost_meta["estimated_cost_usd"],
        "cost_estimate": cost_meta,
        "split": split_meta,
        "schema": merged_result.get("schema", {}),
    }
    write_json(meta_path, meta)
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


def _remote_audio_path(meeting_dir: Path) -> Path:
    for name in ("audio.m4a", "audio-16k.wav", "audio.wav"):
        path = meeting_dir / name
        if path.exists() and path.stat().st_size > 10_000:
            return path
    raise RuntimeError(f"missing prepared audio in {meeting_dir}: expected audio.m4a")


def _voxtral_audio_path(meeting_dir: Path) -> Path:
    return _remote_audio_path(meeting_dir)


def _meeting_duration(meeting: Meeting, audio: Path) -> float:
    if meeting.duration_seconds and float(meeting.duration_seconds) > 0:
        return float(meeting.duration_seconds)
    return _audio_duration(audio)


def _voxtral_context_bias_for_meeting(meeting: Meeting) -> tuple[list[str], dict[str, Any]]:
    load_dotenv()
    enabled = _env_flag_enabled(VOXTRAL_CONTEXT_BIAS_ENV, default=True)
    meta: dict[str, Any] = {
        "enabled": enabled,
        "param": VOXTRAL_CONTEXT_BIAS_PARAM,
        "limit": VOXTRAL_CONTEXT_BIAS_LIMIT,
        "param_source": "mistralai 2.5.1 AudioTranscriptionRequest.context_bias",
        "serialization": "comma_separated",
        "term_format": "hyphen_joined_no_whitespace",
    }
    if not enabled:
        meta["terms"] = []
        meta["count"] = 0
        return [], meta

    roster_records: list[tuple[str, str]] = []
    roster_variant_records: list[tuple[str, str]] = []
    committee_records: list[tuple[str, str]] = []
    agency_records: list[tuple[str, str]] = []
    warnings: list[str] = []
    try:
        for row in current_roster(meeting.event_date):
            variants = _name_bias_variants(str(row.get("name") or ""))
            if not variants:
                continue
            roster_records.append((variants[0], "roster"))
            for term in variants[1:]:
                roster_variant_records.append((term, "roster_variant"))
    except Exception as exc:
        warnings.append(f"roster_unavailable:{type(exc).__name__}")

    for term in _committee_bias_terms(meeting.body_name):
        committee_records.append((term, "committee"))
    for term in _agency_bias_terms(meeting.body_name):
        agency_records.append((term, "agency"))

    terms: list[str] = []
    source_counts: dict[str, int] = {}
    seen: set[str] = set()
    records = roster_records + committee_records + agency_records + roster_variant_records
    for term, source in records:
        cleaned = _context_bias_api_item(term)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(cleaned)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(terms) >= VOXTRAL_CONTEXT_BIAS_LIMIT:
            break

    meta["count"] = len(terms)
    meta["source_counts"] = source_counts
    meta["terms"] = terms
    if warnings:
        meta["warnings"] = warnings
    return terms, meta


def _env_flag_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _name_bias_variants(name: str) -> list[str]:
    cleaned = _clean_context_bias_term(name)
    if not cleaned:
        return []
    variants = [cleaned]
    no_periods = _clean_context_bias_term(cleaned.replace(".", ""))
    if no_periods:
        variants.append(no_periods)
    no_initials = _clean_context_bias_term(re.sub(r"\s+[A-Z]\.(?=\s)", " ", cleaned))
    if no_initials:
        variants.append(no_initials)
    no_suffix = _clean_context_bias_term(re.sub(r",?\s+Jr\.?$", "", no_initials or cleaned, flags=re.I))
    if no_suffix:
        variants.append(no_suffix)
    return _ordered_unique_text(variants)


def _committee_bias_terms(body_name: str | None) -> list[str]:
    body = _clean_context_bias_term(body_name or "")
    if not body:
        return []
    terms = [body]
    base = _clean_context_bias_term(re.sub(r"\s*\([^)]*\)", "", body))
    if base:
        terms.append(base)
    for match in re.finditer(r"\b((?:sub)?committee\s+on\s+[^()]+)", body, flags=re.I):
        committee = _clean_context_bias_term(match.group(1))
        if committee:
            terms.append(committee)
            terms.append(_clean_context_bias_term(re.sub(r"^(?:sub)?committee\s+on\s+", "", committee, flags=re.I)))
    for parenthetical in re.findall(r"\(([^)]*)\)", body):
        value = _clean_context_bias_term(
            re.sub(r"^(?:jointly?\s+with|joint\s+with)\s+", "", parenthetical, flags=re.I)
        )
        if not value:
            continue
        if re.match(r"^(?:sub)?committee\s+on\s+", value, flags=re.I):
            terms.append(value)
        else:
            terms.append(f"Committee on {value}")
            terms.append(value)
    if "stated meeting" in body.lower():
        terms.extend(["City Council Stated Meeting", "Stated Meeting"])
    terms.extend(["New York City Council", "City Council"])
    return _ordered_unique_text(term for term in terms if term)


def _agency_bias_terms(body_name: str | None) -> list[str]:
    text = (body_name or "").lower()
    terms: list[str] = []
    for keywords, agency_terms in COMMITTEE_AGENCY_BIAS_TERMS:
        if any(keyword in text for keyword in keywords):
            terms.extend(agency_terms)
    terms.extend(COMMON_NYC_AGENCY_BIAS_TERMS)
    return _ordered_unique_text(terms)


def _clean_context_bias_term(value: str) -> str:
    return " ".join(str(value).replace("\n", " ").split()).strip(" ,;:")


def _context_bias_api_item(value: str) -> str:
    cleaned = _clean_context_bias_term(value).replace(",", "")
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-;:")
    return cleaned if cleaned and not re.search(r"[\s,]", cleaned) else ""


def _ordered_unique_text(values) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = _clean_context_bias_term(str(value))
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _voxtral_request_form_data(model: str, context_bias: list[str]) -> dict[str, str | list[str]]:
    data: dict[str, str | list[str]] = {
        "model": model,
        "diarize": "true",
        "timestamp_granularities": ["segment"],
    }
    bias_terms = []
    for term in context_bias[:VOXTRAL_CONTEXT_BIAS_LIMIT]:
        cleaned = _context_bias_api_item(term)
        if cleaned:
            bias_terms.append(cleaned)
    if bias_terms:
        data[VOXTRAL_CONTEXT_BIAS_PARAM] = ",".join(bias_terms)
    return data


def _find_silence_split(
    audio: Path,
    duration: float,
    *,
    window_sec: float = VOXTRAL_SILENCE_WINDOW_SEC,
) -> tuple[float, str]:
    _require_command("ffmpeg")
    target = duration / 2
    window = min(window_sec, max(120.0, duration - 120.0))
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


def _split_audio(audio: Path, split_sec: float, tmp_dir: Path, *, prefix: str = "voxtral") -> tuple[Path, Path]:
    _require_command("ffmpeg")
    part1 = tmp_dir / f"{prefix}-part-1.m4a"
    part2 = tmp_dir / f"{prefix}-part-2.m4a"
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


def _fixed_duration_audio_parts(
    audio: Path,
    duration: float,
    max_part_sec: float,
    tmp_dir: Path,
    *,
    prefix: str,
) -> tuple[list[dict[str, Any]], str]:
    _require_command("ffmpeg")
    if max_part_sec <= 0:
        raise ValueError("max_part_sec must be positive")
    parts: list[dict[str, Any]] = []
    start = 0.0
    index = 1
    common = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    encode_args = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k"]
    while start < duration - 0.5:
        part_duration = min(max_part_sec, duration - start)
        path = tmp_dir / f"{prefix}-part-{index}.m4a"
        cmd = common + ["-ss", f"{start:.3f}", "-i", str(audio), "-t", f"{part_duration:.3f}"]
        _run_ffmpeg(cmd + encode_args + [str(path)])
        part: dict[str, Any] = {
            "index": index,
            "path": path,
            "source_audio_file": str(audio),
            "temporary_audio": True,
            "offset_sec": start,
            "speaker_suffix": "" if index == 1 else f"_part{index}",
            "split_start_sec": start,
            "split_end_sec": min(duration, start + part_duration),
        }
        if index == 1:
            part.pop("split_start_sec")
        parts.append(part)
        start += part_duration
        index += 1
    return parts, f"fixed_duration_chunks_{round_sec(max_part_sec)}s"


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with code {proc.returncode}: {proc.stderr.strip()[-2000:]}")


def _transcribe_voxtral_parts(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
    context_bias: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    merged_text: list[str] = []
    usage_totals: dict[str, int | float] = {}
    part_records: list[dict[str, Any]] = []
    languages: list[str] = []

    for part in parts:
        result, request_meta = _request_voxtral_transcription(
            Path(part["path"]),
            model,
            context_bias=context_bias,
        )
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
    context_bias: list[str] | None = None,
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
                    data=_voxtral_request_form_data(model, context_bias or []),
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
                "context_bias_param": VOXTRAL_CONTEXT_BIAS_PARAM,
                "context_bias_count": len(context_bias or []),
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


def _transcribe_scribe_parts(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    merged_words: list[dict[str, Any]] = []
    merged_text: list[str] = []
    part_records: list[dict[str, Any]] = []
    languages: list[str] = []
    schemas: list[dict[str, Any]] = []

    for part in parts:
        result, request_meta = _request_scribe_transcription(Path(part["path"]), model)
        part_index = int(part["index"])
        raw_path = meeting_dir / f"scribe-transcript-part-{part_index}.json"
        write_json(raw_path, result)
        offset = float(part.get("offset_sec") or 0)
        speaker_suffix = str(part.get("speaker_suffix") or "")
        part_utterances, part_labeled, part_segments, part_words = _scribe_result_to_rows(
            result,
            offset_sec=offset,
            speaker_suffix=speaker_suffix,
            part_index=part_index,
        )
        utterances.extend(part_utterances)
        labeled.extend(part_labeled)
        merged_segments.extend(part_segments)
        merged_words.extend(part_words)
        text = clean_text(result.get("text"))
        if text:
            merged_text.append(text)
        language = clean_text(result.get("language_code") or result.get("language"))
        if language and language not in languages:
            languages.append(language)
        schemas.append(_response_schema_summary(result))
        speakers = sorted({str(row["label"]) for row in part_labeled if str(row.get("label") or "").strip()})
        part_records.append(
            _remote_part_record(
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
    merged_words.sort(key=lambda row: (float(row.get("start", 0)), float(row.get("end", 0))))
    merged_result: dict[str, Any] = {
        "model": model,
        "text": "\n".join(merged_text),
        "segments": merged_segments,
        "words": merged_words,
        "schema": _merge_schema_summaries(schemas),
    }
    if languages:
        merged_result["language"] = languages[0] if len(languages) == 1 else languages
    return utterances, labeled, merged_result, part_records


def _request_scribe_transcription(
    audio: Path,
    model: str,
    *,
    max_attempts: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv()
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is required for ElevenLabs Scribe transcription")

    timeout = httpx.Timeout(connect=30.0, read=7_200.0, write=7_200.0, pool=30.0)
    started = time.monotonic()
    response: httpx.Response | None = None
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with audio.open("rb") as file_handle:
                response = httpx.post(
                    ELEVENLABS_STT_URL,
                    headers={"xi-api-key": key, "Accept": "application/json"},
                    data={"model_id": model, "diarize": "true"},
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
            f"ElevenLabs Scribe failed with HTTP {response.status_code} after "
            f"{round_sec(wall_clock)}s and {max_attempts} attempts: {response.text[:2000]}"
        )
    raise RuntimeError(f"ElevenLabs Scribe failed after {round_sec(wall_clock)}s: {last_error}")


def _scribe_result_to_rows(
    result: dict[str, Any],
    *,
    offset_sec: float,
    speaker_suffix: str,
    part_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_segments = result.get("segments")
    if isinstance(raw_segments, list):
        segments = [_scribe_normalized_segment(segment) for segment in raw_segments if isinstance(segment, dict)]
        segments = [segment for segment in segments if segment is not None]
    else:
        segments = []
    if not segments:
        words = result.get("words")
        if not isinstance(words, list):
            raise RuntimeError(f"ElevenLabs Scribe response lacks timestamped segments/words: {result.keys()}")
        segments = _scribe_segments_from_words(words)

    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    for segment in segments:
        text = clean_text(segment.get("text"))
        if not text:
            continue
        start = float(segment["start"]) + offset_sec
        end = float(segment["end"]) + offset_sec
        if end <= start:
            continue
        row = {
            "t0": round_sec(start),
            "t1": round_sec(end),
            "text": text,
        }
        raw_speaker = _clean_speaker_label(segment.get("speaker_id") or segment.get("speaker")) or "UNKNOWN"
        label = f"{raw_speaker}{speaker_suffix}" if speaker_suffix else raw_speaker
        utterances.append(row)
        labeled_row = dict(row)
        labeled_row["label"] = label
        labeled_row["speaker_id"] = raw_speaker
        labeled_row["scribe_part"] = part_index
        if segment.get("word_count") is not None:
            labeled_row["word_count"] = int(segment["word_count"])
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

    merged_words = _offset_scribe_words(
        result.get("words") if isinstance(result.get("words"), list) else [],
        offset_sec=offset_sec,
        speaker_suffix=speaker_suffix,
        part_index=part_index,
    )
    return utterances, labeled, merged_segments, merged_words


def _scribe_normalized_segment(segment: dict[str, Any]) -> dict[str, Any] | None:
    text = clean_text(segment.get("text") or segment.get("transcript"))
    start = _seconds_value(segment.get("start", segment.get("start_time")))
    end = _seconds_value(segment.get("end", segment.get("end_time")))
    words = segment.get("words")
    if (start is None or end is None) and isinstance(words, list):
        timestamped_words = [_scribe_word_record(word) for word in words if isinstance(word, dict)]
        timestamped_words = [word for word in timestamped_words if word is not None]
        if timestamped_words:
            start = timestamped_words[0]["start"] if start is None else start
            end = timestamped_words[-1]["end"] if end is None else end
            if not text:
                text = _join_word_tokens([str(word["text"]) for word in timestamped_words])
    if start is None or end is None or end <= start or not text:
        return None
    out = dict(segment)
    out["text"] = text
    out["start"] = round_sec(start)
    out["end"] = round_sec(end)
    out["speaker_id"] = _clean_speaker_label(
        segment.get("speaker_id") or segment.get("speaker") or segment.get("speaker_label")
    ) or "UNKNOWN"
    if isinstance(words, list):
        out["word_count"] = sum(1 for word in words if isinstance(word, dict) and _scribe_word_record(word))
    return out


def _scribe_segments_from_words(words: list[Any]) -> list[dict[str, Any]]:
    records = [_scribe_word_record(word) for word in words if isinstance(word, dict)]
    records = [record for record in records if record is not None]
    if not records:
        return []

    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        text = _join_word_tokens([str(item["text"]) for item in current])
        if text:
            segments.append(
                {
                    "text": text,
                    "start": round_sec(current[0]["start"]),
                    "end": round_sec(current[-1]["end"]),
                    "speaker_id": current[0]["speaker_id"],
                    "word_count": len(current),
                    "source": "derived_from_words",
                }
            )
        current.clear()

    for record in records:
        if current:
            previous = current[-1]
            current_duration = float(previous["end"]) - float(current[0]["start"])
            speaker_changed = record["speaker_id"] != current[0]["speaker_id"]
            gap = float(record["start"]) - float(previous["end"])
            too_long = current_duration >= 12.0 or len(current) >= 35
            if speaker_changed or gap > 1.2 or too_long:
                flush()
        current.append(record)
        text = str(record["text"])
        current_duration = float(record["end"]) - float(current[0]["start"])
        if len(current) >= 4 and current_duration >= 1.0 and re.search(r"[.!?]$", text):
            flush()
    flush()
    return segments


def _scribe_word_record(word: dict[str, Any]) -> dict[str, Any] | None:
    word_type = clean_text(word.get("type")).lower()
    text = clean_text(word.get("text") or word.get("word"))
    if word_type and word_type not in {"word", "audio_event", "spacing"} and not text:
        return None
    if not text or word_type == "spacing":
        return None
    start = _seconds_value(word.get("start", word.get("start_time")))
    end = _seconds_value(word.get("end", word.get("end_time")))
    if start is None or end is None or end <= start:
        return None
    return {
        "text": text,
        "start": float(start),
        "end": float(end),
        "speaker_id": _clean_speaker_label(
            word.get("speaker_id") or word.get("speaker") or word.get("speaker_label")
        ) or "UNKNOWN",
    }


def _offset_scribe_words(
    words: list[Any],
    *,
    offset_sec: float,
    speaker_suffix: str,
    part_index: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for word in words:
        if not isinstance(word, dict):
            continue
        start = _seconds_value(word.get("start", word.get("start_time")))
        end = _seconds_value(word.get("end", word.get("end_time")))
        if start is None or end is None:
            continue
        raw_speaker = _clean_speaker_label(
            word.get("speaker_id") or word.get("speaker") or word.get("speaker_label")
        )
        out = dict(word)
        out["start"] = round_sec(float(start) + offset_sec)
        out["end"] = round_sec(float(end) + offset_sec)
        if raw_speaker:
            out["speaker_id"] = f"{raw_speaker}{speaker_suffix}" if speaker_suffix else raw_speaker
            if speaker_suffix:
                out["raw_speaker_id"] = raw_speaker
        out["part"] = part_index
        output.append(out)
    return output


def _transcribe_assemblyai_parts(
    meeting_dir: Path,
    parts: list[dict[str, Any]],
    model: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    merged_words: list[dict[str, Any]] = []
    merged_text: list[str] = []
    part_records: list[dict[str, Any]] = []
    schemas: list[dict[str, Any]] = []

    for part in parts:
        result, request_meta = _request_assemblyai_transcription(Path(part["path"]), model)
        part_index = int(part["index"])
        raw_path = meeting_dir / f"assemblyai-transcript-part-{part_index}.json"
        write_json(raw_path, result)
        offset = float(part.get("offset_sec") or 0)
        speaker_suffix = str(part.get("speaker_suffix") or "")
        part_utterances, part_labeled, part_segments, part_words = _assemblyai_result_to_rows(
            result,
            offset_sec=offset,
            speaker_suffix=speaker_suffix,
            part_index=part_index,
        )
        utterances.extend(part_utterances)
        labeled.extend(part_labeled)
        merged_segments.extend(part_segments)
        merged_words.extend(part_words)
        text = clean_text(result.get("text"))
        if text:
            merged_text.append(text)
        schemas.append(_response_schema_summary(result))
        speakers = sorted({str(row["label"]) for row in part_labeled if str(row.get("label") or "").strip()})
        record = _remote_part_record(
            part,
            raw_path,
            request_meta,
            segment_count=len(part_segments),
            utterance_count=len(part_utterances),
            speaker_count=len(speakers),
        )
        if request_meta.get("transcript_id"):
            record["transcript_id"] = request_meta["transcript_id"]
        part_records.append(record)

    utterances.sort(key=lambda row: (float(row["t0"]), float(row["t1"])))
    labeled.sort(key=lambda row: (float(row["t0"]), float(row["t1"])))
    merged_segments.sort(key=lambda row: (float(row.get("start", 0)), float(row.get("end", 0))))
    merged_words.sort(key=lambda row: (float(row.get("start", 0)), float(row.get("end", 0))))
    merged_result = {
        "model": model,
        "speech_models": _assemblyai_speech_models(model),
        "text": "\n".join(merged_text),
        "utterances": merged_segments,
        "words": merged_words,
        "schema": _merge_schema_summaries(schemas),
    }
    return utterances, labeled, merged_result, part_records


def _request_assemblyai_transcription(
    audio: Path,
    model: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv()
    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise RuntimeError("ASSEMBLYAI_API_KEY is required for AssemblyAI transcription")

    upload_timeout = httpx.Timeout(connect=30.0, read=1_800.0, write=7_200.0, pool=30.0)
    poll_timeout = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)
    started = time.monotonic()
    transcript_id = ""
    with httpx.Client(headers={"authorization": key}, timeout=upload_timeout) as client:
        with audio.open("rb") as file_handle:
            upload_response = client.post(
                f"{ASSEMBLYAI_BASE_URL}/upload",
                headers={"content-type": "application/octet-stream"},
                content=file_handle,
            )
        if upload_response.status_code >= 400:
            raise RuntimeError(
                f"AssemblyAI upload failed with HTTP {upload_response.status_code}: "
                f"{upload_response.text[:2000]}"
            )
        upload_url = upload_response.json().get("upload_url")
        if not upload_url:
            raise RuntimeError(f"AssemblyAI upload response lacked upload_url: {upload_response.text[:2000]}")

        speech_models = _assemblyai_speech_models(model)
        payload = {
            "audio_url": upload_url,
            "speaker_labels": True,
            "speech_models": speech_models,
        }
        create_response = client.post(
            f"{ASSEMBLYAI_BASE_URL}/transcript",
            headers={"content-type": "application/json"},
            json=payload,
        )
        if create_response.status_code >= 400:
            raise RuntimeError(
                f"AssemblyAI transcript create failed with HTTP {create_response.status_code}: "
                f"{create_response.text[:2000]}"
            )
        created = create_response.json()
        transcript_id = str(created.get("id") or "")
        if not transcript_id:
            raise RuntimeError(f"AssemblyAI transcript create response lacked id: {create_response.text[:2000]}")

    poll_started = time.monotonic()
    poll_count = 0
    with httpx.Client(headers={"authorization": key}, timeout=poll_timeout) as client:
        while True:
            poll_count += 1
            response = client.get(f"{ASSEMBLYAI_BASE_URL}/transcript/{transcript_id}")
            if response.status_code >= 400:
                raise RuntimeError(
                    f"AssemblyAI transcript poll failed with HTTP {response.status_code}: "
                    f"{response.text[:2000]}"
                )
            result = response.json()
            status = str(result.get("status") or "").lower()
            if status == "completed":
                wall_clock = time.monotonic() - started
                return result, {
                    "wall_clock_sec": round_sec(wall_clock),
                    "transcript_id": transcript_id,
                    "poll_count": poll_count,
                    "speech_models": speech_models,
                    "create_payload": {"speaker_labels": True, "speech_models": speech_models},
                }
            if status == "error":
                raise RuntimeError(
                    "AssemblyAI transcript failed: "
                    + clean_text(result.get("error") or result.get("error_message") or result)
                )
            if time.monotonic() - poll_started > ASSEMBLYAI_POLL_TIMEOUT_SEC:
                raise RuntimeError(
                    f"AssemblyAI transcript {transcript_id} did not complete within "
                    f"{ASSEMBLYAI_POLL_TIMEOUT_SEC}s; last status={status or 'unknown'}"
                )
            time.sleep(ASSEMBLYAI_POLL_INTERVAL_SEC)


def _assemblyai_result_to_rows(
    result: dict[str, Any],
    *,
    offset_sec: float,
    speaker_suffix: str,
    part_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_utterances = result.get("utterances")
    if isinstance(raw_utterances, list) and raw_utterances:
        segments = _assemblyai_segments_from_utterances(raw_utterances)
    else:
        segments = []
    if not segments:
        words = result.get("words")
        if not isinstance(words, list):
            raise RuntimeError(f"AssemblyAI response lacks timestamped utterances/words: {result.keys()}")
        segments = _assemblyai_segments_from_words(words)

    utterances: list[dict[str, Any]] = []
    labeled: list[dict[str, Any]] = []
    merged_segments: list[dict[str, Any]] = []
    for segment in segments:
        text = clean_text(segment.get("text"))
        if not text:
            continue
        start = float(segment["start"]) + offset_sec
        end = float(segment["end"]) + offset_sec
        if end <= start:
            continue
        row = {
            "t0": round_sec(start),
            "t1": round_sec(end),
            "text": text,
        }
        raw_speaker = _clean_speaker_label(segment.get("speaker") or segment.get("speaker_id")) or "UNKNOWN"
        label = f"{raw_speaker}{speaker_suffix}" if speaker_suffix else raw_speaker
        utterances.append(row)
        labeled_row = dict(row)
        labeled_row["label"] = label
        labeled_row["speaker_id"] = raw_speaker
        labeled_row["assemblyai_part"] = part_index
        if segment.get("confidence") is not None:
            labeled_row["confidence"] = round(float(segment["confidence"]), 4)
        if segment.get("word_count") is not None:
            labeled_row["word_count"] = int(segment["word_count"])
        labeled.append(labeled_row)

        merged_segment = dict(segment)
        merged_segment["text"] = text
        merged_segment["start"] = row["t0"]
        merged_segment["end"] = row["t1"]
        merged_segment["speaker"] = label
        if label != raw_speaker:
            merged_segment["raw_speaker"] = raw_speaker
        merged_segment["part"] = part_index
        merged_segments.append(merged_segment)

    merged_words = _offset_assemblyai_words(
        result.get("words") if isinstance(result.get("words"), list) else [],
        offset_sec=offset_sec,
        speaker_suffix=speaker_suffix,
        part_index=part_index,
    )
    return utterances, labeled, merged_segments, merged_words


def _assemblyai_normalized_utterance(item: dict[str, Any]) -> dict[str, Any] | None:
    text = clean_text(item.get("text"))
    start = _millis_value(item.get("start"))
    end = _millis_value(item.get("end"))
    if start is None or end is None or end <= start or not text:
        return None
    out = dict(item)
    out["text"] = text
    out["start"] = round_sec(start)
    out["end"] = round_sec(end)
    out["speaker"] = _clean_speaker_label(item.get("speaker") or item.get("speaker_id")) or "UNKNOWN"
    words = item.get("words")
    if isinstance(words, list):
        out["word_count"] = len(words)
    return out


def _assemblyai_segments_from_utterances(utterances: list[Any]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    fallback_segments: list[dict[str, Any]] = []
    for item in utterances:
        if not isinstance(item, dict):
            continue
        normalized = _assemblyai_normalized_utterance(item)
        if normalized is not None:
            fallback_segments.append(normalized)
        speaker = _clean_speaker_label(item.get("speaker") or item.get("speaker_id")) or "UNKNOWN"
        confidence = item.get("confidence")
        words = item.get("words")
        if not isinstance(words, list) or not words:
            continue
        word_rows = []
        for word in words:
            if not isinstance(word, dict):
                continue
            with_speaker = dict(word)
            with_speaker.setdefault("speaker", speaker)
            word_rows.append(with_speaker)
        for segment in _assemblyai_segments_from_words(word_rows):
            segment["source"] = "derived_from_utterance_words"
            segment["speaker"] = speaker
            if confidence is not None:
                segment["confidence"] = confidence
            segments.append(segment)
    return segments or fallback_segments


def _assemblyai_segments_from_words(words: list[Any]) -> list[dict[str, Any]]:
    records = [_assemblyai_word_record(word) for word in words if isinstance(word, dict)]
    records = [record for record in records if record is not None]
    if not records:
        return []

    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        text = _join_word_tokens([str(item["text"]) for item in current])
        if text:
            segments.append(
                {
                    "text": text,
                    "start": round_sec(current[0]["start"]),
                    "end": round_sec(current[-1]["end"]),
                    "speaker": current[0]["speaker"],
                    "word_count": len(current),
                    "source": "derived_from_words",
                }
            )
        current.clear()

    for record in records:
        if current:
            previous = current[-1]
            current_duration = float(previous["end"]) - float(current[0]["start"])
            speaker_changed = record["speaker"] != current[0]["speaker"]
            gap = float(record["start"]) - float(previous["end"])
            too_long = current_duration >= 12.0 or len(current) >= 35
            if speaker_changed or gap > 1.2 or too_long:
                flush()
        current.append(record)
        text = str(record["text"])
        current_duration = float(record["end"]) - float(current[0]["start"])
        if len(current) >= 4 and current_duration >= 1.0 and re.search(r"[.!?]$", text):
            flush()
    flush()
    return segments


def _assemblyai_word_record(word: dict[str, Any]) -> dict[str, Any] | None:
    text = clean_text(word.get("text") or word.get("word"))
    start = _millis_value(word.get("start"))
    end = _millis_value(word.get("end"))
    if not text or start is None or end is None or end <= start:
        return None
    return {
        "text": text,
        "start": float(start),
        "end": float(end),
        "speaker": _clean_speaker_label(word.get("speaker") or word.get("speaker_id")) or "UNKNOWN",
    }


def _offset_assemblyai_words(
    words: list[Any],
    *,
    offset_sec: float,
    speaker_suffix: str,
    part_index: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for word in words:
        if not isinstance(word, dict):
            continue
        start = _millis_value(word.get("start"))
        end = _millis_value(word.get("end"))
        if start is None or end is None:
            continue
        raw_speaker = _clean_speaker_label(word.get("speaker") or word.get("speaker_id"))
        out = dict(word)
        out["start"] = round_sec(float(start) + offset_sec)
        out["end"] = round_sec(float(end) + offset_sec)
        if raw_speaker:
            out["speaker"] = f"{raw_speaker}{speaker_suffix}" if speaker_suffix else raw_speaker
            if speaker_suffix:
                out["raw_speaker"] = raw_speaker
        out["part"] = part_index
        output.append(out)
    return output


def _remote_part_record(
    part: dict[str, Any],
    raw_path: Path,
    request_meta: dict[str, Any],
    *,
    segment_count: int,
    utterance_count: int,
    speaker_count: int,
) -> dict[str, Any]:
    record = {
        "index": int(part["index"]),
        "offset_sec": round_sec(float(part.get("offset_sec") or 0)),
        "speaker_suffix": str(part.get("speaker_suffix") or ""),
        "request_wall_clock_sec": request_meta["wall_clock_sec"],
        "attempts": request_meta.get("attempts", 1),
        "poll_count": request_meta.get("poll_count"),
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


def _seconds_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _millis_value(value: Any) -> float | None:
    raw = _seconds_value(value)
    if raw is None:
        return None
    return raw / 1000.0


def _clean_speaker_label(value: Any) -> str:
    return re.sub(r"\s+", "_", clean_text(value)).strip("_")


def _join_word_tokens(tokens: list[str]) -> str:
    text = " ".join(clean_text(token) for token in tokens if clean_text(token))
    text = re.sub(r"\s+([,.!?;:%)\]])", r"\1", text)
    text = re.sub(r"([(])\s+", r"\1", text)
    text = re.sub(r"\s+'", "'", text)
    return clean_text(text)


def _response_schema_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "top_level_keys": sorted(str(key) for key in payload.keys()),
    }
    for key, value in payload.items():
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
            first = next((item for item in value if isinstance(item, dict)), None)
            if first is not None:
                summary[f"{key}_item_keys"] = sorted(str(item_key) for item_key in first.keys())
    return summary


def _merge_schema_summaries(schemas: list[dict[str, Any]]) -> dict[str, Any]:
    if not schemas:
        return {}
    top_level_keys: set[str] = set()
    list_counts: dict[str, int] = {}
    item_keys: dict[str, set[str]] = {}
    for schema in schemas:
        top_level_keys.update(str(key) for key in schema.get("top_level_keys", []))
        for key, value in schema.items():
            if key.endswith("_count") and isinstance(value, int):
                list_counts[key] = list_counts.get(key, 0) + value
            if key.endswith("_item_keys") and isinstance(value, list):
                item_keys.setdefault(key, set()).update(str(item) for item in value)
    out: dict[str, Any] = {"top_level_keys": sorted(top_level_keys)}
    out.update({key: value for key, value in sorted(list_counts.items())})
    out.update({key: sorted(value) for key, value in sorted(item_keys.items())})
    return out


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _assemblyai_model_from_env(default: str) -> str:
    value = os.environ.get("ASSEMBLYAI_SPEECH_MODEL")
    return clean_text(value) or default


def _assemblyai_speech_models(model: str) -> list[str]:
    configured = os.environ.get("ASSEMBLYAI_SPEECH_MODELS")
    if configured:
        models = [clean_text(item) for item in configured.split(",")]
        models = [item for item in models if item]
        if models:
            return models
    cleaned = clean_text(model)
    if cleaned == "universal":
        return ["universal-3-pro"]
    return [cleaned or DEFAULT_ASSEMBLYAI_MODEL]


def _vendor_cost_meta(duration_sec: float, *, rate_env: str, default_rate: float) -> dict[str, Any]:
    load_dotenv()
    rate = _env_float(rate_env, default_rate)
    max_cost = _env_float("ASR_VENDOR_MAX_COST_USD", ASR_VENDOR_MAX_COST_USD)
    estimated = (duration_sec / 3600.0) * rate if duration_sec else 0.0
    return {
        "estimated_cost_usd": round(estimated, 4),
        "audio_hours": round(duration_sec / 3600.0, 4) if duration_sec else 0.0,
        "usd_per_hour": rate,
        "rate_env": rate_env,
        "max_cost_usd": max_cost,
        "max_cost_env": "ASR_VENDOR_MAX_COST_USD",
    }


def _enforce_vendor_budget(provider: str, cost_meta: dict[str, Any]) -> None:
    estimated = float(cost_meta.get("estimated_cost_usd") or 0)
    max_cost = float(cost_meta.get("max_cost_usd") or ASR_VENDOR_MAX_COST_USD)
    if estimated > max_cost:
        raise RuntimeError(
            f"{provider} estimated ASR cost ${estimated:.2f} exceeds configured budget "
            f"${max_cost:.2f}; aborting before upload"
        )


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
