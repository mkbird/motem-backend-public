"""Public web-play routes for AI games.

Serves the exported Godot HTML5 bundle for a game at a stable, login-free,
shareable URL: ``/play/{game_id}/``. A modify → re-export overwrites the
published bundle in place, so the URL never changes but the content refreshes.

Not under ``/api/*`` — anonymous-public on purpose (matches share_router).

The bundle is single-threaded (nothreads) Godot Web, so it needs **no**
COOP/COEP cross-origin-isolation headers and any plain static server works.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from robot.games.web_export import published_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/play", tags=["play"])

# Explicit content types for the Godot Web bundle. .wasm in particular must be
# application/wasm for streaming compilation; mimetypes doesn't always know it.
_CONTENT_TYPES = {
    ".wasm": "application/wasm",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".pck": "application/octet-stream",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".otf": "font/otf",
    ".ttf": "font/ttf",
}


def _resolve_in_bundle(game_id: str, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside the game's published bundle, safely.

    Raises 404 if the game isn't published or the resolved path escapes the
    bundle dir (path-traversal guard).
    """
    bundle = published_dir(game_id)
    index = bundle / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="game_not_published")
    target = (bundle / rel_path).resolve()
    bundle_resolved = bundle.resolve()
    if target != bundle_resolved and bundle_resolved not in target.parents:
        raise HTTPException(status_code=404, detail="not_found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not_found")
    return target


def _file_response(path: Path) -> FileResponse:
    media_type = _CONTENT_TYPES.get(path.suffix.lower())
    return FileResponse(path, media_type=media_type)


@router.get("/{game_id}/")
async def play_index(game_id: str) -> FileResponse:
    """Serve the bundle's index.html (the play page)."""
    return _file_response(_resolve_in_bundle(game_id, "index.html"))


@router.get("/{game_id}/{file_path:path}")
async def play_asset(game_id: str, file_path: str) -> FileResponse:
    """Serve any asset within the published bundle."""
    return _file_response(_resolve_in_bundle(game_id, file_path))
