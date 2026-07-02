from __future__ import annotations

from pathlib import Path

from pipeline.models import Meeting
from pipeline.transcribe import (
    VOXTRAL_CONTEXT_BIAS_PARAM,
    _voxtral_context_bias_for_meeting,
    _voxtral_request_form_data,
    _voxtral_result_to_rows,
)


def test_voxtral_rows_offset_and_suffix_split_part_speakers() -> None:
    utterances, labeled, merged = _voxtral_result_to_rows(
        {
            "segments": [
                {
                    "text": " hello ",
                    "start": 1.25,
                    "end": 2.5,
                    "speaker_id": "speaker_7",
                    "type": "transcription_segment",
                },
                {"text": "   ", "start": 3.0, "end": 4.0, "speaker_id": "speaker_8"},
                {"text": "bad timing", "start": 5.0, "end": 5.0, "speaker_id": "speaker_9"},
            ]
        },
        offset_sec=10.0,
        speaker_suffix="_part2",
        part_index=2,
    )

    assert utterances == [{"t0": 11.25, "t1": 12.5, "text": "hello"}]
    assert labeled == [
        {
            "t0": 11.25,
            "t1": 12.5,
            "text": "hello",
            "label": "speaker_7_part2",
            "speaker_id": "speaker_7",
            "voxtral_part": 2,
        }
    ]
    assert merged == [
        {
            "text": "hello",
            "start": 11.25,
            "end": 12.5,
            "speaker_id": "speaker_7_part2",
            "raw_speaker_id": "speaker_7",
            "part": 2,
            "type": "transcription_segment",
        }
    ]


def test_voxtral_context_bias_uses_roster_committee_and_agency_terms(monkeypatch) -> None:
    monkeypatch.delenv("VOXTRAL_CONTEXT_BIAS", raising=False)
    monkeypatch.setattr(
        "pipeline.transcribe.current_roster",
        lambda _date: [{"name": "Julie Menin"}, {"name": "Amanda C. Farias"}],
    )
    meeting = Meeting(
        meeting_key="m1",
        meeting_dir=Path("unused"),
        body_name="Committee on Transportation and Infrastructure (joint with Consumer and Worker Protection)",
        event_date="2025-04-23",
    )

    terms, meta = _voxtral_context_bias_for_meeting(meeting)

    assert meta["param"] == "context_bias"
    assert len(terms) <= 100
    assert all(" " not in term and "," not in term for term in terms)
    assert "Julie-Menin" in terms
    assert "Amanda-C.-Farias" in terms
    assert "Amanda-Farias" in terms
    assert "Committee-on-Transportation-and-Infrastructure" in terms
    assert "Committee-on-Consumer-and-Worker-Protection" in terms
    assert "DOT" in terms
    assert "DCWP" in terms


def test_voxtral_request_form_repeats_context_bias_fields() -> None:
    form = _voxtral_request_form_data("voxtral-mini-2602", ["Julie Menin", "DOT"])

    assert form["model"] == "voxtral-mini-2602"
    assert form["diarize"] == "true"
    assert form["timestamp_granularities"] == ["segment"]
    assert form[VOXTRAL_CONTEXT_BIAS_PARAM] == "Julie-Menin,DOT"
