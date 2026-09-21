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


@pytest.mark.parametrize("cost", [0.0, 0.004321])
def test_openrouter_uses_billed_response_cost_without_extra_requests(monkeypatch, cost):
    def unexpected(*args, **kwargs):
        raise AssertionError("response cost requires no follow-up HTTP call")
    monkeypatch.setattr(gemini, "_openrouter_generation_cost_usd", unexpected)
    monkeypatch.setattr(gemini, "_openrouter_pricing_for_model", unexpected)
    meta = {"usage": {"cost": cost}, "generation_id": "gen-test"}
    gemini._attach_openrouter_cost(meta, model="test/model", api_key="key")
    assert meta["exact_cost_usd"] == cost
    assert meta["cost_source"] == "openrouter_response_usage"


def test_retry_aggregation_preserves_fractional_billing():
    combined = gemini._combine_generation_attempts([
        {"usage": {"cost": 0.012, "completion_tokens": 4}, "estimated_cost_usd": 0.012},
        {"usage": {"cost": 0.023, "completion_tokens": 6}, "estimated_cost_usd": 0.023},
    ])
    assert combined["usage"]["cost"] == pytest.approx(0.035)
    assert combined["usage"]["completion_tokens"] == 10


def test_reasoning_tokens_are_not_charged_twice_in_fallback_estimate():
    usage = {"prompt_tokens": 100, "completion_tokens": 1000,
             "completion_tokens_details": {"reasoning_tokens": 800}}
    assert gemini._estimate_openrouter_usage_cost_usd(
        usage, {"prompt": "0.000001", "completion": "0.000002"}
    ) == pytest.approx(0.0021)


def test_flash_lite_snapshot_uses_lite_price_not_flash_prefix():
    assert gemini.estimate_usage_cost_usd('gemini-3.5-flash-lite-20260721',
        {'promptTokenCount': 1_000_000, 'candidatesTokenCount': 1_000_000}) == 2.8


def test_native_empty_response_retains_charged_usage(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-key')
    monkeypatch.setattr(gemini, '_post_with_retry', lambda *a, **k: FakeResponse(
        '{"usageMetadata":{"promptTokenCount":100},"candidates":[]}'))
    with pytest.raises(RuntimeError, match='no response candidates') as error:
        gemini.generate_json('test', model='gemini-3.8-flash', max_attempts=1)
    assert error.value.generation_meta['usage']['promptTokenCount'] == 100


def test_grounded_interaction_preserves_search_sources_and_billed_usage(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-key')
    captured = {}
    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeResponse(json.dumps({
            'status': 'completed',
            'usage': {'total_input_tokens': 1000, 'total_output_tokens': 100,
                      'total_thought_tokens': 200, 'total_tokens': 1300},
            'steps': [
                {'type': 'google_search_call', 'arguments': {'queries': ['witness organization']}},
                {'type': 'model_output', 'content': [{'type': 'text', 'text': '{"results": []}',
                    'annotations': [{'type': 'url_citation', 'url': 'https://example.org/staff', 'title': 'Staff'}]}]},
            ],
        }))
    monkeypatch.setattr(gemini, '_post_with_retry', post)
    result, meta = gemini.generate_json('verify', model='gemini-3.8-flash', tools=[{'google_search': {}}])
    assert result == {'results': []}
    assert captured['url'].endswith('/interactions')
    assert captured['json_payload']['store'] is False
    assert meta['grounding']['grounding_chunks'][0]['uri'] == 'https://example.org/staff'
    assert meta['grounding']['web_search_query_count'] == 1
    assert meta['usage']['thoughtsTokenCount'] == 200
    assert meta['estimated_cost_usd'] == gemini.estimate_usage_cost_usd('gemini-3.8-flash', meta['usage'])


def test_incomplete_grounded_interaction_preserves_charge(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'test-key')
    monkeypatch.setattr(gemini, '_post_with_retry', lambda *a, **k: FakeResponse(json.dumps({
        'status': 'incomplete', 'usage': {'total_input_tokens': 100, 'total_thought_tokens': 50}, 'steps': [],
    })))
    with pytest.raises(RuntimeError) as exc:
        gemini.generate_json('verify', model='gemini-3.8-flash', tools=[{'google_search': {}}])
    assert exc.value.generation_meta['estimated_cost_usd'] > 0
