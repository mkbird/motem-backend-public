"""Auto-discover a public base URL (ngrok) for the bot.

So the bot's /play link and the Discord embed thumbnail work without manually
juggling ROBOT_SHARE_PUBLIC_BASE_URL: if it's not configured, query ngrok's
local API (same source scripts/start_dev_with_ngrok.py uses) for the https
tunnel to the backend. Returns "" when nothing public is available (bot then
falls back to localhost — embed shows no thumbnail).
"""

from __future__ import annotations

import json
import logging
from urllib.request import urlopen

logger = logging.getLogger(__name__)

NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def discover_ngrok_url(backend_port: int = 8000, *, timeout: float = 2.0) -> str:
    """Return the https ngrok public URL tunneling the backend, or ''."""
    try:
        with urlopen(NGROK_API, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return ""
    https = [
        (t.get("public_url", ""), str(t.get("config", {}).get("addr", "")))
        for t in data.get("tunnels", [])
        if str(t.get("public_url", "")).startswith("https://")
    ]
    for pu, addr in https:  # prefer the tunnel pointing at our backend port
        if addr.endswith(f":{backend_port}"):
            return pu
    return https[0][0] if https else ""


def discover_public_base_url(backend_port: int = 8000) -> str:
    """Configured ROBOT_SHARE_PUBLIC_BASE_URL wins; otherwise auto-discover ngrok."""
    from robot.config import settings

    configured = (settings.share_public_base_url or "").strip()
    if configured:
        return configured
    return discover_ngrok_url(backend_port)
