"""Tests for static file serving — verify H5 game files are accessible."""

import pytest


class TestStaticGameFiles:
    """Verify the pickpocket H5 game files are served correctly."""

    @pytest.mark.asyncio
    async def test_game_html_accessible(self, client):
        resp = await client.get("/static/games/pickpocket/index.html")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<canvas" in resp.text

    @pytest.mark.asyncio
    async def test_game_js_accessible(self, client):
        resp = await client.get("/static/games/pickpocket/game.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert "gameLoop" in resp.text or "requestAnimationFrame" in resp.text

    @pytest.mark.asyncio
    async def test_game_sprites_accessible(self, client):
        resp = await client.get("/static/games/pickpocket/sprites.js")
        assert resp.status_code == 200
        assert "drawThief" in resp.text

    @pytest.mark.asyncio
    async def test_game_input_accessible(self, client):
        resp = await client.get("/static/games/pickpocket/input.js")
        assert resp.status_code == 200
        assert "updateInput" in resp.text

    @pytest.mark.asyncio
    async def test_game_css_accessible(self, client):
        resp = await client.get("/static/games/pickpocket/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_nonexistent_game_404(self, client):
        resp = await client.get("/static/games/nonexistent/index.html")
        assert resp.status_code == 404
