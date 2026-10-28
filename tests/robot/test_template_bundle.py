"""GET /api/games/template-bundle — tests.

The endpoint serves Motem Studio (macOS) a tar.gz of `games/_template/`
plus `games/_template_slowpath/CLAUDE.md` so the macOS sandbox sees the
exact same Godot skeleton + CLAUDE.md the iOS generation pipeline uses.

Covers:
- 200 with tar.gz body containing `_template/project.godot` and
  `_template_slowpath/CLAUDE.md`
- ETag header is present, quoted, deterministic across calls
- `If-None-Match` with the current etag returns 304 + no body
- `If-None-Match` with a stale value returns 200 + a body
- `.godot` cache directories are stripped at tar time
"""

import io
import tarfile

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_template_bundle_returns_tarball(client: AsyncClient):
    resp = await client.get("/api/games/template-bundle")
    assert resp.status_code == 200
    assert resp.headers["content-type"] in {"application/gzip", "application/x-gzip"}

    etag = resp.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')

    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert "_template/project.godot" in names
        assert "_template_slowpath/CLAUDE.md" in names


@pytest.mark.asyncio
async def test_template_bundle_etag_is_deterministic(client: AsyncClient):
    first = await client.get("/api/games/template-bundle")
    second = await client.get("/api/games/template-bundle")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["etag"] == second.headers["etag"]


@pytest.mark.asyncio
async def test_template_bundle_if_none_match_matches_returns_304(client: AsyncClient):
    head = await client.get("/api/games/template-bundle")
    etag = head.headers["etag"]

    resp = await client.get(
        "/api/games/template-bundle",
        headers={"If-None-Match": etag},
    )
    assert resp.status_code == 304
    assert resp.headers["etag"] == etag
    assert resp.content == b""


@pytest.mark.asyncio
async def test_template_bundle_if_none_match_stale_returns_200(client: AsyncClient):
    resp = await client.get(
        "/api/games/template-bundle",
        headers={"If-None-Match": '"deadbeefdeadbeefdeadbeefdeadbeef"'},
    )
    assert resp.status_code == 200
    assert len(resp.content) > 0


@pytest.mark.asyncio
async def test_template_bundle_excludes_godot_cache(client: AsyncClient):
    resp = await client.get("/api/games/template-bundle")
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for name in tar.getnames():
            parts = name.split("/")
            assert ".godot" not in parts, f"engine cache leaked: {name}"
            assert ".git" not in parts, f"vcs metadata leaked: {name}"
            assert ".DS_Store" not in parts, f"finder metadata leaked: {name}"
