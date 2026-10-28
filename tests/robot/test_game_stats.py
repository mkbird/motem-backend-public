"""Tests for AI game share / like counter endpoints."""

import json

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import auth_header, register_and_login


GAME_ID = "ai_1777008002_f1a00b"
UNKNOWN_GAME_ID = "ai_does_not_exist_xyz"

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
    game_dir = tmp_path / GAME_ID
    game_dir.mkdir()
    (game_dir / "meta.json").write_text(json.dumps(_FLAPPY_META), encoding="utf-8")
    monkeypatch.setattr("robot.games.router.GAMES_DIR", tmp_path)
    monkeypatch.setattr("robot.games.share_router.GAMES_DIR", tmp_path)


@pytest.mark.asyncio
async def test_share_increments_anonymously(client: AsyncClient):
    # First share
    resp1 = await client.post(f"/api/games/{GAME_ID}/share")
    assert resp1.status_code == 200
    assert resp1.json()["share_count"] == 1
    # Second share
    resp2 = await client.post(f"/api/games/{GAME_ID}/share")
    assert resp2.json()["share_count"] == 2


@pytest.mark.asyncio
async def test_share_unknown_game_404(client: AsyncClient):
    resp = await client.post(f"/api/games/{UNKNOWN_GAME_ID}/share")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_like_toggle_same_user(client: AsyncClient):
    auth = await register_and_login(client, email="liker@example.com")
    headers = auth_header(auth["access_token"])

    # First like → on
    r1 = await client.post(f"/api/games/{GAME_ID}/like", headers=headers)
    assert r1.status_code == 200
    assert r1.json() == {"liked_by_me": True, "like_count": 1}

    # Second like → toggle off
    r2 = await client.post(f"/api/games/{GAME_ID}/like", headers=headers)
    assert r2.json() == {"liked_by_me": False, "like_count": 0}

    # Third → on again
    r3 = await client.post(f"/api/games/{GAME_ID}/like", headers=headers)
    assert r3.json() == {"liked_by_me": True, "like_count": 1}


@pytest.mark.asyncio
async def test_like_two_users_accumulate(client: AsyncClient):
    a = await register_and_login(client, email="alice@example.com", device_id="dev-a")
    b = await register_and_login(client, email="bob@example.com", device_id="dev-b")

    r1 = await client.post(
        f"/api/games/{GAME_ID}/like", headers=auth_header(a["access_token"])
    )
    assert r1.json()["like_count"] == 1
    r2 = await client.post(
        f"/api/games/{GAME_ID}/like", headers=auth_header(b["access_token"])
    )
    assert r2.json()["like_count"] == 2


@pytest.mark.asyncio
async def test_like_requires_auth(client: AsyncClient):
    resp = await client.post(f"/api/games/{GAME_ID}/like")
    # HTTPBearer 没拿到凭证 → 403/401（FastAPI 默认 403）
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_stats_anonymous_omits_liked_by_me(client: AsyncClient):
    await client.post(f"/api/games/{GAME_ID}/share")  # bump share to 1
    resp = await client.get(f"/api/games/{GAME_ID}/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["share_count"] == 1
    assert body["like_count"] == 0
    assert "liked_by_me" not in body


@pytest.mark.asyncio
async def test_stats_authed_includes_liked_by_me(client: AsyncClient):
    auth = await register_and_login(client, email="me@example.com")
    headers = auth_header(auth["access_token"])

    # Pre-like
    await client.post(f"/api/games/{GAME_ID}/like", headers=headers)

    resp = await client.get(f"/api/games/{GAME_ID}/stats", headers=headers)
    body = resp.json()
    assert body["liked_by_me"] is True
    assert body["like_count"] == 1


@pytest.mark.asyncio
async def test_list_games_includes_counts(client: AsyncClient):
    await client.post(f"/api/games/{GAME_ID}/share")
    auth = await register_and_login(client, email="lister@example.com")
    await client.post(
        f"/api/games/{GAME_ID}/like", headers=auth_header(auth["access_token"])
    )

    # Authed list — liked_by_me 应该出现
    resp = await client.get(
        "/api/games", headers=auth_header(auth["access_token"])
    )
    games = resp.json()["games"]
    target = next((g for g in games if g["game_id"] == GAME_ID), None)
    assert target is not None
    assert target["share_count"] == 1
    assert target["like_count"] == 1
    assert target["liked_by_me"] is True

    # 其他游戏 liked_by_me 应为 False
    other = next((g for g in games if g["game_id"] != GAME_ID), None)
    if other is not None:
        assert other.get("liked_by_me") is False


@pytest.mark.asyncio
async def test_share_landing_does_not_increment(client: AsyncClient):
    """爬虫抓 /share/{id} HTML 不应触发 share_count +1。"""
    # 抓两次落地页
    await client.get(f"/share/{GAME_ID}")
    await client.get(f"/share/{GAME_ID}")
    # 计数应仍为 0
    resp = await client.get(f"/api/games/{GAME_ID}/stats")
    assert resp.json()["share_count"] == 0
