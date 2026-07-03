from __future__ import annotations

import json
from pathlib import Path

from pipeline import db
from pipeline.artifacts import read_jsonl
from pipeline.models import Meeting
from pipeline.production import merge_results, pending_matrix_json, process_one
from pipeline.stages import transcribe
from pipeline.voxtral_prod import _transcribe_voxtral_parts_resumable


def test_pending_matrix_json_includes_only_incomplete_meetings(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    db.upsert_meeting(conn, {"meeting_key": "pending", "viebit_filename": "pending"})
    db.upsert_meeting(conn, {"meeting_key": "done", "viebit_filename": "done"})
    db.update_meeting(
        conn,
        "done",
        {
            "fetch_status": "fetched",
            "prepare_status": "prepared",
            "transcribe_status": "transcribed",
            "diarize_status": "diarized",
            "name_speakers_status": "named",
            "chapterize_status": "chapterized",
        },
    )

    matrix = json.loads(pending_matrix_json(conn))

    assert [item["meeting_key"] for item in matrix["include"]] == ["pending"]


def test_process_one_dry_run_writes_mergeable_result_without_mutating_status(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    db.upsert_meeting(conn, {"meeting_key": "m1", "viebit_filename": "m1"})
    result_json = tmp_path / "result.json"

    assert process_one(db_path, "m1", result_json=result_json, dry_run=True) == 0

    result = json.loads(result_json.read_text())
    row = db.get_meeting(db.connect(db_path), "m1")
    assert result["status"] == "complete"
    assert [stage["status"] for stage in result["stages"]] == ["would_run"] * 5
    assert row["fetch_status"] == "pending"
    assert row["chapterize_status"] == "stubbed"


def test_merge_results_updates_single_writer_registry(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    db.upsert_meeting(conn, {"meeting_key": "m1", "viebit_filename": "m1"})
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    row = dict(db.get_meeting(conn, "m1"))
    row.update(
        {
            "fetch_status": "fetched",
            "prepare_status": "prepared",
            "transcribe_status": "transcribed",
            "diarize_status": "diarized",
            "name_speakers_status": "named",
            "chapterize_status": "chapterized",
            "video_web_url": "https://videos.example/m1/video-web.mp4",
        }
    )
    (results_dir / "m1.json").write_text(json.dumps({"meeting_key": "m1", "row": row}) + "\n")

    assert merge_results(db_path, results_dir) == 1

    merged = db.get_meeting(db.connect(db_path), "m1")
    assert merged["chapterize_status"] == "chapterized"
    assert merged["video_web_url"] == "https://videos.example/m1/video-web.mp4"


def test_process_one_uses_configured_production_llm(tmp_path: Path, monkeypatch) -> None:
    for name in (
        "COUNCIL_LLM_PROVIDER",
        "COUNCIL_LLM_MODEL",
        "COUNCIL_LLM_BASE_URL",
        "COUNCIL_LLM_API_KEY_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    db.upsert_meeting(conn, {"meeting_key": "m1", "viebit_filename": "m1"})
    db.update_meeting(
        conn,
        "m1",
        {
            "fetch_status": "fetched",
            "prepare_status": "prepared",
            "transcribe_status": "transcribed",
            "diarize_status": "diarized",
            "video_web_url": "https://videos.example/m1/video-web.mp4",
        },
    )
    meeting_dir = tmp_path / "meetings" / "m1"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "utterances-labeled.jsonl").write_text(
        '{"t0": 0, "t1": 1, "text": "Hello", "label": "speaker_0"}\n'
    )
    captured: list[dict[str, str | None]] = []

    def fake_name_speakers(_meeting, **kwargs):
        captured.append(kwargs)
        return meeting_dir / "utterances-named.jsonl"

    def fake_chapterize(_meeting, **kwargs):
        captured.append(kwargs)
        return meeting_dir / "chapters.json"

    monkeypatch.setattr(
        "pipeline.production.db.meeting_from_row",
        lambda row: Meeting(meeting_key=str(row["meeting_key"]), meeting_dir=meeting_dir),
    )
    monkeypatch.setattr("pipeline.production.name_speakers", fake_name_speakers)
    monkeypatch.setattr("pipeline.production.chapterize", fake_chapterize)

    assert process_one(db_path, "m1") == 0

    assert captured == [
        {
            "model": "z-ai/glm-5.2",
            "llm_base_url": "https://openrouter.ai/api/v1",
            "llm_api_key_env": "OPENROUTER_API_KEY",
        },
        {
            "model": "z-ai/glm-5.2",
            "llm_base_url": "https://openrouter.ai/api/v1",
            "llm_api_key_env": "OPENROUTER_API_KEY",
        },
    ]


def test_voxtral_production_stage_writes_canonical_name_speakers_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("pipeline.transcribe.current_roster", lambda _date: [])
    monkeypatch.setattr(
        "pipeline.voxtral_prod._request_voxtral_transcription_with_backoff",
        lambda _audio, _model, _bias: (
            {
                "text": "Hello.",
                "segments": [
                    {"text": "Hello.", "start": 0.0, "end": 1.2, "speaker_id": "speaker_0"},
                ],
                "usage": {"prompt_audio_seconds": 1.2},
            },
            {"wall_clock_sec": 0.01, "http_status": 200, "attempts": 1},
        ),
    )
    meeting_dir = tmp_path / "meetings" / "m1"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "audio.m4a").write_bytes(b"0" * 10_001)
    meeting = Meeting(meeting_key="m1", meeting_dir=meeting_dir, duration_seconds=2.0)

    output = transcribe(meeting, backend="voxtral")

    assert output == meeting_dir / "utterances.jsonl"
    assert read_jsonl(meeting_dir / "utterances.jsonl") == [{"t0": 0.0, "t1": 1.2, "text": "Hello."}]
    assert read_jsonl(meeting_dir / "utterances-labeled.jsonl")[0]["label"] == "speaker_0"
    assert (meeting_dir / "transcribe-meta.json").exists()
    assert not (meeting_dir / "utterances-voxtral.jsonl").exists()


def test_voxtral_parts_resume_existing_raw_json_and_delay_between_fresh_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VOXTRAL_INTER_CHUNK_DELAY_SEC", "0.25")
    sleeps: list[float] = []
    monkeypatch.setattr("pipeline.voxtral_prod.time.sleep", sleeps.append)
    (tmp_path / "voxtral-transcript-part-1.json").write_text(
        json.dumps(
            {
                "text": "One.",
                "segments": [{"text": "One.", "start": 0.0, "end": 1.0, "speaker_id": "speaker_0"}],
            }
        )
        + "\n"
    )
    calls: list[str] = []

    def fake_request(audio: Path, _model: str, _bias: list[str]):
        calls.append(audio.name)
        index = int(audio.stem.rsplit("-", 1)[-1])
        return (
            {
                "text": f"Part {index}.",
                "segments": [
                    {"text": f"Part {index}.", "start": 0.0, "end": 1.0, "speaker_id": "speaker_0"},
                ],
            },
            {"wall_clock_sec": 0.01, "http_status": 200, "attempts": 1},
        )

    parts = [
        {"index": 1, "path": tmp_path / "part-1.m4a", "offset_sec": 0.0, "speaker_suffix": ""},
        {"index": 2, "path": tmp_path / "part-2.m4a", "offset_sec": 10.0, "speaker_suffix": "_part2"},
        {"index": 3, "path": tmp_path / "part-3.m4a", "offset_sec": 20.0, "speaker_suffix": "_part3"},
    ]

    utterances, labeled, _merged, records = _transcribe_voxtral_parts_resumable(
        tmp_path,
        parts,
        "voxtral-mini-2602",
        [],
        fake_request,
    )

    assert calls == ["part-2.m4a", "part-3.m4a"]
    assert sleeps == [0.25]
    assert records[0]["reused_partial"] is True
    assert records[1]["reused_partial"] is False
    assert [row["t0"] for row in utterances] == [0.0, 10.0, 20.0]
    assert [row["label"] for row in labeled] == ["speaker_0", "speaker_0_part2", "speaker_0_part3"]
