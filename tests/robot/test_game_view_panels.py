"""View tab 后端：used_audio + assets list / serve / replace。

iOS view tab 音乐 panel 拉 GET /api/games/{id}/used_audio 显示 AI 实际用了哪些
BGM；图片 panel 拉 GET /api/games/{id}/assets 列资源 + 拉 binary 显示缩略图，
亮度调整后用 POST /api/games/{id}/assets?replace=true 覆盖原文件。
"""

import io
import json
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import AIGame, ExploreGameView, UserGameView
from tests.conftest import auth_header, register_and_login


@pytest.fixture(autouse=True)
def _isolate_games_dir(tmp_path, monkeypatch):
    """Redirect every module-level `GAMES_DIR` copy to a per-test tmp dir.

    Why: 历史上这些测试直接写真实仓库 `games/`，crash 时残留 stub 污染 worktree。
    每个 `from .service import GAMES_DIR` 副本都得单独 patch，否则测试代码里
    `game_service.GAMES_DIR` 和 service 内部 `GAMES_DIR` 会指到不同路径。
    """
    games_dir = tmp_path / "games"
    games_dir.mkdir()
    for target in (
        "robot.games.service.GAMES_DIR",
        "robot.games.router.GAMES_DIR",
        "robot.games.share_router.GAMES_DIR",
        "robot.agent.tools.game_tools.GAMES_DIR",
    ):
        monkeypatch.setattr(target, games_dir, raising=True)


def _png_bytes(w: int = 32, h: int = 32, color=(255, 0, 0)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest_asyncio.fixture
async def alice_with_game(db_session: AsyncSession, client: AsyncClient):
    from robot.games import service as game_service

    alice = await register_and_login(client, email="alice_view@example.com")
    game_id = "ai_test_alice_view_001"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=alice["user"]["id"],
        title="Alice View Test",
        title_i18n=json.dumps({"en": "Alice View Test", "zh-Hans": "视图测试"}),
        description="view test",
        description_i18n=json.dumps({"en": "view test", "zh-Hans": "视图测试"}),
        icon="🎮",
        input_mode="touch",
        kind="ai",
        visibility="private",
        original_game_id=None, template_from="", session_id=None,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    db_session.add(ExploreGameView(game_id=game_id, added_at=now))
    db_session.add(UserGameView(
        user_id=alice["user"]["id"], game_id=game_id,
        view_type="created", added_at=now,
    ))
    await db_session.commit()

    src = game_service.GAMES_DIR / game_id
    src.mkdir(parents=True, exist_ok=True)
    (src / "project.godot").write_text(
        '[application]\nconfig/name="View Test"\n', encoding="utf-8"
    )
    (src / "meta.json").write_text(
        json.dumps({"game_id": game_id, "title": "Alice View Test"}),
        encoding="utf-8",
    )

    yield {"alice": alice, "game_id": game_id, "game_dir": src}

    import shutil as _sh
    if src.is_dir():
        _sh.rmtree(src, ignore_errors=True)
    z = game_service.GAMES_DIR / f"{game_id}.zip"
    if z.exists():
        z.unlink()


@pytest_asyncio.fixture
async def bob_user(client: AsyncClient):
    return await register_and_login(client, email="bob_view@example.com")


# ─────────────────────────────────────────────────────────────────────
# GET /assets — list
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_assets_empty_when_no_dir(client, alice_with_game):
    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/assets", headers=h
    )
    assert resp.status_code == 200
    assert resp.json() == {"assets": []}


@pytest.mark.asyncio
async def test_list_assets_returns_pngs(client, alice_with_game):
    assets_dir = alice_with_game["game_dir"] / "assets"
    assets_dir.mkdir()
    (assets_dir / "bg.png").write_bytes(_png_bytes())
    (assets_dir / "ship.jpg").write_bytes(_png_bytes())
    (assets_dir / "ignore.txt").write_text("not an image")

    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/assets", headers=h
    )
    assert resp.status_code == 200
    items = resp.json()["assets"]
    names = [it["filename"] for it in items]
    assert "bg.png" in names
    assert "ship.jpg" in names
    assert "ignore.txt" not in names
    # url 字段格式可解
    assert items[0]["url"].startswith(f"/api/games/{alice_with_game['game_id']}/assets/")
    # size_bytes > 0
    assert all(it["size_bytes"] > 0 for it in items)


@pytest.mark.asyncio
async def test_list_assets_non_owner_403(client, alice_with_game, bob_user):
    h = auth_header(bob_user["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/assets", headers=h
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_assets_unauthenticated_401(client, alice_with_game):
    resp = await client.get(f"/api/games/{alice_with_game['game_id']}/assets")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_assets_invalid_game_404(client):
    user = await register_and_login(client, email="lonely@example.com")
    h = auth_header(user["access_token"])
    resp = await client.get("/api/games/ai_does_not_exist_xxx/assets", headers=h)
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# GET /assets/{filename} — serve binary
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_asset_returns_image_bytes(client, alice_with_game):
    assets_dir = alice_with_game["game_dir"] / "assets"
    assets_dir.mkdir()
    raw = _png_bytes(w=64, h=64, color=(0, 255, 0))
    (assets_dir / "bg.png").write_bytes(raw)

    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/assets/bg.png", headers=h
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == raw


@pytest.mark.asyncio
async def test_get_asset_path_traversal_rejected(client, alice_with_game):
    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/assets/..%2Fmeta.json",
        headers=h,
    )
    # 400 是显式 reject；某些路径会被 FastAPI 自身 normalize 到 404
    assert resp.status_code in {400, 404}


@pytest.mark.asyncio
async def test_get_asset_non_owner_403(client, alice_with_game, bob_user):
    assets_dir = alice_with_game["game_dir"] / "assets"
    assets_dir.mkdir()
    (assets_dir / "bg.png").write_bytes(_png_bytes())
    h = auth_header(bob_user["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/assets/bg.png", headers=h
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_asset_missing_returns_404(client, alice_with_game):
    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/assets/nonexistent.png",
        headers=h,
    )
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# GET /used_audio — list AI-included BGM tracks
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_used_audio_empty_when_no_audio_dir(client, alice_with_game):
    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/used_audio", headers=h
    )
    assert resp.status_code == 200
    assert resp.json() == {"audio": []}


@pytest.mark.asyncio
async def test_used_audio_lists_ogg_files(client, alice_with_game):
    audio_dir = alice_with_game["game_dir"] / "audio"
    audio_dir.mkdir()
    (audio_dir / "8bit_loop.ogg").write_bytes(b"FAKE OGG DATA")
    (audio_dir / "chill_pad.ogg").write_bytes(b"FAKE OGG DATA")
    (audio_dir / "readme.txt").write_text("ignored")

    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/used_audio", headers=h
    )
    assert resp.status_code == 200
    items = resp.json()["audio"]
    ids = [it["id"] for it in items]
    assert "8bit_loop" in ids
    assert "chill_pad" in ids
    # 非 .ogg 不收
    assert "readme" not in ids
    # 每条带 preview_url（全局 BGM manifest 中的走 /audio/bgm-preview/，
    # AI 自定义音频走 /{game_id}/audio/，两种格式都合法）
    for it in items:
        assert it["preview_url"].startswith("/api/games/")


@pytest.mark.asyncio
async def test_used_audio_non_owner_403(client, alice_with_game, bob_user):
    h = auth_header(bob_user["access_token"])
    resp = await client.get(
        f"/api/games/{alice_with_game['game_id']}/used_audio", headers=h
    )
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────
# POST /assets?replace=true — overwrite existing asset
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replace_overwrites_existing_asset(client, alice_with_game):
    """亮度调整流程：上传同名文件并加 replace=true → 直接覆盖原文件，
    不加 _2 后缀。"""
    assets_dir = alice_with_game["game_dir"] / "assets"
    assets_dir.mkdir()
    original = _png_bytes(color=(255, 0, 0))
    (assets_dir / "bg.png").write_bytes(original)

    new_bytes = _png_bytes(color=(0, 0, 255))
    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.post(
        f"/api/games/{alice_with_game['game_id']}/assets?replace=true",
        headers=h,
        files=[("images", ("bg.png", new_bytes, "image/png"))],
    )
    assert resp.status_code == 200
    assert resp.json()["filenames"] == ["bg.png"]   # 没有 _2 后缀
    # 原 bg.png 已被新 bytes 覆盖
    assert (assets_dir / "bg.png").read_bytes() == new_bytes


@pytest.mark.asyncio
async def test_upload_without_replace_still_dedupes(client, alice_with_game):
    """没指定 replace 时仍然走 dedup。"""
    assets_dir = alice_with_game["game_dir"] / "assets"
    assets_dir.mkdir()
    (assets_dir / "bg.png").write_bytes(_png_bytes(color=(255, 0, 0)))

    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.post(
        f"/api/games/{alice_with_game['game_id']}/assets",
        headers=h,
        files=[("images", ("bg.png", _png_bytes(color=(0, 255, 0)), "image/png"))],
    )
    assert resp.status_code == 200
    assert resp.json()["filenames"] == ["bg_2.png"]
    assert (assets_dir / "bg.png").exists()
    assert (assets_dir / "bg_2.png").exists()
