from __future__ import annotations

import pytest

from pipeline.cli import _resolve_llm, build_parser
from pipeline.config import naming_llm_config

_LLM_ENV = (
    "COUNCIL_LLM_PROVIDER",
    "COUNCIL_LLM_MODEL",
    "COUNCIL_LLM_BASE_URL",
    "COUNCIL_LLM_API_KEY_ENV",
)


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LLM_ENV:
        monkeypatch.delenv(name, raising=False)


def _resolve(args: list[str]) -> dict[str, str | None]:
    return _resolve_llm(build_parser().parse_args(args))


def test_production_default_is_glm_via_openrouter() -> None:
    config = naming_llm_config()
    assert config["model"] == "z-ai/glm-5.2"
    assert config["base_url"] == "https://openrouter.ai/api/v1"
    assert config["api_key_env"] == "OPENROUTER_API_KEY"


def test_name_speakers_default_routes_to_glm() -> None:
    assert _resolve(["name-speakers", "--meeting-dir", "/tmp/x"]) == {
        "model": "z-ai/glm-5.2",
        "llm_base_url": "https://openrouter.ai/api/v1",
        "llm_api_key_env": "OPENROUTER_API_KEY",
    }


def test_chapterize_default_routes_to_glm() -> None:
    resolved = _resolve(["chapterize", "--meeting-dir", "/tmp/x"])
    assert resolved["model"] == "z-ai/glm-5.2"
    assert resolved["llm_base_url"] == "https://openrouter.ai/api/v1"


def test_gemini_is_one_flag_away(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COUNCIL_LLM_PROVIDER", "gemini")
    assert _resolve(["name-speakers", "--meeting-dir", "/tmp/x"]) == {
        "model": "gemini-3.5-flash",
        "llm_base_url": None,
        "llm_api_key_env": None,
    }


def test_bare_gemini_model_uses_native_gemini_path() -> None:
    # A native Gemini id (no provider slug) must not be sent through OpenRouter.
    assert _resolve(["name-speakers", "--meeting-dir", "/tmp/x", "--model", "gemini-3.5-flash"]) == {
        "model": "gemini-3.5-flash",
        "llm_base_url": None,
        "llm_api_key_env": None,
    }


def test_explicit_openrouter_override_is_honored() -> None:
    resolved = _resolve(
        [
            "name-speakers",
            "--meeting-dir",
            "/tmp/x",
            "--model",
            "deepseek/deepseek-v4-flash",
            "--llm-base-url",
            "https://openrouter.ai/api/v1",
            "--llm-api-key-env",
            "OPENROUTER_API_KEY",
        ]
    )
    assert resolved["model"] == "deepseek/deepseek-v4-flash"
    assert resolved["llm_base_url"] == "https://openrouter.ai/api/v1"
    assert resolved["llm_api_key_env"] == "OPENROUTER_API_KEY"
