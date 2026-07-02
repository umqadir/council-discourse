from __future__ import annotations

from pipeline.transcribe import _voxtral_result_to_rows


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
