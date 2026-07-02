from __future__ import annotations

from pathlib import Path

from pipeline.models import Meeting
from pipeline.transcribe import (
    VOXTRAL_CONTEXT_BIAS_PARAM,
    _assemblyai_result_to_rows,
    _scribe_result_to_rows,
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


def test_scribe_rows_derive_segments_from_words_and_suffix_split_speakers() -> None:
    utterances, labeled, merged, words = _scribe_result_to_rows(
        {
            "text": "Hello there. Next speaker.",
            "words": [
                {"text": "Hello", "start": 0.1, "end": 0.4, "type": "word", "speaker_id": "speaker_0"},
                {"text": "there.", "start": 0.45, "end": 0.8, "type": "word", "speaker_id": "speaker_0"},
                {"text": "Next", "start": 1.5, "end": 1.8, "type": "word", "speaker_id": "speaker_1"},
                {"text": "speaker.", "start": 1.85, "end": 2.2, "type": "word", "speaker_id": "speaker_1"},
            ],
        },
        offset_sec=10.0,
        speaker_suffix="_part2",
        part_index=2,
    )

    assert utterances == [
        {"t0": 10.1, "t1": 10.8, "text": "Hello there."},
        {"t0": 11.5, "t1": 12.2, "text": "Next speaker."},
    ]
    assert [row["label"] for row in labeled] == ["speaker_0_part2", "speaker_1_part2"]
    assert [row["speaker_id"] for row in labeled] == ["speaker_0", "speaker_1"]
    assert merged[0]["source"] == "derived_from_words"
    assert words[0]["start"] == 10.1
    assert words[0]["speaker_id"] == "speaker_0_part2"
    assert words[0]["raw_speaker_id"] == "speaker_0"


def test_assemblyai_rows_use_diarized_utterances_with_millis_timestamps() -> None:
    utterances, labeled, merged, words = _assemblyai_result_to_rows(
        {
            "utterances": [
                {
                    "text": "Good morning.",
                    "start": 1000,
                    "end": 2500,
                    "speaker": "A",
                    "confidence": 0.91,
                    "words": [
                        {"text": "Good", "start": 1000, "end": 1300, "speaker": "A"},
                        {"text": "morning.", "start": 1400, "end": 2500, "speaker": "A"},
                    ],
                }
            ],
            "words": [{"text": "Good", "start": 1000, "end": 1300, "speaker": "A"}],
        },
        offset_sec=5.0,
        speaker_suffix="_part2",
        part_index=2,
    )

    assert utterances == [{"t0": 6.0, "t1": 7.5, "text": "Good morning."}]
    assert labeled == [
        {
            "t0": 6.0,
            "t1": 7.5,
            "text": "Good morning.",
            "label": "A_part2",
            "speaker_id": "A",
            "assemblyai_part": 2,
            "confidence": 0.91,
            "word_count": 2,
        }
    ]
    assert merged[0]["speaker"] == "A_part2"
    assert merged[0]["raw_speaker"] == "A"
    assert words[0]["start"] == 6.0
    assert words[0]["speaker"] == "A_part2"
