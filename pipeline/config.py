from __future__ import annotations

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
