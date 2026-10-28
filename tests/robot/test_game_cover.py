"""Game cover endpoint tests — POST /api/games/{id}/cover + GET /cover.

覆盖矩阵：
- owner 上传成功 → cover.png 写入 + zip 重打 + cover_url 出现在 list 响应
- 非 owner 上传 403
- builtin 上传 403
- 超过 5MB 413
- 非图片 content-type 415
- 损坏图片 400
- 上传后 GET 200 含 cache header
- 删除游戏后 GET 404
- private 游戏 GET 仅 owner（非 owner 404）
- public_remixable 游戏所有人 GET 200
- list endpoint 的 cover_url 字段：cover 存在时给 url，不存在时 None

**不裁切**只缩放：上传图保持原比例，最大宽度压到 1280。
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import AIGame
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


def _make_image_bytes(width: int = 1024, height: int = 768, color: tuple = (255, 0, 0)) -> bytes:
    """生成一张纯色 PNG 用于上传。"""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload_files(image_bytes: bytes, filename: str = "cover.png", content_type: str = "image/png"):
    return {"image": (filename, image_bytes, content_type)}


# ─────────────────────────────────────────────────────────────────────
# Fixtures (复用 marketplace 的方式，alice + bob + alice_game_with_dir)
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def alice_with_game(db_session: AsyncSession, client: AsyncClient):
    """注册 alice + 创建一个 public_remixable 游戏 + 写 fs 目录。"""
    from robot.games import service as game_service

    alice = await register_and_login(client, email="alice_cover@example.com")
    game_id = "ai_test_alice_cover_001"
    now = __import__("datetime").datetime.utcnow()
    from robot.db.models import ExploreGameView, UserGameView
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=alice["user"]["id"],
        title="Alice Cover Test",
        title_i18n=json.dumps({"en": "Alice Cover Test", "zh-Hans": "封面测试"}),
        description="cover test",
        description_i18n=json.dumps({"en": "cover test", "zh-Hans": "封面测试"}),
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
    # public_remixable 必须在 explore_game_view 才出现在 /explore；alice 自创
    # 也要在 user_game_view 才出现在她的 /mine
    db_session.add(ExploreGameView(game_id=game_id, added_at=now))
    db_session.add(UserGameView(
        user_id=alice["user"]["id"], game_id=game_id,
        view_type="created", added_at=now,
    ))
    await db_session.commit()

    # 写 fs 目录 + meta.json + 一个 zip 让重打 zip 路径不空
    src = game_service.GAMES_DIR / game_id
    src.mkdir(parents=True, exist_ok=True)
    (src / "project.godot").write_text(
        '[application]\nconfig/name="Cover Test"\n', encoding="utf-8"
    )
    (src / "meta.json").write_text(
        json.dumps({"game_id": game_id, "title": "Alice Cover Test"}),
        encoding="utf-8",
    )

    yield {
        "alice": alice,
        "game_id": game_id,
        "game_dir": src,
    }

    # cleanup
    import shutil as _sh
    if src.is_dir():
        _sh.rmtree(src, ignore_errors=True)
    z = game_service.GAMES_DIR / f"{game_id}.zip"
    if z.exists():
        z.unlink()


@pytest_asyncio.fixture
async def bob_user(client: AsyncClient):
    return await register_and_login(client, email="bob_cover@example.com")


# ─────────────────────────────────────────────────────────────────────
# Section A — Upload happy path & resize
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_upload_cover_writes_png_and_repacks_zip(
    client: AsyncClient, alice_with_game: dict,
):
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    # 上传 1024×768 红色 PNG
    img_bytes = _make_image_bytes(1024, 768, color=(255, 0, 0))
    resp = await client.post(
        f"/api/games/{gid}/cover", headers=h, files=_upload_files(img_bytes),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["game_id"] == gid
    assert body["cover_url"] == f"/api/games/{gid}/cover"
    assert body["cover_set_at"] > 0

    # 文件落盘 + 服务端按原比例 resize（max width 1280，不裁切）
    cover_path = alice_with_game["game_dir"] / "cover.png"
    assert cover_path.exists()
    saved = Image.open(cover_path)
    # 原 1024×768 < 1280 max width → 不缩放，保持原尺寸
    assert saved.size == (1024, 768)
    assert abs(saved.size[0]/saved.size[1] - 1024/768) < 0.01  # 比例不变

    # zip 重打 + 含 cover.png
    from robot.games import service as game_service
    zip_path = game_service.GAMES_DIR / f"{gid}.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    assert "cover.png" in names


@pytest.mark.asyncio
async def test_upload_replaces_previous_cover(
    client: AsyncClient, alice_with_game: dict,
):
    """两次上传 → 第二次覆盖第一次（mtime 变化、内容变化）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    r1 = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(800, 450, color=(255, 0, 0))),
    )
    assert r1.status_code == 200
    cover_path = alice_with_game["game_dir"] / "cover.png"
    first_mtime = cover_path.stat().st_mtime
    first_bytes = cover_path.read_bytes()

    # 间隔一下让 mtime 不同
    import time
    time.sleep(0.01)

    r2 = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(800, 450, color=(0, 255, 0))),
    )
    assert r2.status_code == 200
    second_mtime = cover_path.stat().st_mtime
    second_bytes = cover_path.read_bytes()

    assert second_mtime >= first_mtime
    assert first_bytes != second_bytes


# ─────────────────────────────────────────────────────────────────────
# Section B — Permission
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_owner_upload_403(
    client: AsyncClient, alice_with_game: dict, bob_user: dict,
):
    gid = alice_with_game["game_id"]
    resp = await client.post(
        f"/api/games/{gid}/cover",
        headers=auth_header(bob_user["access_token"]),
        files=_upload_files(_make_image_bytes()),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_builtin_upload_403(
    client: AsyncClient, db_session: AsyncSession, alice_with_game: dict,
):
    """builtin 游戏不允许上传 cover。"""
    from datetime import datetime
    builtin_id = "builtin_test_cover"
    db_session.add(AIGame(
        game_id=builtin_id,
        owner_id=None,
        title="Builtin",
        title_i18n=json.dumps({"en": "B", "zh-Hans": "B"}),
        description="b",
        description_i18n=json.dumps({"en": "b", "zh-Hans": "b"}),
        icon="🎮",
        input_mode="touch",
        kind="builtin",
        visibility="public_view_only",
        original_game_id=None,
        template_from="",
        session_id=None,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))
    await db_session.commit()

    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.post(
        f"/api/games/{builtin_id}/cover",
        headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_upload_unauth_401(
    client: AsyncClient, alice_with_game: dict,
):
    resp = await client.post(
        f"/api/games/{alice_with_game['game_id']}/cover",
        files=_upload_files(_make_image_bytes()),
    )
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Section C — Validation
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_too_large_413(
    client: AsyncClient, alice_with_game: dict,
):
    """6MB 数据应被拒绝（上限 5MB）。"""
    h = auth_header(alice_with_game["alice"]["access_token"])
    big = b"\x89PNG\r\n\x1a\n" + (b"X" * (6 * 1024 * 1024))  # 假装是 PNG header + 6MB 填充
    resp = await client.post(
        f"/api/games/{alice_with_game['game_id']}/cover",
        headers=h,
        files=_upload_files(big),
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_upload_wrong_content_type_415(
    client: AsyncClient, alice_with_game: dict,
):
    h = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.post(
        f"/api/games/{alice_with_game['game_id']}/cover",
        headers=h,
        files={"image": ("notimg.txt", b"not an image", "text/plain")},
    )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_upload_corrupted_image_400(
    client: AsyncClient, alice_with_game: dict,
):
    h = auth_header(alice_with_game["alice"]["access_token"])
    # content-type 谎称 image/png 但实际是垃圾字节
    resp = await client.post(
        f"/api/games/{alice_with_game['game_id']}/cover",
        headers=h,
        files=_upload_files(b"not a real image", content_type="image/png"),
    )
    assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────
# Section D — GET cover
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_cover_after_upload(
    client: AsyncClient, alice_with_game: dict,
):
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(800, 450)),
    )

    # 公开下载（不带 auth）
    resp = await client.get(f"/api/games/{gid}/cover")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "max-age=300" in resp.headers.get("cache-control", "")
    assert resp.headers.get("etag", "").startswith('W/"cover-')


@pytest.mark.asyncio
async def test_get_cover_404_when_missing(
    client: AsyncClient, alice_with_game: dict,
):
    """没上传过 cover → GET 404。"""
    gid = alice_with_game["game_id"]
    resp = await client.get(f"/api/games/{gid}/cover")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_cover_private_owner_only(
    client: AsyncClient, db_session: AsyncSession, alice_with_game: dict, bob_user: dict,
):
    """private 游戏的 cover 只 owner 可下载。"""
    gid = alice_with_game["game_id"]
    h_alice = auth_header(alice_with_game["alice"]["access_token"])
    # 上传 cover
    await client.post(
        f"/api/games/{gid}/cover", headers=h_alice,
        files=_upload_files(_make_image_bytes()),
    )
    # 改 visibility 为 private
    row = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == gid))
    ).scalar_one()
    row.visibility = "private"
    await db_session.commit()

    # owner 拿得到
    r_owner = await client.get(f"/api/games/{gid}/cover", headers=h_alice)
    assert r_owner.status_code == 200

    # bob 拿不到
    r_bob = await client.get(
        f"/api/games/{gid}/cover", headers=auth_header(bob_user["access_token"]),
    )
    assert r_bob.status_code == 404

    # 匿名也拿不到
    r_anon = await client.get(f"/api/games/{gid}/cover")
    assert r_anon.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Section E — List endpoint cover_url 字段
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_explore_cover_url_present_after_upload(
    client: AsyncClient, alice_with_game: dict,
):
    """上传 cover 后 /explore 响应该游戏的 cover_url 有值；没上传时为 None。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    # 上传前：cover_url = None
    r1 = await client.get("/api/games/explore", headers=h)
    target1 = next((g for g in r1.json()["games"] if g["game_id"] == gid), None)
    assert target1 is not None
    assert target1["cover_url"] is None

    # 上传
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )

    # 上传后：cover_url = "/api/games/{gid}/cover"
    r2 = await client.get("/api/games/explore", headers=h)
    target2 = next(g for g in r2.json()["games"] if g["game_id"] == gid)
    assert target2["cover_url"] == f"/api/games/{gid}/cover"


@pytest.mark.asyncio
async def test_delete_game_removes_cover(
    client: AsyncClient, alice_with_game: dict,
):
    """删除游戏后 cover 也消失（cover.png 在 game_dir 里被 rmtree 带走）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    # 删游戏
    r_del = await client.delete(f"/api/games/{gid}", headers=h)
    assert r_del.status_code == 200
    # cover 404
    r_cover = await client.get(f"/api/games/{gid}/cover", headers=h)
    assert r_cover.status_code == 404


@pytest.mark.asyncio
async def test_list_mine_cover_url_present(
    client: AsyncClient, alice_with_game: dict,
):
    """/mine 端点也带 cover_url。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    r = await client.get("/api/games/mine", headers=h)
    target = next((g for g in r.json()["games"] if g["game_id"] == gid), None)
    assert target is not None
    assert target["cover_url"] == f"/api/games/{gid}/cover"


# ─────────────────────────────────────────────────────────────────────
# Section F — Resize-only（保持原比例，max width 1280）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_portrait_image_keeps_aspect(
    client: AsyncClient, alice_with_game: dict,
):
    """竖屏 720×1280 → 比例不变，宽 ≤ 1280。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    img_bytes = _make_image_bytes(720, 1280, color=(0, 0, 255))
    r = await client.post(
        f"/api/games/{gid}/cover", headers=h, files=_upload_files(img_bytes),
    )
    assert r.status_code == 200
    saved = Image.open(alice_with_game["game_dir"] / "cover.png")
    # 720 < 1280 → 不缩放
    assert saved.size == (720, 1280)


@pytest.mark.asyncio
async def test_upload_square_image_keeps_square(
    client: AsyncClient, alice_with_game: dict,
):
    """800×800 → 仍然 800×800（不裁切，比例不变）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    r = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(800, 800)),
    )
    assert r.status_code == 200
    assert Image.open(alice_with_game["game_dir"] / "cover.png").size == (800, 800)


@pytest.mark.asyncio
async def test_upload_tiny_image_unchanged(
    client: AsyncClient, alice_with_game: dict,
):
    """100×56 → 不上采样（小于 max width 直接保留原尺寸）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    r = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(100, 56)),
    )
    assert r.status_code == 200
    assert Image.open(alice_with_game["game_dir"] / "cover.png").size == (100, 56)


@pytest.mark.asyncio
async def test_upload_huge_image_downscaled_to_1280(
    client: AsyncClient, alice_with_game: dict,
):
    """全屏截图 2868×1320（iPhone 16 Pro Max landscape）→ 缩到 1280×~590（保比例）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    r = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(2868, 1320)),
    )
    assert r.status_code == 200
    saved = Image.open(alice_with_game["game_dir"] / "cover.png")
    assert saved.size[0] == 1280
    # 比例 ≈ 2868/1320 = 2.173；缩后应为 1280 / 2.173 ≈ 589
    assert abs(saved.size[1] - 589) <= 1  # 取整误差
    assert abs((saved.size[0] / saved.size[1]) - (2868 / 1320)) < 0.01


@pytest.mark.asyncio
async def test_upload_jpeg_content_type(
    client: AsyncClient, alice_with_game: dict,
):
    """image/jpeg 也允许，PIL 解码后落盘统一为 PNG。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    img = Image.new("RGB", (1024, 768), color=(128, 128, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    files = {"image": ("cover.jpg", buf.getvalue(), "image/jpeg")}
    r = await client.post(f"/api/games/{gid}/cover", headers=h, files=files)
    assert r.status_code == 200
    # 落盘是 PNG，PIL 自动转 RGB（不再 crop，保持原 1024×768）
    saved_path = alice_with_game["game_dir"] / "cover.png"
    saved = Image.open(saved_path)
    assert saved.format == "PNG"
    assert saved.size == (1024, 768)


@pytest.mark.asyncio
async def test_upload_png_with_alpha_converted_to_rgb(
    client: AsyncClient, alice_with_game: dict,
):
    """RGBA 输入 → 服务端 .convert('RGB') 保证落盘 PNG 没 alpha 通道。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    img = Image.new("RGBA", (1024, 768), color=(255, 0, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    r = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files={"image": ("cover.png", buf.getvalue(), "image/png")},
    )
    assert r.status_code == 200
    saved = Image.open(alice_with_game["game_dir"] / "cover.png")
    # RGB mode（无 alpha）
    assert saved.mode == "RGB"


# ─────────────────────────────────────────────────────────────────────
# Section G — Cache header / ETag / Zip 完整性
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zip_contains_cover_after_repack(
    client: AsyncClient, alice_with_game: dict,
):
    """重打 zip 必含 cover.png 在 root。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    from robot.games import service as game_service
    zip_path = game_service.GAMES_DIR / f"{gid}.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    assert "cover.png" in names
    # 解压出来能正常读
    with zipfile.ZipFile(zip_path, "r") as zf:
        png_bytes = zf.read("cover.png")
    img = Image.open(io.BytesIO(png_bytes))
    # 默认 _make_image_bytes 是 1024×768，resize-only 不缩到 1280 max → 保持原尺寸
    assert img.size == (1024, 768)


@pytest.mark.asyncio
async def test_cover_set_at_matches_file_mtime(
    client: AsyncClient, alice_with_game: dict,
):
    """upload 响应的 cover_set_at 必须等于落盘文件的 mtime（同一时间源）。
    防止 list 端点用文件 mtime 而 upload 响应用 utcnow，二者对不上。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    r = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    assert r.status_code == 200
    response_set_at = r.json()["cover_set_at"]
    cover_path = alice_with_game["game_dir"] / "cover.png"
    file_mtime = int(cover_path.stat().st_mtime)
    assert response_set_at == file_mtime
    # 同时 list 端点的字段也应等
    r2 = await client.get("/api/games/explore", headers=h)
    target = next(g for g in r2.json()["games"] if g["game_id"] == gid)
    assert target["cover_set_at"] == response_set_at


@pytest.mark.asyncio
async def test_get_cover_404_when_db_row_missing(
    client: AsyncClient, db_session: AsyncSession, alice_with_game: dict,
):
    """fs 上有 cover.png 但 ai_games DB 行不存在 → GET 必须 404。
    防御 _check_files_access 在 row=None 时跳过 visibility 校验，
    导致私有游戏的 cover 被裸下载（visibility 校验绕过漏洞）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    # 上传 cover 让 cover.png 落盘
    r0 = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    assert r0.status_code == 200
    cover_path = alice_with_game["game_dir"] / "cover.png"
    assert cover_path.exists()

    # 删 DB 行（模拟老 ai_* 没回灌，但 fs 上还有目录）— 用同一个
    # db_session 才看得到 fixture 在该事务里创建的行
    row = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == gid))
    ).scalar_one()
    await db_session.delete(row)
    await db_session.commit()

    # GET 应该 404，不能裸下载（防御 visibility 校验绕过）
    r = await client.get(f"/api/games/{gid}/cover")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_etag_changes_on_reupload(
    client: AsyncClient, alice_with_game: dict,
):
    """第二次上传 → ETag 应改变（mtime 不同）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(color=(255, 0, 0))),
    )
    r1 = await client.get(f"/api/games/{gid}/cover")
    etag1 = r1.headers.get("etag")
    assert etag1

    import time
    time.sleep(1.1)  # mtime 是秒级精度，需要 ≥1s

    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes(color=(0, 255, 0))),
    )
    r2 = await client.get(f"/api/games/{gid}/cover")
    etag2 = r2.headers.get("etag")
    assert etag2 and etag2 != etag1


# ─────────────────────────────────────────────────────────────────────
# Section H — Edge cases
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_to_nonexistent_game_404(
    client: AsyncClient, alice_with_game: dict,
):
    """不存在的 game_id POST → 404。"""
    h = auth_header(alice_with_game["alice"]["access_token"])
    r = await client.post(
        "/api/games/ai_does_not_exist/cover",
        headers=h, files=_upload_files(_make_image_bytes()),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_cover_for_builtin_404(
    client: AsyncClient, db_session: AsyncSession,
):
    """builtin 游戏没有 cover.png 文件 → GET 直接 404。"""
    from datetime import datetime
    builtin_id = "builtin_no_cover"
    db_session.add(AIGame(
        game_id=builtin_id, owner_id=None,
        title="B", title_i18n=json.dumps({"en": "B", "zh-Hans": "B"}),
        description="b", description_i18n=json.dumps({"en": "b", "zh-Hans": "b"}),
        icon="🎮", input_mode="touch", kind="builtin",
        visibility="public_view_only", original_game_id=None,
        template_from="", session_id=None,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))
    await db_session.commit()
    r = await client.get(f"/api/games/{builtin_id}/cover")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cover_url_in_legacy_endpoint(
    client: AsyncClient, alice_with_game: dict,
):
    """老 GET /api/games endpoint（向后兼容旧 iOS）也带 cover_url。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    r = await client.get("/api/games", headers=h)
    target = next((g for g in r.json()["games"] if g["game_id"] == gid), None)
    assert target is not None
    assert target["cover_url"] == f"/api/games/{gid}/cover"


@pytest.mark.asyncio
async def test_list_response_includes_content_modified_at(
    client: AsyncClient, alice_with_game: dict,
):
    """list endpoint 响应里有 content_modified_at 字段（取 meta.json.modified_at
    或 zip mtime）。客户端用它判断本地内容是否过期、决定是否 re-download。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    # 写一个 modified_at 到 meta.json
    import json as _json
    meta_path = alice_with_game["game_dir"] / "meta.json"
    meta_path.write_text(_json.dumps({
        "game_id": gid, "title": "Cover Test", "modified_at": 1777380000,
    }), encoding="utf-8")

    r = await client.get("/api/games/explore", headers=h)
    target = next(g for g in r.json()["games"] if g["game_id"] == gid)
    assert target.get("content_modified_at") == 1777380000


@pytest.mark.asyncio
async def test_content_modified_at_falls_back_to_zip_mtime(
    client: AsyncClient, alice_with_game: dict,
):
    """meta.json 没 modified_at 字段（老 ai_*）→ 回退用 zip 文件 mtime。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])

    # 上传 cover 让 zip 重打（拿到一个确定的 zip）
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )

    # meta.json 不带 modified_at
    import json as _json
    meta_path = alice_with_game["game_dir"] / "meta.json"
    meta_path.write_text(_json.dumps({
        "game_id": gid, "title": "Cover Test"
    }), encoding="utf-8")

    from robot.games import service as game_service
    zip_path = game_service.GAMES_DIR / f"{gid}.zip"
    expected = int(zip_path.stat().st_mtime)

    r = await client.get("/api/games/explore", headers=h)
    target = next(g for g in r.json()["games"] if g["game_id"] == gid)
    assert target.get("content_modified_at") == expected


@pytest.mark.asyncio
async def test_upload_decode_bomb_rejected(
    client: AsyncClient, alice_with_game: dict,
):
    """超大像素图 → 服务端在 PIL decode 之前先校验 size，防 decode bomb OOM。
    5000×5000 PNG 压缩后约 100KB（纯色），但 decode 后占 75MB 内存。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    img = Image.new("RGB", (5000, 5000), color=(0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    payload = buf.getvalue()
    # 确保 payload 仍在 5MB 内（不该被 size 上限拦截）
    assert len(payload) < 5 * 1024 * 1024, f"payload {len(payload)}B too large for size test"
    r = await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files={"image": ("bomb.png", payload, "image/png")},
    )
    assert r.status_code == 400, "5000×5000 = 25MP 应被像素上限拒绝"


@pytest.mark.asyncio
async def test_explore_cover_fields_consistent_across_cache_hits(
    client: AsyncClient, alice_with_game: dict,
):
    """连续两次 GET /explore：第一次 cache miss 写入 redis，第二次 cache hit
    从 redis 读。两次响应的 cover_url / cover_set_at / content_modified_at 必须
    完全一致 — 这些字段随 _row_to_dict 一起缓存，cache hit 不应再做 file IO。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    await client.post(
        f"/api/games/{gid}/cover", headers=h,
        files=_upload_files(_make_image_bytes()),
    )
    r1 = await client.get("/api/games/explore", headers=h)
    g1 = next(g for g in r1.json()["games"] if g["game_id"] == gid)
    r2 = await client.get("/api/games/explore", headers=h)
    g2 = next(g for g in r2.json()["games"] if g["game_id"] == gid)
    assert g1["cover_url"] == g2["cover_url"]
    assert g1["cover_set_at"] == g2["cover_set_at"]
    assert g1["content_modified_at"] == g2["content_modified_at"]
    # cover_url 与 cover_set_at 一致性：有 set_at 必有 url
    if g1["cover_set_at"] is not None:
        assert g1["cover_url"] == f"/api/games/{gid}/cover"


@pytest.mark.asyncio
async def test_overwrite_does_not_double_zip(
    client: AsyncClient, alice_with_game: dict,
):
    """连续上传两次 cover → zip 不会出现 cover.png 重复条目（make_archive 会清重打）。"""
    gid = alice_with_game["game_id"]
    h = auth_header(alice_with_game["alice"]["access_token"])
    for _ in range(3):
        await client.post(
            f"/api/games/{gid}/cover", headers=h,
            files=_upload_files(_make_image_bytes()),
        )
    from robot.games import service as game_service
    zip_path = game_service.GAMES_DIR / f"{gid}.zip"
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    # cover.png 只能出现 1 次
    assert names.count("cover.png") == 1
