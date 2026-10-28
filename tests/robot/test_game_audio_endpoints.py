"""Tests for the new audio direct-replace + upload endpoints + canonical
assets pre-creation.

Coverage:
- POST /api/games/{id}/audio/swap     (主库 → 游戏 audio/<old> 字节覆盖)
- POST /api/games/{id}/audio/upload   (multipart 用户音频文件 → 游戏 audio/)
- _ensure_canonical_assets            (background.png 预置)
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from robot.agent.tools.game_tools import (
    _ensure_canonical_assets,
    AUDIO_LIBRARY_DIR,
)
from robot.db.models import AIGame, ExploreGameView, UserGameView
from tests.conftest import auth_header, register_and_login


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


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


@pytest_asyncio.fixture
async def alice_with_game(db_session: AsyncSession, client: AsyncClient):
    from robot.games import service as game_service

    alice = await register_and_login(client, email="alice_audio_endpoints@example.com")
    game_id = "ai_test_audio_endpoints_alice"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=alice["user"]["id"],
        title="Alice Audio EP", title_i18n=json.dumps({"en": "x", "zh-Hans": "x"}),
        description="x", description_i18n=json.dumps({"en": "x", "zh-Hans": "x"}),
        icon="🎮", input_mode="touch", kind="ai", visibility="private",
        original_game_id=None, template_from="", session_id=None,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    db_session.add(UserGameView(
        user_id=alice["user"]["id"], game_id=game_id,
        view_type="created", added_at=now,
    ))
    await db_session.commit()

    src = game_service.GAMES_DIR / game_id
    src.mkdir(parents=True, exist_ok=True)
    (src / "audio").mkdir(exist_ok=True)
    # 预置一个旧的 BGM 文件，swap 测试要替换它
    (src / "audio" / "score_combo.ogg").write_bytes(b"OLDBYTES")

    yield {"alice": alice, "game_id": game_id, "game_dir": src}

    if src.is_dir():
        shutil.rmtree(src, ignore_errors=True)
    z = game_service.GAMES_DIR / f"{game_id}.zip"
    if z.exists():
        z.unlink()


@pytest_asyncio.fixture
async def bob_user(client: AsyncClient):
    return await register_and_login(client, email="bob_audio_endpoints@example.com")


# ─────────────────────────────────────────────────────────────────────
# audio/swap — main library → game audio/<old_filename>
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audio_swap_owner_succeeds_and_overwrites_bytes(
    client: AsyncClient, alice_with_game,
):
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    # Pick a BGM that we know exists in main library
    new_track = "bgm_8bit_chill"
    src = AUDIO_LIBRARY_DIR / "library" / "bgm" / f"{new_track}.ogg"
    if not src.exists():
        pytest.skip(f"missing master library file {src}")
    expected_bytes = src.read_bytes()

    resp = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": new_track},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    # 游戏 audio/score_combo.ogg 字节应该等于主库 bgm_8bit_chill.ogg
    written = (alice_with_game["game_dir"] / "audio" / "score_combo.ogg").read_bytes()
    assert written == expected_bytes
    # 主库本身字节不变
    assert src.read_bytes() == expected_bytes


@pytest.mark.asyncio
async def test_audio_swap_non_owner_returns_403(
    client: AsyncClient, alice_with_game, bob_user,
):
    gid = alice_with_game["game_id"]
    resp = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": "bgm_8bit_chill"},
        headers=auth_header(bob_user["access_token"]),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_audio_swap_path_traversal_blocked(
    client: AsyncClient, alice_with_game,
):
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "../../../etc/passwd", "new_track_id": "bgm_8bit_chill"},
        headers=headers,
    )
    assert resp.status_code == 400
    resp = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": "../etc/passwd"},
        headers=headers,
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_audio_swap_unknown_track_id_404(
    client: AsyncClient, alice_with_game,
):
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    resp = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": "definitely_not_in_library"},
        headers=headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_audio_swap_repacks_zip(
    client: AsyncClient, alice_with_game,
):
    from robot.games import service as game_service
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    track = "bgm_8bit_chill"
    if not (AUDIO_LIBRARY_DIR / "library" / "bgm" / f"{track}.ogg").exists():
        pytest.skip("library file missing")
    resp = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": track},
        headers=headers,
    )
    assert resp.status_code == 200
    z = game_service.GAMES_DIR / f"{gid}.zip"
    assert z.exists()
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        assert any(n.endswith("audio/score_combo.ogg") for n in names)


@pytest.mark.asyncio
async def test_audio_swap_none_sentinel_removes_file(
    client: AsyncClient, alice_with_game,
):
    """iOS `BGMSelection.none = "_none"` 哨兵：删除游戏 audio/<old> 文件。
    skeleton audio_manager._load_stream 找不到文件 silently no-op，运行时不崩。
    """
    from robot.games import service as game_service
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    target = alice_with_game["game_dir"] / "audio" / "score_combo.ogg"
    assert target.exists(), "fixture should pre-place an audio file"

    resp = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": "_none"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["new_track_id"] == "_none"
    assert not target.exists(), "audio file should be removed"

    # zip 也应重打，且不包含该 audio 文件
    z = game_service.GAMES_DIR / f"{gid}.zip"
    assert z.exists()
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        assert not any(n.endswith("audio/score_combo.ogg") for n in names)


@pytest.mark.asyncio
async def test_audio_swap_none_sentinel_idempotent(
    client: AsyncClient, alice_with_game,
):
    """`_none` 重复调用应 idempotent：文件已不存在时仍返回 200，不抛错。"""
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    # 第一次删除
    r1 = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": "_none"},
        headers=headers,
    )
    assert r1.status_code == 200
    # 第二次同样的调用（文件已经没了）
    r2 = await client.post(
        f"/api/games/{gid}/audio/swap",
        params={"old_filename": "score_combo.ogg", "new_track_id": "_none"},
        headers=headers,
    )
    assert r2.status_code == 200


# ─────────────────────────────────────────────────────────────────────
# audio/upload — multipart user audio → game audio/
# ─────────────────────────────────────────────────────────────────────


def _audio_bytes(payload: bytes = b"OGGS\x00\x00fake_ogg") -> bytes:
    return payload


@pytest.mark.asyncio
async def test_audio_upload_owner_succeeds(
    client: AsyncClient, alice_with_game,
):
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    raw = _audio_bytes(b"OGGS\x00\x00abcd_real_ogg_payload")
    files = {"audio": ("myfile.ogg", raw, "audio/ogg")}
    data = {"filename": "myfile.ogg"}
    resp = await client.post(
        f"/api/games/{gid}/audio/upload",
        headers=headers, files=files, data=data,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["filename"] == "myfile.ogg"
    # 文件落在 game audio/myfile.ogg
    assert (alice_with_game["game_dir"] / "audio" / "myfile.ogg").read_bytes() == raw


@pytest.mark.asyncio
async def test_audio_upload_non_owner_403(
    client: AsyncClient, alice_with_game, bob_user,
):
    gid = alice_with_game["game_id"]
    files = {"audio": ("foo.ogg", b"OGGS", "audio/ogg")}
    resp = await client.post(
        f"/api/games/{gid}/audio/upload",
        headers=auth_header(bob_user["access_token"]),
        files=files, data={"filename": "foo.ogg"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_audio_upload_unknown_format_415(
    client: AsyncClient, alice_with_game,
):
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    files = {"audio": ("evil.exe", b"MZbinary", "audio/wav")}
    resp = await client.post(
        f"/api/games/{gid}/audio/upload",
        headers=headers, files=files, data={"filename": "evil.exe"},
    )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_audio_upload_size_limit_413(
    client: AsyncClient, alice_with_game,
):
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    huge = b"X" * (8 * 1024 * 1024 + 1)
    files = {"audio": ("big.ogg", huge, "audio/ogg")}
    resp = await client.post(
        f"/api/games/{gid}/audio/upload",
        headers=headers, files=files, data={"filename": "big.ogg"},
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_audio_upload_path_traversal_blocked(
    client: AsyncClient, alice_with_game,
):
    gid = alice_with_game["game_id"]
    headers = auth_header(alice_with_game["alice"]["access_token"])
    # 路径穿越走 multipart upload 的 filename 字段（不是 form filename）
    files = {"audio": ("../../etc/passwd.ogg", b"OGGS", "audio/ogg")}
    resp = await client.post(
        f"/api/games/{gid}/audio/upload",
        headers=headers, files=files,
    )
    assert resp.status_code == 400
    # 同样应该拒绝从 form filename 字段过来的恶意 name
    files2 = {"audio": ("foo.ogg", b"OGGS", "audio/ogg")}
    resp2 = await client.post(
        f"/api/games/{gid}/audio/upload",
        headers=headers, files=files2, params={"filename": "../../etc/passwd.ogg"},
    )
    assert resp2.status_code == 400


# ─────────────────────────────────────────────────────────────────────
# _ensure_canonical_assets
# ─────────────────────────────────────────────────────────────────────


class TestCanonicalAssets:
    def test_creates_background_png(self, tmp_path: Path):
        _ensure_canonical_assets(tmp_path)
        bg = tmp_path / "assets" / "background.png"
        assert bg.exists()
        assert bg.stat().st_size > 0

    def test_idempotent_does_not_overwrite_existing(self, tmp_path: Path):
        # User 已经上传了一张图 — host 不应覆盖
        (tmp_path / "assets").mkdir(parents=True, exist_ok=True)
        original = b"USER_UPLOADED_PNG_FAKE_BYTES"
        (tmp_path / "assets" / "background.png").write_bytes(original)
        _ensure_canonical_assets(tmp_path)
        assert (tmp_path / "assets" / "background.png").read_bytes() == original
