"""Tests for share router — public social-share landing + OG image."""

import json

import pytest
import pytest_asyncio
from httpx import AsyncClient

_GAME_ID = "ai_1777008002_f1a00b"

_FLAPPY_META = {
    "title": "Flappy Head",
    "title_i18n": {"en": "Flappy Head", "zh-Hans": "飞翔小鸟"},
    "description": "Dodge obstacles by tilting your head!",
    "description_i18n": {
        "en": "Dodge obstacles by tilting your head!",
        "zh-Hans": "靠点头躲避障碍！",
    },
    "icon": "🐦",
    "input_mode": "body_pose",
}


@pytest_asyncio.fixture(autouse=True)
async def _game_on_disk(tmp_path, monkeypatch):
    """Seed Flappy Head game dir + patch GAMES_DIR for every test in this module."""
    game_dir = tmp_path / _GAME_ID
    game_dir.mkdir()
    (game_dir / "meta.json").write_text(json.dumps(_FLAPPY_META), encoding="utf-8")
    monkeypatch.setattr("robot.games.share_router.GAMES_DIR", tmp_path)


@pytest.mark.asyncio
async def test_share_landing_known_game_returns_html_with_og_meta(client: AsyncClient):
    # Pick a seed game known to exist in the repo.
    game_id = "ai_1777008002_f1a00b"  # Flappy Head

    resp = await client.get(f"/share/{game_id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.text

    # OG meta basics
    assert 'property="og:type"' in body
    assert 'property="og:title"' in body
    assert 'property="og:description"' in body
    assert 'property="og:image"' in body
    assert f"/share/{game_id}/og-image.png" in body

    # Twitter card
    assert 'name="twitter:card"' in body
    assert "summary_large_image" in body

    # CTA href present (OneLink template, even if placeholder)
    assert "deep_link_value=" in body

    # Counters chip rendered (counts are 0 since no /share or /like has been hit)
    assert "⤴" in body
    assert "♡" in body


@pytest.mark.asyncio
async def test_share_landing_unknown_game_404(client: AsyncClient):
    resp = await client.get("/share/nonexistent_game_id_xyz")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_og_image_known_game_returns_png(client: AsyncClient):
    game_id = "ai_1777008002_f1a00b"
    resp = await client.get(f"/share/{game_id}/og-image.png")
    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "image/png"
    # PNG magic header
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(resp.content) > 1000  # non-trivial image


@pytest.mark.asyncio
async def test_share_landing_respects_accept_language_zh(client: AsyncClient):
    game_id = "ai_1777008002_f1a00b"
    resp = await client.get(
        f"/share/{game_id}",
        headers={"Accept-Language": "zh-Hans"},
    )
    assert resp.status_code == 200
    # zh-Hans title for Flappy Head is "飞翔小鸟"
    assert "飞翔小鸟" in resp.text
    # Chinese CTA
    assert "在 App 中打开" in resp.text


@pytest.mark.asyncio
async def test_share_landing_respects_accept_language_en(client: AsyncClient):
    game_id = "ai_1777008002_f1a00b"
    resp = await client.get(
        f"/share/{game_id}",
        headers={"Accept-Language": "en"},
    )
    assert resp.status_code == 200
    # English title for this seed game
    assert "Flappy Head" in resp.text
    assert "Open in App" in resp.text
