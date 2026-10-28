"""Tests for the BGM picker endpoints used by the iOS game-creator UI:

- `GET /api/games/audio/bgm-list` — returns the BGM catalog
- `GET /api/games/audio/bgm-preview/{bgm_id}` — streams the m4a clip

The previews on disk are produced by `scripts/build_bgm_previews.py`.
"""

from __future__ import annotations

import pytest


class TestBGMListEndpoint:
    @pytest.mark.asyncio
    async def test_returns_only_bgm_category(self, client):
        resp = await client.get("/api/games/audio/bgm-list")
        assert resp.status_code == 200
        body = resp.json()
        assert "bgms" in body
        # Every returned id must be a bgm_* id (the manifest reserves the
        # `bgm` category for music, not SFX).
        for entry in body["bgms"]:
            assert entry["id"].startswith("bgm_"), f"non-bgm id leaked: {entry['id']}"

    @pytest.mark.asyncio
    async def test_each_entry_has_required_fields(self, client):
        resp = await client.get("/api/games/audio/bgm-list")
        assert resp.status_code == 200
        bgms = resp.json()["bgms"]
        assert len(bgms) > 0, "expected at least one BGM in the catalog"
        for entry in bgms:
            assert set(entry.keys()) >= {"id", "description", "preview_url"}
            assert entry["preview_url"] == f"/api/games/audio/bgm-preview/{entry['id']}"

    @pytest.mark.asyncio
    async def test_returns_at_least_25_tracks(self, client):
        """BGM library should keep covering 5 categories × 5 tracks. Drop
        below this threshold and the picker UI will look anemic."""
        resp = await client.get("/api/games/audio/bgm-list")
        assert resp.status_code == 200
        assert len(resp.json()["bgms"]) >= 25


class TestBGMPreviewEndpoint:
    @pytest.mark.asyncio
    async def test_returns_m4a_for_known_id(self, client):
        # Pick the first id from the catalog so this test stays valid even
        # if track ids are re-ordered.
        listing = (await client.get("/api/games/audio/bgm-list")).json()["bgms"]
        assert listing, "no bgms in catalog — preview test cannot run"
        first_id = listing[0]["id"]

        resp = await client.get(f"/api/games/audio/bgm-preview/{first_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/")
        # m4a starts with `....ftyp` (offset 4-8 bytes); cheaper sanity
        # check: non-empty body that begins with the ftyp header marker.
        data = resp.content
        assert len(data) > 100
        assert b"ftyp" in data[:32]

    @pytest.mark.asyncio
    async def test_unknown_id_returns_404(self, client):
        resp = await client.get("/api/games/audio/bgm-preview/bgm_definitely_not_real")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("malicious", [
        "..%2F..%2Fetc%2Fpasswd",   # url-encoded path traversal
        "bgm_8bit%2F..%2Fsomething",
    ])
    async def test_rejects_path_traversal_attempts(self, client, malicious):
        resp = await client.get(f"/api/games/audio/bgm-preview/{malicious}")
        # 400 (explicit reject) or 404 (file not found after sanitization)
        # both prove no file outside the preview dir was reachable.
        assert resp.status_code in (400, 404)
