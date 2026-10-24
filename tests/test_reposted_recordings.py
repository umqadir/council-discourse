"""Fetching and guarding the recordings the clerk re-posted on 2026-10-23
(body-named, no WebVTT, COM_ titles served under CHA_ asset names)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pipeline import db, production
from pipeline.discover import detach_event, discover_viebit_rss, match_recordings
from pipeline.legistar import LegistarEvent
from pipeline.matching import SHORT_RECORDING_HOLD_PREFIX, pairing_source, post_fetch_hold_note
from pipeline.prepare import prepare_meeting
from pipeline.production import process_one, select_process_candidates, verify_run_results
from pipeline.viebit import parse_rss

# Three items from https://councilnyc.viebit.com/rss.xml as of 2026-10-24.
RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:media="http://search.yahoo.com/mrss/" version="2.0"><channel>
<item>
  <title>NYCC-PV-CH-COM_261022-Parks.mp4</title>
  <media:content url="https://vbfast-vod.viebit.com/counciln/W3zQEbqDf9F11iwB/NYCC-PV-CH-CHA_261022-Parks.jpg?v=0" medium="image" />
  <guid isPermaLink="false">W3zQEbqDf9F11iwB</guid>
  <pubDate>Fri, 23 Oct 2026 16:00:21 +0000</pubDate>
</item>
<item>
  <title>NYCC-PV-CH-CHA_261022-Education.mp4</title>
  <media:content url="https://vbfast-vod.viebit.com/counciln/1LZiRuWhnanTEcM6/NYCC-PV-CH-CHA_261022-Education.jpg?v=0" medium="image" />
  <guid isPermaLink="false">1LZiRuWhnanTEcM6</guid>
  <pubDate>Fri, 23 Oct 2026 16:00:47 +0000</pubDate>
</item>
<item>
  <title>NYCC-PV-CH-CHA_260930-100340 - Join.mp4</title>
  <media:content url="https://vbfast-vod.viebit.com/counciln/4x3sjEs6ya3aI5gP/NYCC-PV-CH-CHA_260930-100340%20-%20Join.jpg?v=0" medium="image" />
  <guid isPermaLink="false">4x3sjEs6ya3aI5gP</guid>
  <pubDate>Thu, 01 Oct 2026 18:09:46 +0000</pubDate>
</item>
</channel></rss>
"""


def test_rss_exposes_the_cdn_asset_name() -> None:
    items = {item.filename: item.cdn_filename for item in parse_rss(RSS)}
    assert items == {
        "NYCC-PV-CH-COM_261022-Parks": "NYCC-PV-CH-CHA_261022-Parks",
        "NYCC-PV-CH-CHA_261022-Education": "NYCC-PV-CH-CHA_261022-Education",
        "NYCC-PV-CH-CHA_260930-100340 - Join": "NYCC-PV-CH-CHA_260930-100340 - Join",
    }


def test_discover_stores_the_cdn_asset_name(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "registry.db")
    monkeypatch.setattr("pipeline.discover.fetch_rss", lambda *_a, **_k: parse_rss(RSS))
    discover_viebit_rss(conn)
    row = db.get_meeting(conn, "NYCC-PV-CH-COM_261022-Parks")
    assert row["viebit_cdn_filename"] == "NYCC-PV-CH-CHA_261022-Parks"
    assert row["viebit_hash"] == "W3zQEbqDf9F11iwB"


# --- fetch ------------------------------------------------------------------


def _fake_media(monkeypatch, downloads: list[str], missing_ext: str = ".vtt") -> None:
    def fake_download(url: str, dest: Path, min_size: int = 1) -> bool:
        downloads.append(url)
        if url.endswith(missing_ext):
            raise subprocess.CalledProcessError(22, ["curl", url])  # curl --fail on 404
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x" * 20_000)
        return True

    def touch(_src: Path, dest: Path) -> bool:
        dest.write_bytes(b"x" * 20_000)
        return True

    monkeypatch.setattr("pipeline.fetch._download", fake_download)
    monkeypatch.setattr("pipeline.production._download", fake_download)
    monkeypatch.setattr("pipeline.production._require_tool", lambda _name: None)
    monkeypatch.setattr("pipeline.production._transcode_video_web", touch)
    monkeypatch.setattr("pipeline.production._extract_audio", touch)
    monkeypatch.setattr("pipeline.production._extract_wav", touch)
    monkeypatch.setattr("pipeline.production.upload_video_web", lambda *_a: None)
    monkeypatch.setattr("pipeline.production._probe_duration", lambda _p: 282.0)


def test_fetch_uses_feed_asset_name_and_tolerates_missing_captions(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "registry.db")
    monkeypatch.setattr("pipeline.discover.fetch_rss", lambda *_a, **_k: parse_rss(RSS))
    discover_viebit_rss(conn)
    downloads: list[str] = []
    _fake_media(monkeypatch, downloads)

    row = db.get_meeting(conn, "NYCC-PV-CH-COM_261022-Parks")
    production.fetch_meeting_prod(conn, row, meetings_dir=tmp_path / "meetings")

    base = "https://vbfast-vod.viebit.com/counciln/W3zQEbqDf9F11iwB/NYCC-PV-CH-CHA_261022-Parks"
    assert downloads == [f"{base}.vtt", f"{base}.jpg", f"{base}.mp4"]
    fetched = db.get_meeting(conn, "NYCC-PV-CH-COM_261022-Parks")
    assert fetched["fetch_status"] == "fetched"
    assert fetched["duration_seconds"] == 282.0
    meeting_dir = tmp_path / "meetings" / "NYCC-PV-CH-COM_261022-Parks"
    assert not (meeting_dir / "captions.vtt").exists()
    assert not (meeting_dir / ".captions.vtt.part").exists()


def test_fetch_still_fails_when_the_video_itself_is_missing(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "registry.db")
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-CHA_261022-Education", "viebit_hash": "h"})
    _fake_media(monkeypatch, [], missing_ext=".mp4")
    with pytest.raises(subprocess.CalledProcessError):
        production.fetch_meeting_prod(
            conn, db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-Education"), meetings_dir=tmp_path / "meetings"
        )


def test_prepare_without_captions_marks_prepared(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    row = db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-CHA_261022-CJ", "fetch_status": "fetched"})
    (tmp_path / "meetings" / "NYCC-PV-CH-CHA_261022-CJ").mkdir(parents=True)

    prepare_meeting(conn, row, meetings_dir=tmp_path / "meetings")

    assert db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-CJ")["prepare_status"] == "prepared"
    assert not (tmp_path / "meetings" / "NYCC-PV-CH-CHA_261022-CJ" / "captions-clean.jsonl").exists()


def test_reconcile_does_not_rerun_prepare_for_missing_captions(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-CHA_261022-CJ"})
    db.update_meeting(conn, "NYCC-PV-CH-CHA_261022-CJ", {"fetch_status": "fetched", "prepare_status": "prepared"})
    meeting_dir = tmp_path / "NYCC-PV-CH-CHA_261022-CJ"
    meeting_dir.mkdir()
    (meeting_dir / "audio.m4a").write_bytes(b"x")

    production._reconcile_artifacts(conn, "NYCC-PV-CH-CHA_261022-CJ", tmp_path)

    row = db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-CJ")
    assert (row["fetch_status"], row["prepare_status"]) == ("fetched", "prepared")


# --- duration guard after fetch ----------------------------------------------


def _paired_row(conn, filename: str, duration: float, **extra) -> str:
    row = db.upsert_meeting(
        conn,
        {
            "legistar_event_id": 22800,
            "viebit_filename": filename,
            "body_name": "Committee on Parks and Recreation",
            "event_date": "2026-10-22T00:00:00",
            "event_time": "10:00 AM",
            **extra,
        },
    )
    db.update_meeting(conn, str(row["meeting_key"]), {"duration_seconds": duration})
    return str(row["meeting_key"])


def _stub_stages(monkeypatch) -> list[str]:
    paid: list[str] = []
    monkeypatch.setattr("pipeline.production._reconcile_artifacts", lambda *_a, **_k: None)
    monkeypatch.setattr("pipeline.production._persist_result_to_r2", lambda *_a, **_k: True)
    monkeypatch.setattr("pipeline.production._stage_fetch", lambda *_a, **_k: None)
    monkeypatch.setattr("pipeline.production._stage_prepare", lambda *_a, **_k: None)
    for name in ("_stage_transcribe_voxtral", "_stage_name_speakers", "_stage_chapterize"):
        monkeypatch.setattr(f"pipeline.production.{name}", lambda *_a, _n=name, **_k: paid.append(_n))
    return paid


def test_short_time_matched_recording_is_held_before_paid_stages(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "registry.db"
    conn = db.connect(db_path)
    key = _paired_row(conn, "NYCC-PV-CH-COM_261022-121338", 226.0, match_source="time")
    paid = _stub_stages(monkeypatch)

    result_json = tmp_path / "result.json"
    assert process_one(db_path, key, result_json=result_json, fail_on_error=True) == 0

    assert paid == []
    result = json.loads(result_json.read_text())
    assert result["status"] == "held"
    row = db.get_meeting(db.connect(db_path), key)
    assert row["last_error"].startswith(SHORT_RECORDING_HOLD_PREFIX + "NYCC-PV-CH-COM_261022-121338 is 3.8 min")
    assert row["process_attempts"] == 0
    assert select_process_candidates(db.connect(db_path)) == []
    assert verify_run_results(tmp_path, json.dumps({"include": [{"meeting_key": key}]})) == []


def test_dry_run_reports_the_hold_without_writing(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "registry.db"
    key = _paired_row(db.connect(db_path), "NYCC-PV-CH-COM_261022-121338", 226.0, match_source="time")
    _stub_stages(monkeypatch)
    result_json = tmp_path / "dry.json"
    process_one(db_path, key, result_json=result_json, dry_run=True)
    stages = json.loads(result_json.read_text())["stages"]
    assert {"stage": "duration-guard", "status": "would_hold"} in [
        {"stage": s["stage"], "status": s["status"]} for s in stages
    ]
    assert db.get_meeting(db.connect(db_path), key)["last_error"] is None


@pytest.mark.parametrize(
    "filename,duration,extra",
    [
        # Clerk-linked 3.4-minute Public Safety vote (2026-08-13).
        ("NYCC-250-8-3_260813-102749", 204.0, {"match_source": "legistar"}),
        # Pre-match_source clerk link, recognised from the Legistar video path.
        (
            "NYCC-PV-CH-COM_260923-132306",
            60.0,
            {"event_video_path": "https://councilnyc.viebit.com/vod/?s=true&v=NYCC-PV-CH-COM_260923-132306.mp4"},
        ),
        # The 4.7-minute Parks vote, named by the clerk in the filename.
        ("NYCC-PV-CH-COM_261022-Parks", 282.0, {"match_source": "suffix"}),
        # Long enough.
        ("NYCC-PV-CH-CHA_261022-101209", 557.0, {"match_source": "time"}),
        # Released by a human after review.
        ("NYCC-PV-CH-COM_261022-121338", 226.0, {"match_source": "manual"}),
    ],
)
def test_guard_trusts_clerk_and_named_pairings(tmp_path, monkeypatch, filename, duration, extra) -> None:
    db_path = tmp_path / "registry.db"
    key = _paired_row(db.connect(db_path), filename, duration, **extra)
    paid = _stub_stages(monkeypatch)
    process_one(db_path, key, result_json=tmp_path / "result.json")
    assert paid == ["_stage_transcribe_voxtral", "_stage_name_speakers", "_stage_chapterize"]
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "complete"


def test_legacy_pairing_without_source_or_clerk_link_counts_as_time_matched(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "registry.db")
    key = _paired_row(conn, "NYCC-PV-CH-CHA_260924-134504", 60.0)
    row = db.get_meeting(conn, key)
    assert pairing_source(row) == "time"
    assert post_fetch_hold_note(row) is not None


# --- match_source bookkeeping -----------------------------------------------


def test_match_source_is_recorded_and_cleared(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(tmp_path / "registry.db")
    for event_id, body, time in [(22800, "Committee on Parks and Recreation", "10:00 AM"),
                                 (22774, "Committee on Finance", "10:00 AM")]:
        db.upsert_meeting(conn, {"legistar_event_id": event_id, "body_name": body, "event_date": "2026-10-22T00:00:00",
                                 "event_time": time, "event_location": "Committee Room - City Hall"
                                 if event_id == 22800 else "Council Chambers - City Hall"})
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-COM_261022-Parks", "last_error": "match held: stale"})
    db.upsert_meeting(conn, {"viebit_filename": "NYCC-PV-CH-CHA_261022-101209",
                             "viebit_pub_date": "2026-10-22T14:22:46+00:00"})

    match_recordings(conn)

    parks = db.get_meeting(conn, "NYCC-PV-CH-COM_261022-Parks")
    assert (parks["match_source"], parks["last_error"]) == ("suffix", None)
    assert db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-101209")["match_source"] == "time"
    detach_event(conn, "NYCC-PV-CH-CHA_261022-101209")
    assert db.get_meeting(conn, "NYCC-PV-CH-CHA_261022-101209")["match_source"] is None


def test_clerk_link_records_legistar_source(tmp_path: Path, monkeypatch) -> None:
    from pipeline.discover import _attach_video_filename

    conn = db.connect(tmp_path / "registry.db")
    monkeypatch.setattr("pipeline.discover.resolve_viebit_hash", lambda _f: "hash")
    event = LegistarEvent(
        event_id=22666, event_guid=None, last_modified_utc=None, body_name="Committee on Public Safety",
        event_date="2026-08-13T00:00:00", event_time="10:00 AM",
        location="250 Broadway - 8th Floor - Hearing Room 3", agenda_pdf_url=None, minutes_pdf_url=None,
        insite_url=None, video_path="https://councilnyc.viebit.com/vod/?s=true&v=NYCC-250-8-3_260813-102749.mp4",
        agenda_status_name=None, minutes_status_name=None,
    )
    values: dict = {}
    _attach_video_filename(conn, client=None, event=event, values=values)
    assert values["viebit_filename"] == "NYCC-250-8-3_260813-102749"
    assert values["match_source"] == "legistar"
