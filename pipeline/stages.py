from __future__ import annotations

from pathlib import Path

from .models import Meeting


def transcribe(meeting: Meeting) -> Path:
    """ASR interface: audio files in meeting_dir -> utterances.jsonl."""
    output = meeting.meeting_dir / "utterances.jsonl"
    raise NotImplementedError(f"ASR benchmark pending; expected output is {output}")


def name_speakers(meeting: Meeting) -> Path:
    """Speaker naming interface: utterances.jsonl -> utterances-named.jsonl."""
    input_path = meeting.meeting_dir / "utterances.jsonl"
    output = meeting.meeting_dir / "utterances-named.jsonl"
    raise NotImplementedError(f"speaker naming pending; read {input_path}, write {output}")


def chapterize(meeting: Meeting) -> Path:
    """Chaptering interface: utterances-named.jsonl + agenda context -> chapters.json."""
    input_path = meeting.meeting_dir / "utterances-named.jsonl"
    output = meeting.meeting_dir / "chapters.json"
    raise NotImplementedError(f"chaptering benchmark pending; read {input_path}, write {output}")
