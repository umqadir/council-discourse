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

# --- Naming/chaptering LLM ---
# Production default (2026-07-02): z-ai/glm-5.2 via OpenRouter beats Gemini 3.5 Flash on
# same-person and strict-spelling on both benchmarks (see PLAN.md sections 8 and 12), and is
# far steadier on the label->name mapping (no whole-speaker block collapse). Gemini stays one
# env flag away: set COUNCIL_LLM_PROVIDER=gemini (or COUNCIL_LLM_MODEL=gemini-3.5-flash).
GEMINI_LLM = {
    "provider": "gemini",
    "model": "gemini-3.5-flash",
    "base_url": None,
    "api_key_env": "GOOGLE_API_KEY",
}
OPENROUTER_GLM_LLM = {
    "provider": "openrouter",
    "model": "z-ai/glm-5.2",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
}
# Naming default (2026-07-03 LLM cost round, experiments/out/llm-cost-round.md):
# DeepSeek V4 Pro ties GLM-5.2 on both naming gates (87.9/97.3) at ~1/3 the cost;
# it FAILS the chaptering gates, so the split is naming=V4 Pro, chaptering=GLM-5.2.
OPENROUTER_DEEPSEEK_LLM = {
    "provider": "openrouter",
    "model": "deepseek/deepseek-v4-pro",
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
    """Production speaker-naming LLM (default: DeepSeek V4 Pro via OpenRouter)."""
    return _resolve_llm(OPENROUTER_DEEPSEEK_LLM, "COUNCIL_NAMING_LLM")


def chaptering_llm_config() -> dict[str, str | None]:
    """Production chaptering/summary LLM (default: GLM-5.2 via OpenRouter)."""
    return _resolve_llm(OPENROUTER_GLM_LLM, "COUNCIL_CHAPTER_LLM")
