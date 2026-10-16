import json
import pytest
from datetime import date
from pipeline import roster


def test_dated_memberships_replace_stale_feed_without_changing_history(tmp_path, monkeypatch):
    cache = tmp_path / 'roster.csv'
    cache.write_text('name,district,party,term_start,term_end\nPrevious Member,3,Democrat,2022-01-01,2025-12-31\n')
    monkeypatch.setattr(roster, 'fetch_roster', lambda **kwargs: cache)
    monkeypatch.setattr(roster, 'fetch_memberships', lambda **kwargs: [
        {'name':'New Member','district':'3','party':'','term_start':'2026-05-13','term_end':'2029-12-31'}])
    assert roster.current_roster('2025-09-01')[0]['name'] == 'Previous Member'
    assert roster.current_roster('2026-04-01') == []
    assert roster.current_roster('2026-09-01')[0]['name'] == 'New Member'
    assert roster.current_roster('2030-01-01') == []


def test_parser_excludes_non_district_officers_and_trims_source_names():
    record = {'OfficeRecordBodyName':'City Council','OfficeRecordExtraText':'District 03',
        'OfficeRecordFullName':'New Member ', 'OfficeRecordStartDate':'2026-05-13T00:00:00',
        'OfficeRecordEndDate':'2029-12-31T00:00:00', 'OfficeRecordId':6218}
    rows=roster._parse_memberships([record, dict(record, OfficeRecordExtraText='', OfficeRecordFullName='Public Advocate')])
    assert len(rows)==1
    assert rows[0]['name']=='New Member'
    assert rows[0]['district']=='3'
    assert rows[0]['term_start']=='2026-05-13'


def test_overlapping_terms_and_incomplete_source_fail_closed():
    row={'name':'Member','district':'3','term_start':'2026-01-01','term_end':'2029-12-31'}
    with pytest.raises(RuntimeError,match='Overlapping'):
        roster._validate_memberships([row,row],date(2026,9,21))
    with pytest.raises(RuntimeError,match='0 active'):
        roster._validate_memberships([],date(2026,9,21))


def test_fresh_membership_cache_needs_no_network_or_token(tmp_path,monkeypatch):
    cache=tmp_path/'members.json'; cache.write_text(json.dumps({'rows':[{'name':'Member'}]}))
    monkeypatch.setattr(roster,'MEMBERSHIP_CACHE',cache)
    monkeypatch.delenv('LEGISTAR_TOKEN',raising=False)
    assert roster.fetch_memberships()==[{'name':'Member'}]


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


def test_fetch_roster_retries_transient_5xx_then_succeeds(tmp_path, monkeypatch):
    cache = tmp_path / 'roster.csv'
    monkeypatch.setattr(roster, 'ROSTER_CACHE', cache)
    monkeypatch.setattr(roster.time, 'sleep', lambda *a: None)
    calls = {'n': 0}

    def fake_get(url, headers=None, timeout=None):
        calls['n'] += 1
        if calls['n'] < 3:
            return _Resp(503, 'Service unavailable')
        return _Resp(200, 'name,district,party,term_start,term_end\nX,3,,2026-01-01,2029-12-31\n')

    monkeypatch.setattr(roster.httpx, 'get', fake_get)
    assert roster.fetch_roster(force_refresh=True) == cache
    assert cache.read_text().startswith('name,district')
    assert calls['n'] == 3


def test_fetch_roster_fails_fast_on_non_transient_status(tmp_path, monkeypatch):
    cache = tmp_path / 'roster.csv'
    monkeypatch.setattr(roster, 'ROSTER_CACHE', cache)
    monkeypatch.setattr(roster.time, 'sleep', lambda *a: None)
    calls = {'n': 0}

    def fake_get(url, headers=None, timeout=None):
        calls['n'] += 1
        return _Resp(404, 'not found')

    monkeypatch.setattr(roster.httpx, 'get', fake_get)
    with pytest.raises(RuntimeError, match='404'):
        roster.fetch_roster(force_refresh=True)
    assert calls['n'] == 1  # persistent failure is not retried


def test_directory_detects_missing_successor(monkeypatch):
    class Response:
        text = '<table><tr><td>3</td><td>New Member</td><td></td><td>Manhattan</td><td>Democrat</td></tr></table>'
        def raise_for_status(self): pass
    monkeypatch.setattr(roster.httpx, 'get', lambda *a, **kw: Response())
    with pytest.raises(RuntimeError, match='district 3'):
        roster._check_directory([{'district':'3','name':'Previous Member'}])
    assert roster._check_directory([{'district':'3','name':'New M. Member'}])['3']['party']=='Democrat'
