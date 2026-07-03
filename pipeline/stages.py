from __future__ import annotations

from pathlib import Path

from .config import DATA_DIR
from .chapterize import chapterize_meeting
from .diarize import DEFAULT_DIARIZATION_MODEL, diarize_meeting
from .models import Meeting
from .speakers import name_speakers_meeting
from .transcribe import DEFAULT_VOXTRAL_MODEL, transcribe_meeting
from .voxtral_prod import transcribe_voxtral_production


def transcribe(meeting: Meeting, backend: str = "voxtral", model: str | None = None) -> Path:
    """ASR interface: audio files in meeting_dir -> utterances.jsonl."""
    if backend in {"voxtral", "mistral-voxtral"} and not _is_benchmark_dir(meeting.meeting_dir):
        return transcribe_voxtral_production(meeting, model=model or DEFAULT_VOXTRAL_MODEL)
    return transcribe_meeting(meeting, backend=backend, model=model)


def _is_benchmark_dir(meeting_dir: Path) -> bool:
    try:
        meeting_dir.resolve().relative_to((DATA_DIR / "benchmark").resolve())
        return True
    except ValueError:
        return False


def diarize(meeting: Meeting, model: str = DEFAULT_DIARIZATION_MODEL, device: str | None = None) -> Path:
    """Diarization interface: audio-16k.wav + utterances.jsonl -> utterances-labeled.jsonl."""
    return diarize_meeting(meeting, model=model, device=device)


def name_speakers(
    meeting: Meeting,
    model: str = "gemini-3.5-flash",
    *,
    input_path: Path | None = None,
    output_path: Path | None = None,
    meta_path: Path | None = None,
    runlog_stage: str = "name_speakers",
    write_runlog: bool = True,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    llm_api_key_env: str | None = None,
    verification_model: str | None = "gemini-3.1-flash-lite",
    verification_base_url: str | None = None,
    verification_api_key: str | None = None,
    verification_api_key_env: str | None = None,
) -> Path:
    """Speaker naming interface: utterances-labeled.jsonl -> utterances-named.jsonl."""
    return name_speakers_meeting(
        meeting,
        model=model,
        input_path=input_path,
        output_path=output_path,
        meta_path=meta_path,
        runlog_stage=runlog_stage,
        write_runlog=write_runlog,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_api_key_env=llm_api_key_env,
        verification_model=verification_model,
        verification_base_url=verification_base_url,
        verification_api_key=verification_api_key,
        verification_api_key_env=verification_api_key_env,
    )


def chapterize(
    meeting: Meeting,
    model: str = "gemini-3.5-flash",
    *,
    input_path: Path | None = None,
    output_path: Path | None = None,
    derived_path: Path | None = None,
    runlog_stage: str = "chapterize",
    write_runlog: bool = True,
    llm_base_url: str | None = None,
    llm_api_key: str | None = None,
    llm_api_key_env: str | None = None,
) -> Path:
    """Chaptering interface: utterances-named.jsonl + agenda context -> chapters.json."""
    chapters_path, _ = chapterize_meeting(
        meeting,
        model=model,
        input_path=str(input_path) if input_path else None,
        output_path=str(output_path) if output_path else None,
        derived_path=str(derived_path) if derived_path else None,
        runlog_stage=runlog_stage,
        write_runlog=write_runlog,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_api_key_env=llm_api_key_env,
    )
    return Path(chapters_path)
