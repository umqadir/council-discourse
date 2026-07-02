from __future__ import annotations

from pipeline.models import Meeting
from pipeline.speakers import (
    _apply_candidate_org_anchors,
    _apply_name_spelling_anchors,
    _extract_label_mapping_records,
    _extract_label_range_overrides,
    _spelling_anchor_sets,
    join_label_mappings,
)


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


def test_extract_label_mapping_unwraps_single_object_list_response() -> None:
    mappings = _extract_label_mapping_records(
        [
            {
                "labels": [
                    {
                        "label": "A",
                        "name": "Julie Menin",
                        "role": "Council Member",
                        "confidence": "high",
                    }
                ]
            }
        ]
    )
    overrides = _extract_label_range_overrides(
        [
            {
                "range_overrides": [
                    {
                        "start_index": 2,
                        "end_index": 3,
                        "name": "Council Staff",
                        "confidence": "medium",
                    }
                ]
            }
        ]
    )

    assert mappings[0]["label"] == "A"
    assert mappings[0]["speaker"] == "Julie Menin"
    assert overrides[0]["speaker"] == "Council Staff"


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


def test_name_spelling_anchors_snap_roster_and_legistar_names_without_public_names(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.speakers.current_roster",
        lambda _date: [{"name": "Julie Menin"}],
    )
    (tmp_path / "official-transcript.txt").write_text(
        """
        A P P E A R A N C E S

        Margaret Forgione
        First Deputy Commissioner NYC Department of Transportation
        """
    )
    meeting = Meeting(
        meeting_key="m1",
        meeting_dir=tmp_path,
        event_date="2025-04-23",
    )
    named = [
        {"t0": 0.0, "t1": 1.0, "text": "hello", "speaker": "Julie Menon", "confidence": 0.8},
        {"t0": 1.0, "t1": 2.0, "text": "hello", "speaker": "Margaret Forgioni", "confidence": 0.8},
        {"t0": 2.0, "t1": 3.0, "text": "hello", "speaker": "Member of the Public - Sarah Lind", "confidence": 0.8},
    ]

    name_anchors, _, _ = _spelling_anchor_sets(meeting)
    corrections = _apply_name_spelling_anchors(named, name_anchors)

    assert [row["speaker"] for row in named] == [
        "Julie Menin",
        "Margaret Forgione",
        "Member of the Public - Sarah Lind",
    ]
    assert [(item["before"], item["after"]) for item in corrections] == [
        ("Julie Menon", "Julie Menin"),
        ("Margaret Forgioni", "Margaret Forgione"),
    ]


def test_org_spelling_anchors_snap_short_org_hints() -> None:
    candidates = [{"id": "v001", "role_org_hint": "New York City Department of Transportion"}]

    corrections = _apply_candidate_org_anchors(candidates, ["New York City Department of Transportation"])

    assert candidates[0]["role_org_hint"] == "New York City Department of Transportation"
    assert corrections == [
        {
            "id": "v001",
            "before": "New York City Department of Transportion",
            "after": "New York City Department of Transportation",
            "confidence": "deterministic-anchor",
            "method": "org_fuzzy_match",
        }
    ]
