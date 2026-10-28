"""City mapping for MotemBackend MCP weather.

Ports the logic from echo_agent/src/mcp/weather/city_mapping.py:
- city name -> cityId (fuzzy include match; LRU cached)
- action -> token (hard-coded ACTION_TOKENS)

We expect a `cities.txt` where each line is a JSON object (one city per line).

By default, in dev we try to locate echo_agent's cities file automatically.
For production, set one of:
- ROBOT_WEATHER_CITIES_FILE
- HOMEHUB_WEATHER_CITIES_FILE
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


ACTION_TOKENS: dict[str, str] = {
    "current": "50b53ff8dd7d9fa320d3d3ca32cf8ed1",
    "hourly_24h": "008d2ad9197090c5dddc76f583616606",
    "daily_15d": "f9f212e1996e79e0e602b08ea297ffb0",
    "aqi_5d": "0418c1f4e5e66405d33556418189d2d0",
    "aqi": "8b36edf8e3444047812be3a59d27bab9",
    "life_index": "5944a84ec4a071359cc4f6928b797f91",
    "alert": "7ebe966ee2e04bbd8cdbc0b84f7f3bc7",
    "restriction": "27200005b3475f8b0e26428f9bfb13e9",
}


def _find_cities_file() -> Path | None:
    # Allow overriding via env var.
    import os

    for key in ("ROBOT_WEATHER_CITIES_FILE", "HOMEHUB_WEATHER_CITIES_FILE"):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        p = Path(raw)
        if p.exists():
            if p.is_file():
                return p
            logger.error("%s points to a directory: %s", key, str(p))

    # Try locating echo_agent's cities.txt under workspace.
    this = Path(__file__).resolve()
    for parent in this.parents:
        candidate = (
            parent
            / "echo_agent"
            / "src"
            / "mcp"
            / "weather"
            / "data"
            / "cities.txt"
        )
        if candidate.exists() and candidate.is_file():
            return candidate

    return None


_CITIES_FILE: Path | None = _find_cities_file()


def _search_cities(keyword: str, limit: int = 100) -> list[dict[str, Any]]:
    """Fuzzy search cities by substring include (ported)."""
    results: list[dict[str, Any]] = []
    if not keyword:
        return results

    if _CITIES_FILE is None or not _CITIES_FILE.exists() or not _CITIES_FILE.is_file():
        logger.error("cities.txt not found for MCP weather.")
        return results

    try:
        with _CITIES_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    city = json.loads(line)
                    name = city.get("name", "")
                    if keyword and keyword in name:
                        results.append(city)
                        if len(results) >= limit:
                            break
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        logger.error("cities file not found: %s", _CITIES_FILE)

    return results


@lru_cache(maxsize=256)
def get_city_id(city_name: str) -> int | None:
    """Get cityId from city name (exact match first, then include match)."""
    if not city_name:
        return None

    results = _search_cities(city_name, limit=100)
    for city in results:
        if city.get("name") == city_name:
            return city.get("id")

    if results:
        return results[0].get("id")
    return None


def get_token(action: str) -> str:
    """Get token for given action."""
    return ACTION_TOKENS.get(action, "")
