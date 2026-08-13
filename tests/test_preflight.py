import io
import json
import urllib.error

import pytest

from pipeline import preflight


class FakeResp:
    """Minimal stand-in for the urlopen context manager, readable by json.load."""

    def __init__(self, body: dict):
        self._buf = io.BytesIO(json.dumps(body).encode())

    def read(self, *args):
        return self._buf.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", hdrs=None, fp=None)


def _install(monkeypatch, outcomes):
    """Feed a sequence of urlopen outcomes (FakeResp bodies or exceptions)."""
    calls = {"n": 0}
    it = iter(outcomes)

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return FakeResp(item)

    monkeypatch.setattr(preflight.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_transient_503_is_retried_then_succeeds(monkeypatch):
    body = {"ok": True}
    calls = _install(monkeypatch, [_http_error(503), body])
    result = preflight._get_with_retry("http://x", "key", sleep=lambda _s: None)
    assert result == body
    assert calls["n"] == 2


def test_auth_error_fails_fast_without_retry(monkeypatch):
    calls = _install(monkeypatch, [_http_error(401)])
    with pytest.raises(urllib.error.HTTPError):
        preflight._get_with_retry("http://x", "key", sleep=lambda _s: None)
    assert calls["n"] == 1  # persistent auth failure is not retried


def test_transient_error_exhausts_attempts(monkeypatch):
    calls = _install(monkeypatch, [_http_error(503)] * 3)
    with pytest.raises(urllib.error.HTTPError):
        preflight._get_with_retry("http://x", "key", max_attempts=3, sleep=lambda _s: None)
    assert calls["n"] == 3


def test_url_error_is_retried(monkeypatch):
    body = {"ok": True}
    calls = _install(monkeypatch, [urllib.error.URLError("timed out"), body])
    result = preflight._get_with_retry("http://x", "key", sleep=lambda _s: None)
    assert result == body
    assert calls["n"] == 2


def test_run_preflight_passes_when_all_healthy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")

    def fake_get(url, key=None, **kwargs):
        if "openrouter" in url:
            return {"data": {"total_credits": "10", "total_usage": "1"}}
        return {"data": []}

    monkeypatch.setattr(preflight, "_get_with_retry", fake_get)
    assert preflight.run_preflight() == 0


def test_run_preflight_fails_on_low_credits(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")

    def fake_get(url, key=None, **kwargs):
        if "openrouter" in url:
            return {"data": {"total_credits": "2", "total_usage": "1.5"}}  # $0.50 left
        return {"data": []}

    monkeypatch.setattr(preflight, "_get_with_retry", fake_get)
    assert preflight.run_preflight() == 1


def test_run_preflight_fails_when_provider_down(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")

    def fake_get(url, key=None, **kwargs):
        if "generativelanguage" in url:
            raise _http_error(503)
        if "openrouter" in url:
            return {"data": {"total_credits": "10", "total_usage": "1"}}
        return {"data": []}

    monkeypatch.setattr(preflight, "_get_with_retry", fake_get)
    assert preflight.run_preflight() == 1
