from __future__ import annotations

from pathlib import Path

from .chapterize import chapterize_meeting
from .diarize import DEFAULT_DIARIZATION_MODEL, diarize_meeting
from .models import Meeting
from .speakers import name_speakers_meeting
from .transcribe import transcribe_meeting


def transcribe(meeting: Meeting, backend: str = "local", model: str | None = None) -> Path:
    """ASR interface: audio files in meeting_dir -> utterances.jsonl."""
    return transcribe_meeting(meeting, backend=backend, model=model)


def diarize(meeting: Meeting, model: str = DEFAULT_DIARIZATION_MODEL, device: str | None = None) -> Path:
    """Diarization interface: audio-16k.wav + utterances.jsonl -> utterances-labeled.jsonl."""
    return diarize_meeting(meeting, model=model, device=device)


def name_speakers(meeting: Meeting, model: str = "gemini-3.5-flash") -> Path:
    """Speaker naming interface: utterances-labeled.jsonl -> utterances-named.jsonl."""
    return name_speakers_meeting(meeting, model=model)


def chapterize(meeting: Meeting, model: str = "gemini-3.5-flash") -> Path:
    """Chaptering interface: utterances-named.jsonl + agenda context -> chapters.json."""
    chapters_path, _ = chapterize_meeting(meeting, model=model)
    return Path(chapters_path)
