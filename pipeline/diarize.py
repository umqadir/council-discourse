from __future__ import annotations

import importlib.metadata
import os
import time
from pathlib import Path
from typing import Any

from .artifacts import normalize_utterances, read_jsonl, round_sec, write_json, write_jsonl
from .models import Meeting
from .utils import load_dotenv

DEFAULT_DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"


def diarize_meeting(
    meeting: Meeting,
    model: str = DEFAULT_DIARIZATION_MODEL,
    device: str | None = None,
) -> Path:
    audio = meeting.meeting_dir / "audio-16k.wav"
    if not audio.exists() or audio.stat().st_size <= 10_000:
        raise RuntimeError(f"missing prepared 16kHz audio for diarization: {audio}")

    utterances_path = meeting.meeting_dir / "utterances.jsonl"
    utterances = normalize_utterances(read_jsonl(utterances_path))
    if not utterances:
        raise RuntimeError(f"missing ASR utterances for diarization label assignment: {utterances_path}")

    started = time.monotonic()
    raw_turns, runtime = _run_pyannote(audio, model=model, device=device)
    turns = normalize_diarization_turns(raw_turns)
    wall_clock = time.monotonic() - started
    if not turns:
        raise RuntimeError("pyannote returned no diarization turns")

    labeled = assign_labels_to_utterances(utterances, turns)
    labels = sorted({str(turn["label"]) for turn in turns})

    diarization_path = meeting.meeting_dir / "diarization.jsonl"
    labeled_path = meeting.meeting_dir / "utterances-labeled.jsonl"
    meta_path = meeting.meeting_dir / "diarize-meta.json"
    write_jsonl(diarization_path, turns)
    write_jsonl(labeled_path, labeled)
    meta = {
        "model": model,
        "audio_file": str(audio),
        "device": runtime["device"],
        "wall_clock_sec": round_sec(wall_clock),
        "n_labels": len(labels),
        "labels": labels,
        "turn_count": len(turns),
        "utterance_count": len(labeled),
        "pyannote_audio_version": _package_version("pyannote.audio"),
        "torch_version": _package_version("torch"),
    }
    if runtime.get("requested_device"):
        meta["requested_device"] = runtime["requested_device"]
    if runtime.get("fallback_reason"):
        meta["fallback_reason"] = runtime["fallback_reason"]
    write_json(meta_path, meta)
    return labeled_path


def normalize_diarization_turns(raw_turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    label_by_raw: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    for raw in sorted(raw_turns, key=lambda item: (float(item.get("start", 0)), float(item.get("end", 0)))):
        start = float(raw.get("start", 0))
        end = float(raw.get("end", 0))
        if end <= start:
            continue
        raw_label = str(raw.get("label") or raw.get("speaker") or raw.get("raw_label") or "").strip()
        if not raw_label:
            continue
        if raw_label not in label_by_raw:
            label_by_raw[raw_label] = f"SPK_{len(label_by_raw):02d}"
        turns.append(
            {
                "start": round_sec(start),
                "end": round_sec(end),
                "label": label_by_raw[raw_label],
            }
        )
    return turns


def assign_labels_to_utterances(
    utterances: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not turns:
        raise ValueError("cannot assign diarization labels without turns")

    normalized_turns = [
        {
            "start": float(turn["start"]),
            "end": float(turn["end"]),
            "label": str(turn["label"]),
            "midpoint": (float(turn["start"]) + float(turn["end"])) / 2,
        }
        for turn in turns
        if float(turn.get("end", 0)) > float(turn.get("start", 0)) and str(turn.get("label") or "").strip()
    ]
    if not normalized_turns:
        raise ValueError("cannot assign diarization labels without valid turns")

    labeled: list[dict[str, Any]] = []
    for row in normalize_utterances(utterances):
        start = float(row["t0"])
        end = float(row["t1"])
        label = _max_overlap_label(start, end, normalized_turns)
        if label is None:
            label = _nearest_midpoint_label(start, end, normalized_turns)
        out = dict(row)
        out["label"] = label
        labeled.append(out)
    return labeled


def _max_overlap_label(start: float, end: float, turns: list[dict[str, Any]]) -> str | None:
    best_label = None
    best_overlap = 0.0
    best_duration = 0.0
    for turn in turns:
        overlap = max(0.0, min(end, float(turn["end"])) - max(start, float(turn["start"])))
        if overlap <= 0:
            continue
        duration = float(turn["end"]) - float(turn["start"])
        if overlap > best_overlap or (overlap == best_overlap and duration > best_duration):
            best_label = str(turn["label"])
            best_overlap = overlap
            best_duration = duration
    return best_label


def _nearest_midpoint_label(start: float, end: float, turns: list[dict[str, Any]]) -> str:
    midpoint = (start + end) / 2
    nearest = min(turns, key=lambda turn: abs(float(turn["midpoint"]) - midpoint))
    return str(nearest["label"])


def _run_pyannote(audio: Path, model: str, device: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    load_dotenv()
    token = os.environ.get("HF_ACCESS_TOKEN") or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if not token:
        raise RuntimeError("HF_ACCESS_TOKEN is required for pyannote diarization")

    import torch
    from pyannote.audio import Pipeline

    device_name = device or _default_torch_device(torch)
    try:
        pipeline = Pipeline.from_pretrained(model, token=token)
    except TypeError:
        pipeline = Pipeline.from_pretrained(model, use_auth_token=token)
    try:
        turns = _run_pyannote_pipeline(pipeline, torch, audio, device_name)
        return turns, {"device": device_name}
    except Exception as exc:
        if device_name != "mps":
            raise
        turns = _run_pyannote_pipeline(pipeline, torch, audio, "cpu")
        return turns, {"device": "cpu", "requested_device": "mps", "fallback_reason": str(exc)[:500]}


def _run_pyannote_pipeline(pipeline: Any, torch_module: Any, audio: Path, device_name: str) -> list[dict[str, Any]]:
    pipeline.to(torch_module.device(device_name))
    result = pipeline(str(audio))
    annotation = _pyannote_annotation(result)
    return _pyannote_turns(annotation)


def _default_torch_device(torch_module: Any) -> str:
    mps = getattr(getattr(torch_module, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def _pyannote_annotation(result: Any) -> Any:
    for attr in ("exclusive_speaker_diarization", "speaker_diarization", "diarization"):
        annotation = getattr(result, attr, None)
        if annotation is not None:
            return annotation
    return result


def _pyannote_turns(annotation: Any) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for segment, _, label in annotation.itertracks(yield_label=True):
        turns.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "label": str(label),
            }
        )
    return turns


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
