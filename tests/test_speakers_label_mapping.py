from __future__ import annotations

from pipeline.speakers import _extract_label_mapping_records, _extract_label_range_overrides, join_label_mappings


def test_extract_label_mapping_schema_normalizes_public_witnesses() -> None:
    mappings = _extract_label_mapping_records(
        {
            "labels": [
                {
                    "label": "SPK_00",
                    "name": "Jane Doe",
                    "role": "public witness",
                    "org": "Transit Riders",
                    "confidence": "high",
                }
            ]
        }
    )

    assert mappings == [
        {
            "label": "SPK_00",
            "name": "Jane Doe",
            "speaker": "Member of the Public - Jane Doe",
            "role": "public witness",
            "org": "Transit Riders",
            "confidence": 0.9,
            "confidence_label": "high",
            "reason": "",
        }
    ]


def test_join_label_mappings_preserves_named_utterance_schema_and_overrides() -> None:
    utterances = [
        {"t0": 0.0, "t1": 1.0, "text": "hello", "label": "SPK_00"},
        {"t0": 1.0, "t1": 2.0, "text": "roll call name", "label": "SPK_00"},
        {"t0": 2.0, "t1": 3.0, "text": "answer", "label": "SPK_01"},
    ]
    mappings = _extract_label_mapping_records(
        {
            "labels": [
                {"label": "SPK_00", "name": "Council Staff", "role": "Clerk", "confidence": "medium"},
                {"label": "SPK_01", "name": "Julie Menin", "role": "Council Member", "confidence": 0.8},
            ]
        }
    )
    overrides = _extract_label_range_overrides(
        {
            "range_overrides": [
                {
                    "start_index": 1,
                    "end_index": 1,
                    "name": "Selvena N. Brooks-Powers",
                    "role": "Council Member",
                    "confidence": "high",
                }
            ]
        }
    )

    named = join_label_mappings(utterances, mappings, overrides)

    assert [set(row) for row in named] == [
        {"t0", "t1", "text", "speaker", "confidence"},
        {"t0", "t1", "text", "speaker", "confidence"},
        {"t0", "t1", "text", "speaker", "confidence"},
    ]
    assert [row["speaker"] for row in named] == [
        "Council Staff",
        "Selvena N. Brooks-Powers",
        "Julie Menin",
    ]
    assert [row["confidence"] for row in named] == [0.65, 0.9, 0.8]
