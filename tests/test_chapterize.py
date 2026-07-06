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
