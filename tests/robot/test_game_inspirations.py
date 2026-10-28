"""Inspirations endpoint tests — GET /api/games/inspirations.

Covers:
- en + zh-Hans return ≥ 6 cards each, with title in correct language
- Accept-Language normalization (en-US, zh-Hans-CN, zh-CN, missing)
- Each card has the required (id/title/description/prompt) non-empty fields
- IDs are unique within a language
- No auth required (探索性内容)
- Two consecutive calls return identical content (no random mutation)
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_inspirations_returns_min_six_en(client: AsyncClient):
    resp = await client.get(
        "/api/games/inspirations",
        headers={"Accept-Language": "en"},
    )
    assert resp.status_code == 200
    cards = resp.json()["inspirations"]
    assert len(cards) >= 6
    # All English titles should be ASCII-printable (no CJK leaked from zh)
    for c in cards:
        assert c["title"].isascii(), f"non-ASCII en title: {c['title']!r}"


@pytest.mark.asyncio
async def test_inspirations_returns_min_six_zh(client: AsyncClient):
    resp = await client.get(
        "/api/games/inspirations",
        headers={"Accept-Language": "zh-Hans"},
    )
    assert resp.status_code == 200
    cards = resp.json()["inspirations"]
    assert len(cards) >= 6
    # At least one card should contain CJK characters in title
    assert any(any("一" <= ch <= "鿿" for ch in c["title"]) for c in cards)


@pytest.mark.asyncio
@pytest.mark.parametrize("hdr,expected_kind", [
    ("en-US", "ascii"),
    ("zh-Hans-CN", "cjk"),
    ("zh-CN", "cjk"),
    ("", "ascii"),  # missing → fall back to en
    ("fr", "ascii"),  # unknown → fall back to en
])
async def test_inspirations_lang_normalization(
    client: AsyncClient, hdr: str, expected_kind: str,
):
    headers = {"Accept-Language": hdr} if hdr else {}
    resp = await client.get("/api/games/inspirations", headers=headers)
    assert resp.status_code == 200
    cards = resp.json()["inspirations"]
    titles = "".join(c["title"] for c in cards)
    has_cjk = any("一" <= ch <= "鿿" for ch in titles)
    if expected_kind == "cjk":
        assert has_cjk, f"expected CJK titles for header={hdr!r}, got {titles!r}"
    else:
        assert not has_cjk, f"expected ASCII titles for header={hdr!r}, got {titles!r}"


@pytest.mark.asyncio
async def test_inspirations_each_card_has_required_fields(client: AsyncClient):
    resp = await client.get("/api/games/inspirations")
    cards = resp.json()["inspirations"]
    for c in cards:
        for field in ("id", "title", "description", "prompt"):
            assert field in c, f"missing {field}"
            assert isinstance(c[field], str)
            assert c[field].strip(), f"{field} empty in card {c.get('id')!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "zh-Hans"])
async def test_inspirations_id_unique_within_lang(
    client: AsyncClient, lang: str,
):
    resp = await client.get(
        "/api/games/inspirations",
        headers={"Accept-Language": lang},
    )
    cards = resp.json()["inspirations"]
    ids = [c["id"] for c in cards]
    assert len(ids) == len(set(ids)), f"duplicate ids in {lang}: {ids}"


@pytest.mark.asyncio
async def test_inspirations_no_auth_required(client: AsyncClient):
    """探索内容公开，无 token 也应能拉。"""
    resp = await client.get("/api/games/inspirations")
    assert resp.status_code == 200
    assert "inspirations" in resp.json()


@pytest.mark.asyncio
async def test_inspirations_consistent_across_calls(client: AsyncClient):
    """连续两次拉同一 lang，内容必须一字不差（防随机化 / 顺序漂移）。"""
    a = await client.get("/api/games/inspirations", headers={"Accept-Language": "en"})
    b = await client.get("/api/games/inspirations", headers={"Accept-Language": "en"})
    assert a.json() == b.json()
