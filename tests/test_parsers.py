from __future__ import annotations

from pathlib import Path

from pipeline.legistar import extract_viebit_filename_from_insite_html, filename_matches_event
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
