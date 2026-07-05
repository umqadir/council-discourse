from __future__ import annotations

import pytest

from pipeline.export_site import normalize_summary_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The brief's canonical ASR artifact.
        ("The budget was $125. 8 billion this year.", "The budget was $125.8 billion this year."),
        ("It grew by 3. 5 percent.", "It grew by 3.5 percent."),
        ("reached 27. 2 million residents", "reached 27.2 million residents"),
        ("a 4. 5% increase", "a 4.5% increase"),
        # Stray space before the decimal point.
        ("$125 .8 billion allocated", "$125.8 billion allocated"),
        # Chained fragments collapse left to right.
        ("price of $1. 234. 5 billion", "price of $1.234.5 billion"),
        # Whitespace is collapsed and trimmed.
        ("  spaced   out  10. 25 percent  ", "spaced out 10.25 percent"),
    ],
)
def test_normalize_summary_text_repairs_decimal_splits(raw: str, expected: str) -> None:
    assert normalize_summary_text(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Genuine sentence boundary: "5 members" is not a numeric unit.
        "The vote was held in 2012. 5 members voted no.",
        # Ordinary sentence starting with a capitalized word.
        "Section 27. This applies to all committees.",
        # A number followed by a non-unit word must be left alone.
        "There were 12. 6 council members were present is wrong context.",
        # Prose without numbers is unchanged.
        "The committee discussed the outdoor dining program at length.",
        # A unit-like word that is only a longer word must not trigger a join.
        "the millionth visitor arrived",
    ],
)
def test_normalize_summary_text_preserves_real_sentence_boundaries(text: str) -> None:
    assert normalize_summary_text(text) == text


def test_normalize_summary_text_handles_empty_and_none() -> None:
    assert normalize_summary_text("") == ""
    assert normalize_summary_text(None) == ""  # type: ignore[arg-type]


def test_export_skips_meetings_without_local_artifacts_and_keeps_existing_json(tmp_path, monkeypatch):
    from pipeline import db
    from pipeline.export_site import export_site

    conn = db.connect(tmp_path / "registry.db")
    db.upsert_meeting(conn, {"meeting_key": "remote-only", "viebit_filename": "remote-only"})
    db.update_meeting(
        conn,
        "remote-only",
        {
            "transcribe_status": "transcribed",
            "name_speakers_status": "named",
            "chapterize_status": "chapterized",
        },
    )
    monkeypatch.setattr("pipeline.db.MEETINGS_DIR", tmp_path / "meetings")

    out_dir = tmp_path / "site-data"
    out_dir.mkdir()
    existing = out_dir / "previously-published.json"
    existing.write_text("{}\n")

    written = export_site(
        db_path=tmp_path / "registry.db",
        out_dir=out_dir,
        r2_out_dir=tmp_path / "r2-data",
        include_benchmark=False,
        allow_empty=True,
    )

    assert written == [existing]
    assert existing.exists()
