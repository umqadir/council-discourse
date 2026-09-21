from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REGISTRY_DB = DATA_DIR / "registry.db"
MEETINGS_DIR = DATA_DIR / "meetings"

VIEBIT_RSS_URL = "https://councilnyc.viebit.com/rss.xml"
VIEBIT_VOD_URL = "https://councilnyc.viebit.com/vod/?s=true&v={filename}.mp4"
VIEBIT_CDN_URL = "https://vbfast-vod.viebit.com/counciln/{hash}/{filename}.{ext}"

LEGISTAR_BASE_URL = "https://webapi.legistar.com/v1/nyc"
LEGISTAR_INSITE_BASE_URL = "https://legistar.council.nyc.gov"

HTTP_TIMEOUT_SECONDS = 60
# September 21: batch again supports diarization. Paired 8-minute hearing and
# roll-call clips preserved every word and timestamp; hearing labels matched
# exactly, with minor roll-call clustering differences. Runtime validation
# rejects speech with no speaker labels. Sync remains an explicit override.
VOXTRAL_BATCH_USD_PER_AUDIO_HOUR = 0.09
VOXTRAL_SYNC_USD_PER_AUDIO_HOUR = 0.18
VOXTRAL_USD_PER_AUDIO_HOUR = VOXTRAL_BATCH_USD_PER_AUDIO_HOUR


def voxtral_mode() -> str:
    value = os.environ.get("COUNCIL_VOXTRAL_MODE", "batch").strip().lower()
    if value in {"batch", "sync"}:
        return value
    return "sync"


def voxtral_usd_per_audio_hour(mode: str | None = None) -> float:
    return VOXTRAL_SYNC_USD_PER_AUDIO_HOUR if (mode or voxtral_mode()) == "sync" else VOXTRAL_BATCH_USD_PER_AUDIO_HOUR

# --- Naming/chaptering LLM ---
# September review: Gemini 3.8 Flash scored 81/87 on the paired naming screen,
# versus Pro 70 and GLM Flash 72, followed by production-prompt replay.
GEMINI_LLM = {
    "provider": "gemini",
    "model": "gemini-3.8-flash",
    "base_url": None,
    "api_key_env": "GOOGLE_API_KEY",
}
OPENROUTER_GLM_LLM = {
    "provider": "openrouter",
    "model": "z-ai/glm-5.3-flash",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
}
# V4 Pro remains the recovery model for failed/incomplete naming responses.
OPENROUTER_DEEPSEEK_LLM = {
    "provider": "openrouter",
    "model": "deepseek/deepseek-v4-pro",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
}
# September 2026 paired production replay: V4.1 Flash preserved witness
# chapters and individual stated-meeting votes at $0.056 vs GLM-5.2's $0.347
# across a 4.4h hearing and a 1.6h stated meeting. V4.1 Flash scored below
# the naming candidates, so it is selected for chaptering only.
OPENROUTER_CHAPTER_LLM = {
    "provider": "openrouter",
    "model": "deepseek/deepseek-v4.1-flash",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
}

DEFAULT_LLM_PROVIDER = "openrouter"
_LLM_PROVIDERS = {
    "openrouter": OPENROUTER_GLM_LLM,
    "glm": OPENROUTER_GLM_LLM,
    "gemini": GEMINI_LLM,
    "google": GEMINI_LLM,
}


def _resolve_llm(default: dict[str, str | None], stage_prefix: str) -> dict[str, str | None]:
    """Resolve an LLM config: stage-specific envs win, then shared envs, then default.

    Shared: COUNCIL_LLM_PROVIDER/MODEL/BASE_URL/API_KEY_ENV.
    Stage-specific: e.g. COUNCIL_NAMING_LLM_MODEL, COUNCIL_CHAPTER_LLM_PROVIDER.
    """
    def env(name: str) -> str | None:
        return os.environ.get(f"{stage_prefix}_{name}") or os.environ.get(f"COUNCIL_LLM_{name}")

    provider = (env("PROVIDER") or "").strip().lower()
    base = dict(_LLM_PROVIDERS[provider]) if provider in _LLM_PROVIDERS else dict(default)
    model = env("MODEL")
    if model:
        base["model"] = model.strip()
    base_url = env("BASE_URL")
    if base_url:
        base["base_url"] = base_url.strip() or None
    api_key_env = env("API_KEY_ENV")
    if api_key_env:
        base["api_key_env"] = api_key_env.strip()
    return base


def naming_llm_config() -> dict[str, str | None]:
    """Production speaker-naming LLM (default: native Gemini 3.8 Flash)."""
    return _resolve_llm(GEMINI_LLM, "COUNCIL_NAMING_LLM")


def chaptering_llm_config() -> dict[str, str | None]:
    """Production chaptering/summary LLM (default: DeepSeek V4.1 Flash)."""
    return _resolve_llm(OPENROUTER_CHAPTER_LLM, "COUNCIL_CHAPTER_LLM")
