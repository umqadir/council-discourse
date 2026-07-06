from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import db
from pipeline.artifacts import read_jsonl
from pipeline.cli import main as cli_main
from pipeline.models import Meeting
from pipeline.production import merge_results, pending_matrix_json, process_one, select_process_candidates
from pipeline.stages import transcribe
from pipeline.voxtral_prod import _transcribe_voxtral_parts_resumable


def test_pending_matrix_json_includes_only_incomplete_meetings(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    db.upsert_meeting(
        conn,
        {"meeting_key": "pending", "viebit_filename": "pending", "event_date": "2026-06-25T10:00:00"},
    )
    db.upsert_meeting(
        conn,
        {"meeting_key": "done", "viebit_filename": "done", "event_date": "2026-06-25T14:00:00"},
    )
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


def test_pending_matrix_requires_legistar_match_and_post_floor_date(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    # Pre-floor despite a Legistar match: excluded.
    db.upsert_meeting(
        conn,
        {
            "meeting_key": "old-event-date",
            "viebit_filename": "old-event-date",
            "event_date": "2026-06-10T10:00:00",
        },
    )
    # Recent recording but no Legistar match yet: excluded until enriched.
    db.upsert_meeting(
        conn,
        {
            "meeting_key": "recent-unmatched",
            "viebit_filename": "recent-unmatched",
            "viebit_pub_date": "2026-06-25T12:00:00+00:00",
        },
    )
    # Recent and matched: included.
    db.upsert_meeting(
        conn,
        {
            "meeting_key": "recent-matched",
            "viebit_filename": "recent-matched",
            "event_date": "2026-06-25T10:00:00",
        },
    )

    matrix = json.loads(pending_matrix_json(conn))

    assert [item["meeting_key"] for item in matrix["include"]] == ["recent-matched"]


def test_status_includes_truncated_last_error(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    db.upsert_meeting(
        conn,
        {"meeting_key": "m1", "viebit_filename": "m1", "event_date": "2026-06-25T10:00:00"},
    )
    long_error = "verification failed because " + ("x" * 80)
    db.update_meeting(conn, "m1", {"last_error": long_error})

    assert cli_main(["status", "--db", str(db_path), "--limit", "1"]) == 0

    out = capsys.readouterr().out
    assert "error" in out.splitlines()[0]
    assert "verification failed because" in out
    assert ("x" * 80) not in out


def test_ci_health_prints_errors_unmatched_count_and_newest_date(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    # Stale AND inside coverage counts; pre-floor rows are deliberately ignored.
    db.upsert_meeting(
        conn,
        {"meeting_key": "stale-unmatched", "viebit_filename": "stale-unmatched", "viebit_pub_date": "2026-06-22T00:00:00+00:00"},
    )
    db.upsert_meeting(
        conn,
        {"meeting_key": "pre-floor-unmatched", "viebit_filename": "pre-floor-unmatched", "viebit_pub_date": "2000-01-01T00:00:00+00:00"},
    )
    db.upsert_meeting(
        conn,
        {"meeting_key": "err", "viebit_filename": "err", "event_date": "2026-06-25T10:00:00"},
    )
    db.update_meeting(conn, "err", {"last_error": "bad things happened " + ("y" * 220)})

    assert cli_main(["ci-health", "--db", str(db_path)]) == 0

    out = capsys.readouterr().out
    assert "err: bad things happened" in out
    assert "stale_unmatched_viebit_rows_older_than_7d=1" in out
    assert "newest_event_date=2026-06-25T10:00:00" in out


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


def test_registry_dedupe_merges_paid_statuses_before_deleting_event_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    event = db.upsert_meeting(
        conn,
        {
            "legistar_event_id": 123,
            "event_date": "2026-06-25T10:00:00",
            "body_name": "Committee on Finance",
        },
    )
    db.update_meeting(
        conn,
        str(event["meeting_key"]),
        {
            "transcribe_status": "transcribed",
            "diarize_status": "diarized",
            "name_speakers_status": "named",
        },
    )
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV_260625-100000", "viebit_hash": "abc"})

    merged = db.upsert_meeting(
        conn,
        {
            "legistar_event_id": 123,
            "viebit_filename": "NYCC-PV_260625-100000",
        },
    )

    assert merged["meeting_key"] == "NYCC-PV_260625-100000"
    assert merged["event_date"] == "2026-06-25T10:00:00"
    assert merged["viebit_hash"] == "abc"
    assert merged["transcribe_status"] == "transcribed"
    assert merged["name_speakers_status"] == "named"
    with pytest.raises(KeyError):
        db.get_meeting(conn, "event-123")


def test_registry_dedupe_prefers_completed_event_row_over_pristine_filename_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    event = db.upsert_meeting(
        conn,
        {
            "legistar_event_id": 456,
            "event_date": "2026-06-25T10:00:00",
            "body_name": "Committee on Finance",
        },
    )
    db.update_meeting(
        conn,
        str(event["meeting_key"]),
        {
            "fetch_status": "fetched",
            "prepare_status": "prepared",
            "transcribe_status": "transcribed",
            "diarize_status": "diarized",
            "name_speakers_status": "named",
            "chapterize_status": "chapterized",
        },
    )
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV_260625-110000", "viebit_hash": "def"})

    merged = db.upsert_meeting(
        conn,
        {
            "legistar_event_id": 456,
            "viebit_filename": "NYCC-PV_260625-110000",
        },
    )

    assert merged["meeting_key"] == "event-456"
    assert merged["viebit_filename"] == "NYCC-PV_260625-110000"
    assert merged["viebit_hash"] == "def"
    assert merged["chapterize_status"] == "chapterized"
    with pytest.raises(KeyError):
        db.get_meeting(conn, "nycc-pv_260625-110000")


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


def test_merge_results_does_not_regress_completed_statuses(tmp_path: Path) -> None:
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
            "name_speakers_status": "named",
            "chapterize_status": "chapterized",
            "process_attempts": 3,
            "cost_usd": 1.23,
        },
    )
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    row = dict(db.get_meeting(conn, "m1"))
    row.update(
        {
            "fetch_status": "pending",
            "prepare_status": "pending",
            "transcribe_status": "pending",
            "diarize_status": "pending",
            "name_speakers_status": "pending",
            "chapterize_status": "pending",
            "process_attempts": 2,
            "cost_usd": None,
            "video_web_url": "https://videos.example/new.mp4",
        }
    )
    (results_dir / "m1.json").write_text(json.dumps({"meeting_key": "m1", "row": row}) + "\n")

    assert merge_results(db_path, results_dir) == 1

    merged = db.get_meeting(db.connect(db_path), "m1")
    assert merged["fetch_status"] == "fetched"
    assert merged["chapterize_status"] == "chapterized"
    assert merged["process_attempts"] == 3
    assert merged["cost_usd"] == 1.23
    assert merged["video_web_url"] == "https://videos.example/new.mp4"

    row["process_attempts"] = 0
    row["cost_usd"] = 2.5
    (results_dir / "m1.json").write_text(json.dumps({"meeting_key": "m1", "row": row}) + "\n")

    assert merge_results(db_path, results_dir) == 1
    merged = db.get_meeting(db.connect(db_path), "m1")
    assert merged["process_attempts"] == 0
    assert merged["cost_usd"] == 2.5


def test_merge_results_ignores_nested_meeting_artifact_json(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    db.upsert_meeting(conn, {"meeting_key": "m1", "viebit_filename": "m1"})
    db.upsert_meeting(conn, {"meeting_key": "m2", "viebit_filename": "m2"})
    results_dir = tmp_path / "results"
    (results_dir / "meetings" / "m2").mkdir(parents=True)
    row = dict(db.get_meeting(conn, "m1"))
    row["fetch_status"] = "fetched"
    nested_row = dict(db.get_meeting(conn, "m2"))
    nested_row["fetch_status"] = "fetched"
    (results_dir / "m1.json").write_text(json.dumps({"meeting_key": "m1", "row": row}) + "\n")
    (results_dir / "meetings" / "m2" / "artifact.json").write_text(
        json.dumps({"meeting_key": "m2", "row": nested_row}) + "\n"
    )

    assert merge_results(db_path, results_dir) == 1

    merged = db.connect(db_path)
    assert db.get_meeting(merged, "m1")["fetch_status"] == "fetched"
    assert db.get_meeting(merged, "m2")["fetch_status"] == "pending"


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
    # status implies artifacts (reconcile downgrades otherwise): seed the files
    # the fetched/prepared/transcribed statuses claim exist.
    (meeting_dir / "audio.m4a").write_text("x")
    (meeting_dir / "captions-clean.jsonl").write_text("{}\n")
    (meeting_dir / "utterances.jsonl").write_text(
        '{"t0": 0, "t1": 1, "text": "Hello"}\n'
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
    monkeypatch.setattr("pipeline.production.MEETINGS_DIR", tmp_path / "meetings")

    assert process_one(db_path, "m1") == 0

    assert captured == [
        {
            "model": "deepseek/deepseek-v4-pro",
            "llm_base_url": "https://openrouter.ai/api/v1",
            "llm_api_key_env": "OPENROUTER_API_KEY",
        },
        {
            "model": "z-ai/glm-5.2",
            "llm_base_url": "https://openrouter.ai/api/v1",
            "llm_api_key_env": "OPENROUTER_API_KEY",
        },
    ]


def test_process_attempts_increment_reset_and_exclude_at_cap(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    db.upsert_meeting(
        conn,
        {
            "meeting_key": "fail-me",
            "viebit_filename": "fail-me",
            "event_date": "2026-06-25T10:00:00",
        },
    )
    db.update_meeting(conn, "fail-me", {"process_attempts": 4})

    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("fetch exploded")

    monkeypatch.setattr("pipeline.production._stage_fetch", fail_fetch)

    result_json = tmp_path / "failed.json"
    assert process_one(db_path, "fail-me", result_json=result_json) == 0

    failed = json.loads(result_json.read_text())
    row = db.get_meeting(db.connect(db_path), "fail-me")
    assert row["process_attempts"] == 5
    assert failed["row"]["process_attempts"] == 5

    assert select_process_candidates(db.connect(db_path)) == []
    assert "fail-me(5)" in capsys.readouterr().err

    db.update_meeting(
        db.connect(db_path),
        "fail-me",
        {
            "fetch_status": "fetched",
            "prepare_status": "prepared",
            "transcribe_status": "transcribed",
            "diarize_status": "diarized",
            "name_speakers_status": "named",
            "chapterize_status": "chapterized",
        },
    )
    monkeypatch.setattr("pipeline.production._reconcile_artifacts", lambda *_args, **_kwargs: None)
    for name in (
        "_stage_fetch",
        "_stage_prepare",
        "_stage_transcribe_voxtral",
        "_stage_name_speakers",
        "_stage_chapterize",
    ):
        monkeypatch.setattr(f"pipeline.production.{name}", lambda *_args, **_kwargs: None)

    assert process_one(db_path, "fail-me", result_json=tmp_path / "success.json") == 0

    succeeded = json.loads((tmp_path / "success.json").read_text())
    row = db.get_meeting(db.connect(db_path), "fail-me")
    assert row["process_attempts"] == 0
    assert succeeded["row"]["process_attempts"] == 0


def test_process_one_captures_cost_on_success_and_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "registry.db"
    meetings_dir = tmp_path / "meetings"
    meeting_dir = meetings_dir / "m1"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "transcribe-meta.json").write_text(json.dumps({"audio_duration_sec": 7200}) + "\n")
    (meeting_dir / "name-speakers-meta.json").write_text(json.dumps({"exact_cost_total": 0.04}) + "\n")
    (meeting_dir / "chapters.json").write_text(json.dumps({"exact_cost_usd": 0.02, "chapters": [{}]}) + "\n")
    conn = db.connect(db_path)
    db.upsert_meeting(conn, {"meeting_key": "m1", "viebit_filename": "m1"})
    monkeypatch.setattr("pipeline.production.MEETINGS_DIR", meetings_dir)
    monkeypatch.setattr("pipeline.production._reconcile_artifacts", lambda *_args, **_kwargs: None)
    for name in (
        "_stage_fetch",
        "_stage_prepare",
        "_stage_transcribe_voxtral",
        "_stage_name_speakers",
        "_stage_chapterize",
    ):
        monkeypatch.setattr(f"pipeline.production.{name}", lambda *_args, **_kwargs: None)

    result_json = tmp_path / "success-cost.json"
    assert process_one(db_path, "m1", result_json=result_json) == 0

    row = db.get_meeting(db.connect(db_path), "m1")
    result = json.loads(result_json.read_text())
    assert row["cost_usd"] == 0.24
    assert result["row"]["cost_usd"] == 0.24
    assert "cost_usd=0.240000" in capsys.readouterr().out

    (meeting_dir / "name-speakers-meta.json").unlink()
    (meeting_dir / "name-speakers-chunk-1.json").write_text(json.dumps({"exact_cost_usd": 0.03}) + "\n")

    def fail_fetch(*_args, **_kwargs):
        raise RuntimeError("fetch exploded")

    monkeypatch.setattr("pipeline.production._stage_fetch", fail_fetch)

    assert process_one(db_path, "m1", result_json=tmp_path / "failed-cost.json") == 0
    failed = json.loads((tmp_path / "failed-cost.json").read_text())
    assert failed["row"]["cost_usd"] == 0.23
    assert db.get_meeting(db.connect(db_path), "m1")["cost_usd"] == 0.23


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
