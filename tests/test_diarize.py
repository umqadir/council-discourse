from __future__ import annotations

from pipeline.diarize import assign_labels_to_utterances, normalize_diarization_turns


def test_normalize_diarization_turns_uses_spk_labels_and_schema() -> None:
    turns = normalize_diarization_turns(
        [
            {"start": 4, "end": 8, "label": "speaker-b"},
            {"start": 0, "end": 3.25, "label": "speaker-a"},
            {"start": 9, "end": 9, "label": "invalid"},
            {"start": 8.5, "end": 9.5, "label": "speaker-a"},
        ]
    )

    assert turns == [
        {"start": 0.0, "end": 3.25, "label": "SPK_00"},
        {"start": 4.0, "end": 8.0, "label": "SPK_01"},
        {"start": 8.5, "end": 9.5, "label": "SPK_00"},
    ]
    assert set(turns[0]) == {"start", "end", "label"}


def test_assign_labels_by_max_overlap_then_nearest_midpoint() -> None:
    utterances = [
        {"t0": 1.0, "t1": 6.0, "text": "first"},
        {"t0": 9.5, "t1": 10.0, "text": "gap near second speaker"},
        {"t0": 18.0, "t1": 19.0, "text": "gap near third speaker"},
    ]
    turns = [
        {"start": 0.0, "end": 4.0, "label": "SPK_00"},
        {"start": 4.0, "end": 9.0, "label": "SPK_01"},
        {"start": 20.0, "end": 22.0, "label": "SPK_02"},
    ]

    labeled = assign_labels_to_utterances(utterances, turns)

    assert [row["label"] for row in labeled] == ["SPK_00", "SPK_01", "SPK_02"]
    assert labeled[0]["text"] == "first"
