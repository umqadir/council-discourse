import json

import pytest

from pipeline import gemini


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


def _completion_body(content: str) -> str:
    return json.dumps(
        {
            "id": "gen-1",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )


def _run(monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse], max_attempts: int):
    calls = iter(responses)
    monkeypatch.setattr(gemini, "_post_with_retry", lambda *a, **k: next(calls))
    monkeypatch.setattr(gemini, "_attach_openrouter_cost", lambda *a, **k: None)
    return gemini.generate_json(
        "prompt",
        model="test/model",
        base_url="https://openrouter.ai/api/v1",
        api_key="key",
        max_attempts=max_attempts,
    )


def test_malformed_response_body_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        FakeResponse('{"choices": [{"mess'),  # truncated body
        FakeResponse(_completion_body('{"ok": true}')),
    ]
    parsed, meta = _run(monkeypatch, responses, max_attempts=2)
    assert parsed == {"ok": True}
    assert meta["attempt"] == 2


def test_malformed_response_body_exhausts_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="malformed response body"):
        _run(monkeypatch, [FakeResponse("<html>bad gateway</html>")], max_attempts=1)
