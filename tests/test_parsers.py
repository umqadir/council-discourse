from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import db
from pipeline.discover import discover_legistar, discover_viebit_rss
from pipeline.legistar import (
    extract_viebit_filename_from_insite_html,
    filename_matches_event,
    infer_meeting_type,
    meeting_slug,
    viebit_filename_from_url,
)
from pipeline.prepare import dedupe_rollup, parse_vtt
from pipeline.viebit import parse_filename_timestamp, parse_rss, room_prefix

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_viebit_rss_fixture() -> None:
    items = parse_rss((FIXTURES / "rss.xml").read_text())

    assert len(items) == 2
    assert items[0].filename == "NYCC-PV-CH-CHA_250423-100921"
    assert items[0].hash == "qFAxOQb56lhjkl8g"
    assert items[0].pub_date == "2025-04-23T18:42:00+00:00"


def test_extract_insite_video_filename() -> None:
    html = (FIXTURES / "meeting_detail.html").read_text()

    assert extract_viebit_filename_from_insite_html(html) == "NYCC-PV-CH-CHA_250423-100921"


def test_filename_timestamp_and_backstop_match() -> None:
    filename = "NYCC-PV-CH-CHA_250423-100921"

    assert parse_filename_timestamp(filename).isoformat() == "2025-04-23T10:09:21"
    assert room_prefix(filename) == "NYCC-PV-CH-CHA"
    assert filename_matches_event(
        filename,
        "2025-04-23T00:00:00",
        "10:00 AM",
        "Council Chambers - City Hall",
    )


def test_legistar_video_path_and_slug_helpers() -> None:
    url = "https://councilnyc.viebit.com/vod/?s=true&v=NYCC-PV-CH-CHA_260528-100627.mp4"

    assert viebit_filename_from_url(url) == "NYCC-PV-CH-CHA_260528-100627"
    assert (
        meeting_slug("2026-05-28T00:00:00", "10:00 AM", "Committee on Finance")
        == "2026-05-28-1000-am-committee-on-finance"
    )
    assert infer_meeting_type("City Council") == "STATED_MEETING"
    assert infer_meeting_type("Subcommittee on Zoning and Franchises") == "LAND_USE"
    assert infer_meeting_type("Committee on Transportation and Infrastructure") == "HEARING"


def test_vtt_rollup_dedupe(tmp_path: Path) -> None:
    vtt = tmp_path / "captions.vtt"
    vtt.write_text(
        """WEBVTT

00:00:01.000 --> 00:00:02.000
HELLO

00:00:02.000 --> 00:00:03.000
HELLO
WORLD

00:00:03.000 --> 00:00:04.000
WORLD
AGAIN
"""
    )

    assert dedupe_rollup(parse_vtt(vtt)) == [
        {"t": 1.0, "text": "HELLO"},
        {"t": 2.0, "text": "WORLD"},
        {"t": 3.0, "text": "AGAIN"},
    ]


def test_extract_event_topic_prefers_matter_name_and_filters_junk() -> None:
    from pipeline.legistar import extract_event_topic

    items = [
        {"EventItemAgendaSequence": 2, "EventItemMatterName": "Second item"},
        {"EventItemAgendaSequence": 1, "EventItemMatterName": "Executive Budget Hearings - Finance"},
    ]
    assert extract_event_topic(items) == "Executive Budget Hearings - Finance"

    # Falls back to the item title when the matter name is empty.
    titled = [{"EventItemAgendaSequence": 1, "EventItemTitle": "Oversight - Dining Out NYC.\nSecond line."}]
    assert extract_event_topic(titled) == "Oversight - Dining Out NYC. Second line"

    # Procedural boilerplate yields no topic.
    assert extract_event_topic([{"EventItemAgendaSequence": 1, "EventItemMatterName": "Agenda 1 p.m."}]) is None
    assert extract_event_topic([{"EventItemAgendaSequence": 1, "EventItemMatterName": "See Land Use Calendar"}]) is None
    assert extract_event_topic([{"EventItemAgendaSequence": 1, "EventItemMatterName": "Roll Call"}]) is None
    assert (
        extract_event_topic(
            [{"EventItemAgendaSequence": 1, "EventItemMatterName": "Committee on Finance"}],
            body_name="Committee on Finance",
        )
        is None
    )
    assert extract_event_topic([]) is None


def test_discover_viebit_rss_raises_when_feed_has_zero_items(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "registry.db")
    monkeypatch.setattr("pipeline.discover.fetch_rss", lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="Viebit RSS parsed to zero items"):
        discover_viebit_rss(conn)


def test_discover_legistar_missing_token_is_loud_in_github_actions(tmp_path: Path, monkeypatch, capsys) -> None:
    conn = db.connect(tmp_path / "registry.db")
    monkeypatch.delenv("LEGISTAR_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr("pipeline.discover.load_dotenv", lambda: None)

    assert discover_legistar(conn) == (0, True)
    assert "::error::LEGISTAR_TOKEN unset" in capsys.readouterr().err
