from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import db
from pipeline.artifacts import read_json, read_jsonl
from pipeline.config import VOXTRAL_USD_PER_AUDIO_HOUR, voxtral_mode
from pipeline.models import Meeting
from pipeline.production import _voxtral_cost_usd, process_one
from pipeline.voxtral_prod import (
    VOXTRAL_BATCH_ENDPOINT,
    VOXTRAL_BATCH_JOB_FILENAME,
    VoxtralBatchPending,
    _batch_request_params_hash,
    _run_voxtral_batch_job,
    _transcribe_voxtral_parts_batch,
    transcribe_voxtral_production,
)


def _part(path: Path, index: int = 1) -> dict[str, Any]:
    path.write_bytes(b"0" * 10_001)
    return {"index": index, "path": path, "offset_sec": float((index - 1) * 10), "speaker_suffix": "" if index == 1 else f"_part{index}"}


def _transcript(text: str, *, speaker: str = "speaker_0") -> dict[str, Any]:
    return {
        "text": text,
        "segments": [{"text": text, "start": 0.0, "end": 1.0, "speaker_id": speaker}],
        "usage": {"prompt_audio_seconds": 1.0},
    }


def test_batch_job_creation_payload_persists_before_poll_and_writes_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setenv("VOXTRAL_BATCH_POLL_INTERVAL_SEC", "0")
    monkeypatch.setattr("pipeline.voxtral_prod.time.sleep", lambda _delay: None)
    meeting_dir = tmp_path / "meeting"
    meeting_dir.mkdir()
    parts = [_part(meeting_dir / "part-1.m4a", 1), _part(meeting_dir / "part-2.m4a", 2)]
    posts: list[tuple[str, dict[str, Any]]] = []
    job_get_checked_state = False

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        posts.append((url, kwargs))
        if url.endswith("/files"):
            upload_index = len([item for item in posts if item[0].endswith("/files")])
            return httpx.Response(200, json={"id": f"file-{upload_index}"})
        assert url.endswith("/batch/jobs")
        return httpx.Response(200, json={"id": "job-1", "status": "RUNNING", "total_requests": 2})

    def fake_get(url: str, **_kwargs: Any) -> httpx.Response:
        nonlocal job_get_checked_state
        if url.endswith("/batch/jobs/job-1"):
            assert (meeting_dir / VOXTRAL_BATCH_JOB_FILENAME).exists()
            job_get_checked_state = True
            return httpx.Response(200, json={"id": "job-1", "status": "SUCCESS", "output_file": "out-1"})
        assert url.endswith("/files/out-1/content")
        content = "\n".join(
            [
                json.dumps({"custom_id": "1", "response": {"status_code": 200, "body": _transcript("One.")}}),
                json.dumps({"custom_id": "2", "response": {"status_code": 200, "body": _transcript("Two.")}}),
            ]
        )
        return httpx.Response(200, content=(content + "\n").encode())

    monkeypatch.setattr("pipeline.voxtral_prod.httpx.post", fake_post)
    monkeypatch.setattr("pipeline.voxtral_prod.httpx.get", fake_get)

    _run_voxtral_batch_job(
        meeting_dir,
        parts,
        "voxtral-mini-2602",
        ["Julie Menin", "DOT"],
        meeting_key="m1",
    )

    assert job_get_checked_state is True
    create_payload = posts[-1][1]["json"]
    assert create_payload["endpoint"] == VOXTRAL_BATCH_ENDPOINT
    assert create_payload["model"] == "voxtral-mini-2602"
    assert create_payload["input_files"] == ["file-3"]
    batch_upload = posts[2][1]["files"]["file"]
    rows = [json.loads(line) for line in batch_upload[1].decode().splitlines()]
    assert [row["custom_id"] for row in rows] == ["1", "2"]
    assert rows[0]["body"] == {
        "context_bias": ["Julie-Menin", "DOT"],
        "diarize": True,
        "file_id": "file-1",
        "timestamp_granularities": ["segment"],
    }
    state = read_json(meeting_dir / VOXTRAL_BATCH_JOB_FILENAME)
    assert state["job_id"] == "job-1"
    assert state["input_file_ids"] == ["file-3"]
    assert sorted(state["chunk_custom_id_map"]) == ["1", "2"]
    assert read_json(meeting_dir / "voxtral-transcript-part-1.json")["text"] == "One."
    assert read_json(meeting_dir / "voxtral-transcript-part-2.json")["segments"][0]["speaker_id"] == "speaker_0"


def test_batch_reattaches_existing_job_without_resubmitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    meeting_dir = tmp_path / "meeting"
    meeting_dir.mkdir()
    parts = [_part(meeting_dir / "part-1.m4a", 1)]
    params_hash = _batch_request_params_hash(parts, "voxtral-mini-2602", [])
    (meeting_dir / VOXTRAL_BATCH_JOB_FILENAME).write_text(
        json.dumps({"job_id": "job-existing", "request_params_hash": params_hash}) + "\n"
    )

    def fake_post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise AssertionError("reattach must not create a new job")

    def fake_get(url: str, **_kwargs: Any) -> httpx.Response:
        if url.endswith("/batch/jobs/job-existing"):
            return httpx.Response(200, json={"id": "job-existing", "status": "SUCCESS", "output_file": "out-1"})
        assert url.endswith("/files/out-1/content")
        return httpx.Response(
            200,
            content=(json.dumps({"custom_id": "1", "response": {"status_code": 200, "body": _transcript("Done.")}}) + "\n").encode(),
        )

    monkeypatch.setattr("pipeline.voxtral_prod.httpx.post", fake_post)
    monkeypatch.setattr("pipeline.voxtral_prod.httpx.get", fake_get)

    _run_voxtral_batch_job(meeting_dir, parts, "voxtral-mini-2602", [], meeting_key="m1")

    assert read_json(meeting_dir / "voxtral-transcript-part-1.json")["text"] == "Done."


def test_batch_params_hash_mismatch_resubmits_and_overwrites_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    meeting_dir = tmp_path / "meeting"
    meeting_dir.mkdir()
    parts = [_part(meeting_dir / "part-1.m4a", 1)]
    (meeting_dir / VOXTRAL_BATCH_JOB_FILENAME).write_text(
        json.dumps({"job_id": "old-job", "request_params_hash": "stale"}) + "\n"
    )
    post_count = 0

    def fake_post(url: str, **_kwargs: Any) -> httpx.Response:
        nonlocal post_count
        post_count += 1
        if url.endswith("/files"):
            return httpx.Response(200, json={"id": f"new-file-{post_count}"})
        return httpx.Response(200, json={"id": "new-job", "status": "SUCCESS", "output_file": "out-1"})

    def fake_get(url: str, **_kwargs: Any) -> httpx.Response:
        assert url.endswith("/files/out-1/content")
        return httpx.Response(
            200,
            content=(json.dumps({"custom_id": "1", "response": {"status_code": 200, "body": _transcript("New.")}}) + "\n").encode(),
        )

    monkeypatch.setattr("pipeline.voxtral_prod.httpx.post", fake_post)
    monkeypatch.setattr("pipeline.voxtral_prod.httpx.get", fake_get)

    _run_voxtral_batch_job(meeting_dir, parts, "voxtral-mini-2602", [], meeting_key="m1")

    assert post_count == 3
    assert read_json(meeting_dir / VOXTRAL_BATCH_JOB_FILENAME)["job_id"] == "new-job"


def test_completed_batch_merges_part_files_like_sync_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    meeting_dir = tmp_path / "meeting"
    meeting_dir.mkdir()
    parts = [_part(meeting_dir / "part-1.m4a", 1), _part(meeting_dir / "part-2.m4a", 2)]

    def fake_post(url: str, **_kwargs: Any) -> httpx.Response:
        if url.endswith("/files"):
            return httpx.Response(200, json={"id": f"file-{fake_post.calls}"})
        return httpx.Response(200, json={"id": "job-1", "status": "SUCCESS", "output_file": "out-1"})

    fake_post.calls = 0  # type: ignore[attr-defined]

    def counted_post(url: str, **kwargs: Any) -> httpx.Response:
        fake_post.calls += 1  # type: ignore[attr-defined]
        return fake_post(url, **kwargs)

    def fake_get(url: str, **_kwargs: Any) -> httpx.Response:
        assert url.endswith("/files/out-1/content")
        content = "\n".join(
            [
                json.dumps({"custom_id": "1", "response": {"status_code": 200, "body": _transcript("One.", speaker="speaker_a")}}),
                json.dumps({"custom_id": "2", "response": {"status_code": 200, "body": _transcript("Two.", speaker="speaker_b")}}),
            ]
        )
        return httpx.Response(200, content=(content + "\n").encode())

    monkeypatch.setattr("pipeline.voxtral_prod.httpx.post", counted_post)
    monkeypatch.setattr("pipeline.voxtral_prod.httpx.get", fake_get)

    utterances, labeled, merged, _records = _transcribe_voxtral_parts_batch(
        meeting_dir,
        parts,
        "voxtral-mini-2602",
        [],
        meeting_key="m1",
    )

    assert utterances == [{"t0": 0.0, "t1": 1.0, "text": "One."}, {"t0": 10.0, "t1": 11.0, "text": "Two."}]
    assert [row["label"] for row in labeled] == ["speaker_a", "speaker_b_part2"]
    assert merged["text"] == "One.\nTwo."
    assert read_json(meeting_dir / "voxtral-transcript-part-2.json") == _transcript("Two.", speaker="speaker_b")


def test_voxtral_mode_default_sync_until_batch_diarizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cost-regression pin, inverted with cause: Mistral's batch endpoint would
    # halve ASR cost but silently drops diarization (verified live 2026-07-06),
    # and speaker labels are a hard quality gate. Sync stays the default; the
    # cost constant must reflect the rate we actually pay. If this test is
    # being changed to make batch the default, first verify diarize works on
    # the batch endpoint with a real clip.
    monkeypatch.delenv("COUNCIL_VOXTRAL_MODE", raising=False)
    assert voxtral_mode() == "sync"
    assert VOXTRAL_USD_PER_AUDIO_HOUR == 0.18
    monkeypatch.setenv("COUNCIL_VOXTRAL_MODE", "batch")
    assert voxtral_mode() == "batch"
    monkeypatch.setattr("pipeline.transcribe.current_roster", lambda _date: [])
    meeting_dir = tmp_path / "meeting"
    meeting_dir.mkdir()
    (meeting_dir / "audio.m4a").write_bytes(b"0" * 10_001)
    meeting = Meeting(meeting_key="m1", meeting_dir=meeting_dir, duration_seconds=1.0)
    calls: list[str] = []

    def fake_batch(_meeting_dir, _parts, _model, _context_bias, *, meeting_key):
        calls.append(f"batch:{meeting_key}")
        return (
            [{"t0": 0.0, "t1": 1.0, "text": "Hello."}],
            [{"t0": 0.0, "t1": 1.0, "text": "Hello.", "label": "speaker_0"}],
            {"text": "Hello.", "segments": [{"text": "Hello.", "start": 0.0, "end": 1.0, "speaker_id": "speaker_0"}], "usage": {}},
            [],
        )

    monkeypatch.setattr("pipeline.voxtral_prod._transcribe_voxtral_parts_batch", fake_batch)

    transcribe_voxtral_production(meeting)

    assert calls == ["batch:m1"]
    assert read_json(meeting_dir / "transcribe-meta.json")["mode"] == "batch"

    for name in ("utterances.jsonl", "utterances-labeled.jsonl", "transcribe-meta.json", "voxtral-transcript.json"):
        (meeting_dir / name).unlink()
    monkeypatch.setenv("COUNCIL_VOXTRAL_MODE", "sync")

    def fake_sync(_audio: Path, _model: str, _bias: list[str]):
        return _transcript("Sync."), {"wall_clock_sec": 0.01, "http_status": 200, "attempts": 1}

    transcribe_voxtral_production(meeting, request_func=fake_sync)

    assert read_json(meeting_dir / "transcribe-meta.json")["mode"] == "sync"
    assert read_jsonl(meeting_dir / "utterances.jsonl")[0]["text"] == "Sync."


def test_voxtral_sync_cost_uses_sync_rate(tmp_path: Path) -> None:
    meta = tmp_path / "transcribe-meta.json"
    meta.write_text(json.dumps({"audio_duration_sec": 7200, "mode": "sync"}) + "\n")

    assert _voxtral_cost_usd(meta) == 0.36


def test_process_one_batch_pending_is_not_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    db.upsert_meeting(conn, {"meeting_key": "m1", "viebit_filename": "m1"})
    db.update_meeting(conn, "m1", {"process_attempts": 2})
    monkeypatch.setattr("pipeline.production._reconcile_artifacts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("pipeline.production._stage_fetch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("pipeline.production._stage_prepare", lambda *_args, **_kwargs: None)

    def pending_transcribe(*_args: Any, **_kwargs: Any) -> None:
        raise VoxtralBatchPending("job-1", "RUNNING", 1.0)

    monkeypatch.setattr("pipeline.production._stage_transcribe_voxtral", pending_transcribe)
    result_json = tmp_path / "result.json"

    assert process_one(db_path, "m1", result_json=result_json, fail_on_error=True) == 0

    row = db.get_meeting(db.connect(db_path), "m1")
    result = json.loads(result_json.read_text())
    assert result["status"] == "pending"
    assert "job-1" in result["note"]
    assert row["process_attempts"] == 2
    assert row["last_error"] is None
