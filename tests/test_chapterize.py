from __future__ import annotations

import json

import pytest

from pipeline.chapterize import (
    _chapter_count_floor,
    _coarse_retry_note,
    _split_serial_voice_votes,
    chapterize_meeting,
)
from pipeline.models import Meeting


def test_split_serial_voice_votes_handles_generic_vote_parent() -> None:
    chapters = [
        {
            "start": "1:22:04",
            "start_sec": 4924.0,
            "end_sec": 5026.0,
            "type": "VOTE",
            "title": "Voice Votes on Today's Resolutions",
            "summary": "The Council passed multiple resolutions by voice vote.",
        }
    ]
    utterances = [
        {"t0": 4938.0, "text": "Resolution 8 calls on the federal government to fund lead service line replacement."},
        {"t0": 4962.0, "text": "Resolution 85-A calls for a noise tax on non-essential helicopter flights."},
        {"t0": 4982.0, "text": "Resolution 144A would support water infrastructure funding upgrades."},
    ]

    split = _split_serial_voice_votes(chapters, utterances)

    assert [chapter["start_sec"] for chapter in split] == [4938.0, 4962.0, 4982.0]
    assert [chapter["type"] for chapter in split] == ["VOICE_VOTE", "VOICE_VOTE", "VOICE_VOTE"]
    assert split[0]["title"] == "Voice Vote on Resolution 8: the federal government to fund lead service line replacement"
    assert split[1]["title"] == "Voice Vote on Resolution 85-A: a noise tax on non-essential helicopter flights"
    assert split[2]["title"] == "Voice Vote on Resolution 144A: support water infrastructure funding upgrades"


def test_split_serial_voice_votes_keeps_single_vote_parent() -> None:
    chapters = [
        {
            "start": "1:22:04",
            "start_sec": 4924.0,
            "end_sec": 4950.0,
            "type": "VOTE",
            "title": "Vote on Resolution 8",
            "summary": "The Council voted on Resolution 8.",
        }
    ]
    utterances = [{"t0": 4938.0, "text": "Resolution 8 calls on the federal government to fund lead service lines."}]

    assert _split_serial_voice_votes(chapters, utterances) == chapters


def test_coarse_retry_note_uses_duration_based_floor() -> None:
    assert _chapter_count_floor("HEARING", 4 * 3600) == 80
    assert _chapter_count_floor("STATED_MEETING", 5400) == 45
    assert _coarse_retry_note("HEARING", 55, 4 * 3600)
    assert _coarse_retry_note("HEARING", 82, 4 * 3600) is None


def test_chapterize_reuses_valid_cached_output_without_paid_call(tmp_path, monkeypatch) -> None:
    chapters = tmp_path / "chapters.json"
    chapters.write_text(
        json.dumps(
            {
                "model": "cached",
                "elapsed_sec": 0.0,
                "usage": {},
                "chapters": [
                    {
                        "start": "0:00:00",
                        "start_sec": 0.0,
                        "end_sec": 1.0,
                        "type": "REMARKS",
                        "title": "Opening",
                        "summary": "The meeting opens.",
                    }
                ],
            }
        )
        + "\n"
    )
    meeting = Meeting(meeting_key="m1", meeting_dir=tmp_path)

    def fail_generate_json(*_args, **_kwargs):
        raise AssertionError("cache hit should not call the chaptering LLM")

    monkeypatch.setattr("pipeline.chapterize.generate_json", fail_generate_json)

    assert chapterize_meeting(meeting, write_runlog=False) == (
        str(chapters),
        str(tmp_path / "meeting-derived.json"),
    )


def test_chapterize_rejects_oversized_prompt_before_paid_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COUNCIL_CHAPTER_MAX_PROMPT_TOKENS", "10")
    (tmp_path / "utterances-named.jsonl").write_text(
        json.dumps({"t0": 0.0, "t1": 1.0, "text": "This transcript text is intentionally long.", "speaker": "Speaker"})
        + "\n"
    )
    meeting = Meeting(meeting_key="m1", meeting_dir=tmp_path)

    def fail_generate_json(*_args, **_kwargs):
        raise AssertionError("oversized prompt should fail before the chaptering LLM")

    monkeypatch.setattr("pipeline.chapterize.generate_json", fail_generate_json)

    with pytest.raises(RuntimeError, match=r"transcript too long for chaptering: ~\d+k tokens"):
        chapterize_meeting(meeting, write_runlog=False)


def test_chapterize_retries_once_when_gemini_returns_no_chapters(tmp_path, monkeypatch) -> None:
    (tmp_path / "utterances-named.jsonl").write_text(
        json.dumps({"t0": 0.0, "t1": 5.0, "text": "The meeting will come to order.", "speaker": "Chair"}) + "\n"
    )
    meeting = Meeting(meeting_key="m1", meeting_dir=tmp_path, duration_seconds=60.0)
    responses = [
        {"chapters": []},
        {"tags": ["HEARING"], "chapters": [{"start": "0:00:00", "start_index": 0, "type": "REMARKS", "title": "Opening", "summary": "Opens."}]},
    ]
    calls = []

    def fake_generate_json(*_args, **_kwargs):
        calls.append(1)
        return responses[len(calls) - 1], {"elapsed_sec": 0.0, "usage": {}}

    monkeypatch.setattr("pipeline.chapterize.generate_json", fake_generate_json)
    monkeypatch.setattr("pipeline.chapterize._coarse_retry_note", lambda *_a: None)

    chapters_path, _ = chapterize_meeting(meeting, write_runlog=False)
    assert len(calls) == 2
    assert json.loads(open(chapters_path).read())["chapters"][0]["title"] == "Opening"


def _hour_meeting(tmp_path, minutes: int = 89):
    rows = [
        {"t0": float(i * 60), "t1": float(i * 60 + 50), "text": f"Remarks number {i}.", "speaker": "Chair"}
        for i in range(minutes)
    ]
    (tmp_path / "utterances-named.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return Meeting(meeting_key="m1", meeting_dir=tmp_path, duration_seconds=minutes * 60.0,
                   body_name="Committee on Mental Health")


def _chapters_response(count: int, spacing_min: int = 5) -> dict:
    return {
        "chapters": [
            {"start_index": i * spacing_min, "type": "REMARKS", "title": f"Part {i + 1}", "summary": "..."}
            for i in range(count)
        ]
    }


def test_failed_quality_gate_call_resumes_from_saved_first_pass(tmp_path, monkeypatch) -> None:
    from pipeline.chapterize import FIRST_PASS_CHECKPOINT

    meeting = _hour_meeting(tmp_path)
    calls = []

    def first_run(prompt, **_kwargs):
        calls.append("QUALITY GATE" in prompt)
        if len(calls) == 1:
            return _chapters_response(8), {"elapsed_sec": 1.0, "usage": {}, "estimated_cost_usd": 0.05}
        raise RuntimeError("OpenAI-compatible model failed: 503")

    monkeypatch.setattr("pipeline.chapterize.generate_json", first_run)
    with pytest.raises(RuntimeError, match="503"):
        chapterize_meeting(meeting, write_runlog=False)
    assert calls == [False, True]
    checkpoint = tmp_path / FIRST_PASS_CHECKPOINT
    assert json.loads(checkpoint.read_text())["estimated_cost_usd"] == 0.05
    assert not (tmp_path / "chapters.json").exists()

    retry_calls = []

    def second_run(prompt, **_kwargs):
        retry_calls.append("QUALITY GATE" in prompt)
        return _chapters_response(17), {"elapsed_sec": 1.0, "usage": {}, "estimated_cost_usd": 0.06}

    monkeypatch.setattr("pipeline.chapterize.generate_json", second_run)
    chapters_path, _ = chapterize_meeting(meeting, write_runlog=False)
    assert retry_calls == [True]  # the paid first pass was not repeated
    payload = json.loads(open(chapters_path).read())
    assert len(payload["chapters"]) == 17
    assert payload["resumed_first_pass"] is True
    assert payload["estimated_cost_usd"] == 0.11
    assert not checkpoint.exists()


def test_first_pass_checkpoint_is_ignored_when_the_prompt_changes(tmp_path, monkeypatch) -> None:
    from pipeline.chapterize import FIRST_PASS_CHECKPOINT

    meeting = _hour_meeting(tmp_path)
    (tmp_path / FIRST_PASS_CHECKPOINT).write_text(json.dumps(
        {"prompt_sha256": "stale", "result": _chapters_response(8), "metas": [{"estimated_cost_usd": 1.0}]}) + "\n")
    calls = []

    def generate(prompt, **_kwargs):
        calls.append("QUALITY GATE" in prompt)
        return _chapters_response(17), {"elapsed_sec": 1.0, "usage": {}, "estimated_cost_usd": 0.02}

    monkeypatch.setattr("pipeline.chapterize.generate_json", generate)
    chapterize_meeting(meeting, write_runlog=False)
    assert calls[0] is False  # a fresh first pass


def test_hour_long_meeting_with_one_chapter_fails_instead_of_publishing(tmp_path, monkeypatch) -> None:
    from pipeline.chapterize import DegenerateChaptersError, FIRST_PASS_CHECKPOINT

    meeting = _hour_meeting(tmp_path, minutes=89)
    calls = []

    def generate(prompt, **_kwargs):
        calls.append("QUALITY GATE" in prompt)
        return _chapters_response(1), {"elapsed_sec": 1.0, "usage": {}, "estimated_cost_usd": 0.01}

    monkeypatch.setattr("pipeline.chapterize.generate_json", generate)
    with pytest.raises(DegenerateChaptersError, match="1 chapter"):
        chapterize_meeting(meeting, write_runlog=False)
    assert calls == [False, True]
    assert not (tmp_path / "chapters.json").exists()
    assert not (tmp_path / "meeting-derived.json").exists()
    assert (tmp_path / FIRST_PASS_CHECKPOINT).exists()


def test_degenerate_check_applies_to_types_without_a_count_floor(tmp_path, monkeypatch) -> None:
    import dataclasses

    meeting = dataclasses.replace(_hour_meeting(tmp_path, minutes=70), meeting_type="LAND_USE")
    responses = iter([_chapters_response(2), _chapters_response(6)])
    monkeypatch.setattr("pipeline.chapterize.generate_json",
                        lambda *_a, **_k: (next(responses), {"elapsed_sec": 0.0, "usage": {}}))
    chapters_path, _ = chapterize_meeting(meeting, write_runlog=False)
    assert len(json.loads(open(chapters_path).read())["chapters"]) == 6

    short = Meeting(meeting_key="m2", meeting_dir=tmp_path / "short", duration_seconds=50 * 60.0, meeting_type="LAND_USE")
    short.meeting_dir.mkdir()
    (short.meeting_dir / "utterances-named.jsonl").write_text(
        json.dumps({"t0": 0.0, "t1": 5.0, "text": "Opening.", "speaker": "Chair"}) + "\n")
    monkeypatch.setattr("pipeline.chapterize.generate_json",
                        lambda *_a, **_k: (_chapters_response(1, spacing_min=0), {"elapsed_sec": 0.0, "usage": {}}))
    chapterize_meeting(short, write_runlog=False)  # under an hour: one chapter is allowed
    assert len(json.loads((short.meeting_dir / "chapters.json").read_text())["chapters"]) == 1


def test_degenerate_cached_chapters_are_not_reused(tmp_path, monkeypatch) -> None:
    meeting = _hour_meeting(tmp_path, minutes=89)
    (tmp_path / "chapters.json").write_text(json.dumps({"model": "cached", "chapters": [
        {"start": "0:00:00", "start_sec": 0.0, "end_sec": 5340.0, "type": "REMARKS", "title": "Everything", "summary": ""}]}) + "\n")
    monkeypatch.setattr("pipeline.chapterize.generate_json",
                        lambda *_a, **_k: (_chapters_response(17), {"elapsed_sec": 0.0, "usage": {}}))
    chapters_path, _ = chapterize_meeting(meeting, write_runlog=False)
    assert len(json.loads(open(chapters_path).read())["chapters"]) == 17
