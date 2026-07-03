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
DEFAULT_LLM_PROVIDER = "openrouter"
_LLM_PROVIDERS = {
    "openrouter": OPENROUTER_GLM_LLM,
    "glm": OPENROUTER_GLM_LLM,
    "gemini": GEMINI_LLM,
    "google": GEMINI_LLM,
}


def naming_llm_config() -> dict[str, str | None]:
    """Resolve the production naming/chaptering LLM, honouring env overrides.

    COUNCIL_LLM_PROVIDER selects a provider preset (openrouter|gemini); COUNCIL_LLM_MODEL,
    COUNCIL_LLM_BASE_URL, and COUNCIL_LLM_API_KEY_ENV override individual fields.
    """
    provider = (os.environ.get("COUNCIL_LLM_PROVIDER") or DEFAULT_LLM_PROVIDER).strip().lower()
    base = dict(_LLM_PROVIDERS.get(provider, OPENROUTER_GLM_LLM))
    model = os.environ.get("COUNCIL_LLM_MODEL")
    if model:
        base["model"] = model.strip()
    base_url = os.environ.get("COUNCIL_LLM_BASE_URL")
    if base_url:
        base["base_url"] = base_url.strip() or None
    api_key_env = os.environ.get("COUNCIL_LLM_API_KEY_ENV")
    if api_key_env:
        base["api_key_env"] = api_key_env.strip()
    return base
