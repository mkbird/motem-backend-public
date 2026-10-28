"""Game asset upload endpoint tests — POST /api/games/{id}/assets.

Coverage matrix:
- owner happy path: PNG / JPEG / WebP all accepted, files land in assets/
- non-owner 403, builtin 403, invalid game_id 404, unauthenticated 401
- filename sanitization: special chars + path traversal stripped
- filename de-dup against existing files + within same batch
- max batch (>10) → 400
- max single file size (>8MB) → 413
- non-image content-type → 415
- post-upload zip is repacked and contains assets/<file>
- updated_at on ai_games is bumped after upload
- _build_assets_section helper enumerates files for the agent prompt
- empty assets/ dir produces empty section
"""

import io
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
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


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _png_bytes(w: int = 64, h: int = 64, color=(255, 0, 0)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(w: int = 64, h: int = 64, color=(0, 255, 0)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _webp_bytes(w: int = 64, h: int = 64, color=(0, 0, 255)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────
# Fixtures (mirrors test_game_cover.py pattern)
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def alice_with_game(db_session: AsyncSession, client: AsyncClient):
    from robot.games import service as game_service

    alice = await register_and_login(client, email="alice_assets@example.com")
    game_id = "ai_test_alice_assets_001"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=alice["user"]["id"],
        title="Alice Assets Test",
        title_i18n=json.dumps({"en": "Alice Assets Test", "zh-Hans": "资源测试"}),
        description="assets test",
        description_i18n=json.dumps({"en": "assets test", "zh-Hans": "资源测试"}),
        icon="🎮",
        input_mode="touch",
        kind="ai",
        visibility="public_remixable",
        original_game_id=None,
        template_from="",
        session_id=None,
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
        '[application]\nconfig/name="Assets Test"\n', encoding="utf-8"
    )
    (src / "meta.json").write_text(
        json.dumps({"game_id": game_id, "title": "Alice Assets Test"}),
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
    return await register_and_login(client, email="bob_assets@example.com")


@pytest_asyncio.fixture
async def builtin_game(db_session: AsyncSession):
    """A builtin game row — should reject asset upload."""
    game_id = "builtin_test_assets"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=None,
        title="Builtin",
        title_i18n=json.dumps({"en": "Builtin", "zh-Hans": "内置"}),
        description="",
        description_i18n=json.dumps({"en": "", "zh-Hans": ""}),
        icon="🎮",
        input_mode="touch",
        kind="builtin",
        visibility="public_view_only",
        original_game_id=None, template_from="", session_id=None,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    await db_session.commit()
    yield game_id


# ─────────────────────────────────────────────────────────────────────
# Section A — Happy paths
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_upload_two_pngs_succeeds(client: AsyncClient, alice_with_game):
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    files = [
        ("images", ("space_bg.png", _png_bytes(), "image/png")),
        ("images", ("ship.png", _png_bytes(), "image/png")),
    ]
    resp = await client.post(
        f"/api/games/{gid}/assets",
        headers=h,
        files=files,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filenames"] == ["space_bg.png", "ship.png"]

    assets_dir = alice_with_game["game_dir"] / "assets"
    assert (assets_dir / "space_bg.png").exists()
    assert (assets_dir / "ship.png").exists()


@pytest.mark.asyncio
async def test_jpeg_supported(client: AsyncClient, alice_with_game):
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [("images", ("bg.jpg", _jpeg_bytes(), "image/jpeg"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200
    assert (alice_with_game["game_dir"] / "assets" / "bg.jpg").exists()


@pytest.mark.asyncio
async def test_webp_supported(client: AsyncClient, alice_with_game):
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [("images", ("bg.webp", _webp_bytes(), "image/webp"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200
    assert (alice_with_game["game_dir"] / "assets" / "bg.webp").exists()


@pytest.mark.asyncio
async def test_zip_repacked_after_upload(client: AsyncClient, alice_with_game):
    """上传后 zip 文件被重打，且 zip 内含 assets/<file>。"""
    from robot.games import service as game_service

    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [("images", ("bg.png", _png_bytes(), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200

    zip_path = game_service.GAMES_DIR / f"{gid}.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert "assets/bg.png" in names


@pytest.mark.asyncio
async def test_upload_bumps_updated_at(
    client: AsyncClient, alice_with_game, db_session: AsyncSession,
):
    """Upload → ai_games.updated_at 必须刷新（触发 iOS 内容自动同步）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    row_before = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == gid))
    ).scalar_one()
    before_ts = row_before.updated_at

    # MySQL DATETIME 是秒精度，sleep 跨秒
    import asyncio
    await asyncio.sleep(1.1)

    files = [("images", ("bg.png", _png_bytes(), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200

    # 重新查询，强制 refresh 拿最新 updated_at（router 在另一个 session 提交了改动）
    db_session.expire_all()
    row_after = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == gid))
    ).scalar_one()
    assert row_after.updated_at > before_ts


# ─────────────────────────────────────────────────────────────────────
# Section B — Auth / ownership / kind
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthenticated_returns_401(client: AsyncClient, alice_with_game):
    gid = alice_with_game["game_id"]
    files = [("images", ("bg.png", _png_bytes(), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", files=files)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_owner_returns_403(
    client: AsyncClient, alice_with_game, bob_user,
):
    gid = alice_with_game["game_id"]
    h = auth_header(bob_user["access_token"])
    files = [("images", ("bg.png", _png_bytes(), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_invalid_game_id_returns_404(client: AsyncClient):
    user = await register_and_login(client, email="lonely_user@example.com")
    h = auth_header(user["access_token"])
    files = [("images", ("bg.png", _png_bytes(), "image/png"))]
    resp = await client.post(
        "/api/games/ai_does_not_exist_xxx/assets", headers=h, files=files,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_builtin_game_returns_403(
    client: AsyncClient, builtin_game, db_session: AsyncSession,
):
    user = await register_and_login(client, email="builtin_assets@example.com")
    h = auth_header(user["access_token"])
    files = [("images", ("bg.png", _png_bytes(), "image/png"))]
    resp = await client.post(
        f"/api/games/{builtin_game}/assets", headers=h, files=files,
    )
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────
# Section C — Filename sanitization & dedup
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filename_special_chars_sanitized(client: AsyncClient, alice_with_game):
    """带空格 / 中文 / 特殊符号的文件名应被安全化。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    # Upload filename with spaces, capitals, and CJK
    files = [("images", ("My Cool 太空 BG!.png", _png_bytes(), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200
    final_names = resp.json()["filenames"]
    assert len(final_names) == 1
    name = final_names[0]
    # Must be lowercase, no spaces, no CJK, no `!`
    assert name.islower()
    assert " " not in name
    assert "!" not in name
    assert all(ord(c) < 128 for c in name), f"non-ASCII: {name}"
    assert name.endswith(".png")


@pytest.mark.asyncio
async def test_path_traversal_stripped(client: AsyncClient, alice_with_game):
    """`../../etc/passwd.png` → 安全化为 `etc_passwd.png` 或类似（绝不能逃出 assets/）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [("images", ("../../etc/passwd.png", _png_bytes(), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200
    name = resp.json()["filenames"][0]
    # File should be in assets/ NOT outside
    assets_dir = alice_with_game["game_dir"] / "assets"
    assert (assets_dir / name).exists()
    # No `..` or `/` in final name
    assert ".." not in name
    assert "/" not in name


@pytest.mark.asyncio
async def test_filename_dedup_within_batch(client: AsyncClient, alice_with_game):
    """单批两个同名文件 → 第二个加 _2 后缀。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [
        ("images", ("bg.png", _png_bytes(color=(255, 0, 0)), "image/png")),
        ("images", ("bg.png", _png_bytes(color=(0, 255, 0)), "image/png")),
    ]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200
    names = resp.json()["filenames"]
    assert names == ["bg.png", "bg_2.png"]
    assets_dir = alice_with_game["game_dir"] / "assets"
    assert (assets_dir / "bg.png").exists()
    assert (assets_dir / "bg_2.png").exists()


@pytest.mark.asyncio
async def test_filename_dedup_against_existing_file(client: AsyncClient, alice_with_game):
    """已有 bg.png 时再上传 bg.png → 得到 bg_2.png。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    # Pre-create assets/bg.png on disk
    assets_dir = alice_with_game["game_dir"] / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "bg.png").write_bytes(_png_bytes(color=(0, 0, 0)))

    files = [("images", ("bg.png", _png_bytes(color=(255, 0, 0)), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200
    assert resp.json()["filenames"] == ["bg_2.png"]


@pytest.mark.asyncio
async def test_filename_default_when_no_hint(client: AsyncClient, alice_with_game):
    """upload 文件名为意外字符 → 安全化失败回 fallback bg_N.<ext>。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    # 全 CJK 文件名 → sanitize 后只剩 ext，进 fallback 默认 bg_<idx>.png
    files = [("images", ("中文背景.png", _png_bytes(), "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 200
    name = resp.json()["filenames"][0]
    assert name.startswith("bg_")
    assert name.endswith(".png")


# ─────────────────────────────────────────────────────────────────────
# Section D — Limits
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_too_many_assets_rejected(client: AsyncClient, alice_with_game):
    """单次超过 10 张 → 400。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [
        ("images", (f"bg_{i}.png", _png_bytes(), "image/png"))
        for i in range(11)
    ]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oversized_image_rejected(client: AsyncClient, alice_with_game):
    """单图超 8MB → 413。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    big = b"\x89PNG\r\n\x1a\n" + b"A" * (9 * 1024 * 1024)  # not a real png but tests the size check
    files = [("images", ("big.png", big, "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_non_image_content_type_rejected(client: AsyncClient, alice_with_game):
    """text/plain 等 → 415。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [("images", ("evil.txt", b"hello world", "text/plain"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_corrupted_image_rejected(client: AsyncClient, alice_with_game):
    """content-type 是 image 但 bytes 不是有效图 → PIL.verify 抛 → 400。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    files = [("images", ("bad.png", b"this is not a real png", "image/png"))]
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=files)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_no_images_provided(client: AsyncClient, alice_with_game):
    """空 images list → 400."""
    # FastAPI will 422 if images list field missing entirely; explicit empty
    # files list is hard to send via httpx directly so we test the implicit
    # case where someone POSTs no `images` at all → 422.
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.post(f"/api/games/{gid}/assets", headers=h, files=[])
    # Without `images` field → FastAPI complains 422
    assert resp.status_code in {400, 422}


# ─────────────────────────────────────────────────────────────────────
# Section E — Agent prompt enumeration helper
# ─────────────────────────────────────────────────────────────────────


def test_build_assets_section_lists_files(tmp_path):
    """_build_assets_section enumerates assets/ files in alphabetical order."""
    from robot.agent.tools.game_tools import _build_assets_section

    game_dir = tmp_path / "g"
    game_dir.mkdir()
    assets = game_dir / "assets"
    assets.mkdir()
    (assets / "bg.png").write_bytes(_png_bytes())
    (assets / "ship.jpg").write_bytes(_jpeg_bytes())
    (assets / "ignore.txt").write_text("not an image")

    section = _build_assets_section(game_dir)
    # section 标题历史是 "## User-uploaded assets"，现已重命名为
    # "## Image files available in `assets/`"。两个表述都接受，避免后续
    # 文案微调再次破测试。
    assert "assets/" in section and ("Image files" in section or "User-uploaded" in section)
    assert "assets/bg.png" in section
    assert "assets/ship.jpg" in section
    # txt files are ignored
    assert "ignore.txt" not in section


def test_build_assets_section_empty_when_no_files(tmp_path):
    from robot.agent.tools.game_tools import _build_assets_section
    game_dir = tmp_path / "g"
    game_dir.mkdir()
    # No assets dir at all
    assert _build_assets_section(game_dir) == ""
    # Empty assets dir
    (game_dir / "assets").mkdir()
    assert _build_assets_section(game_dir) == ""


def test_build_assets_section_includes_load_hint(tmp_path):
    from robot.agent.tools.game_tools import _build_assets_section
    game_dir = tmp_path / "g"
    game_dir.mkdir()
    (game_dir / "assets").mkdir()
    (game_dir / "assets" / "first.png").write_bytes(_png_bytes())
    section = _build_assets_section(game_dir)
    assert "load(\"res://assets/" in section
    assert "load(\"res://assets/first.png\")" in section


# ─────────────────────────────────────────────────────────────────────
# Section F — Helper unit tests for filename sanitization
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("input_name,expected", [
    ("bg.png", "bg.png"),
    ("BG.PNG", "bg.png"),
    ("my cool bg.jpg", "my_cool_bg.jpg"),
    # 路径组件被 strip 掉，只保留最后一段
    ("../etc/passwd.png", "passwd.png"),
    ("/abs/path/to/image.png", "image.png"),
    ("with___multiple___underscores.png", "with_multiple_underscores.png"),
    # CJK-only stem → strip 后只剩 ext → reject (返回 "")
    ("中文背景.png", ""),
    # 含 CJK 但有 ASCII stem 的部分应保留 ASCII 部分
    ("bg中文.png", "bg_.png"),
])
def test_sanitize_filename_normalizations(input_name, expected):
    """白盒：直接调 helper 验证清洗规则。"""
    from robot.games.router import _sanitize_asset_filename
    result = _sanitize_asset_filename(input_name)
    assert result == expected, f"input={input_name!r}: got {result!r}, expected {expected!r}"


@pytest.mark.parametrize("bad_input", [
    "",
    ".",
    "..",
    "no_extension",
    "wrongext.exe",
    "wrongext.txt",
    "wrongext.html",
])
def test_sanitize_filename_rejects(bad_input):
    from robot.games.router import _sanitize_asset_filename
    assert _sanitize_asset_filename(bad_input) == ""
