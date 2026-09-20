from pipeline import roster


def test_supplement_fills_missing_district_for_verified_term_only(tmp_path, monkeypatch):
    cache = tmp_path / 'roster.csv'
    cache.write_text('name,district,party,term_start,term_end\nErik Bottcher,3,Democrat,2022-01-01,2026-03-01\n')
    monkeypatch.setattr(roster, 'fetch_roster', lambda **kwargs: cache)
    assert roster.current_roster('2025-09-01')[0]['name'] == 'Erik Bottcher'
    assert roster.current_roster('2026-04-01') == []
    assert roster.current_roster('2026-09-01')[0]['name'] == 'Carl Wilson'
    assert roster.current_roster('2030-01-01') == []


def test_upstream_roster_takes_precedence_when_feed_catches_up(tmp_path, monkeypatch):
    cache = tmp_path / 'roster.csv'
    cache.write_text('name,district,party,term_start,term_end\nCarl Wilson,03,Democrat,2026-04-30,2029-12-31\n')
    monkeypatch.setattr(roster, 'fetch_roster', lambda **kwargs: cache)
    rows = roster.current_roster('2026-09-01')
    assert len(rows) == 1
    assert rows[0]['term_start'] == '2026-04-30'
