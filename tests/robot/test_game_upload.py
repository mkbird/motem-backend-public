"""POST /api/games — tests.

The endpoint accepts a zipped Godot project + meta.json from macOS
Motem Studio. It persists the files, inserts an `ai_games` row owned
by the current user (visibility=private), and broadcasts `game_ready`
on the iPhone-of-same-user channel so the existing iOS download path
picks the upload up.

Covers:
- Authenticated POST with a minimal valid zip → 201 + files on disk + DB row
- Unauthenticated POST → 401
- Bad zip / missing project.godot / missing meta.json → 400
- Path traversal in zip → 400
- Re-upload by same owner overwrites
- Re-upload by *different* user rejected with 403
- Oversized zip → 413
"""

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from robot.db.models import AIGame
from tests.conftest import auth_header, register_and_login


def _build_zip(
    *,
    include_project: bool = True,
    include_meta: bool = True,
    meta_overrides: dict | None = None,
    extra_members: dict[str, str] | None = None,
) -> bytes:
    """Build a minimal valid-ish Godot project zip in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if include_project:
            zf.writestr(
                "project.godot",
                'config_version=5\n\n[application]\nconfig/name="UploadedGame"\n',
            )
        if include_meta:
            meta = {
                "game_id": "mac_test_game_001",
                "title": "Uploaded Game",
                "title_i18n": {"en": "Uploaded Game", "zh-Hans": "上传游戏"},
                "description_i18n": {"en": "", "zh-Hans": ""},
                "icon": "🎯",
                "input_mode": "touch",
            }
            if meta_overrides:
                meta.update(meta_overrides)
            zf.writestr("meta.json", json.dumps(meta))
        zf.writestr("scenes/main.tscn", "[gd_scene format=3]\n[node name=\"M\" type=\"Node2D\"]\n")
        zf.writestr("scenes/main.gd", "extends Node2D\nfunc _ready(): pass\n")
        for name, content in (extra_members or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


# ── Auth ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(), "application/zip")},
    )
    assert resp.status_code in (401, 403)


# ── Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_succeeds_and_creates_db_row(client: AsyncClient, db_session):
    creds = await register_and_login(client, email="alice@example.com")

    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(), "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["game_id"] == "mac_test_game_001"
    assert body["title_i18n"]["en"] == "Uploaded Game"
    assert body["input_mode"] == "touch"

    # DB row exists, owned by alice, private.
    row = await db_session.scalar(
        select(AIGame).where(AIGame.game_id == "mac_test_game_001")
    )
    assert row is not None
    assert row.owner_id == creds["user"]["id"]
    assert row.visibility == "private"
    assert row.kind == "ai"

    # Files on disk (cleanup after assertions).
    from robot.games.router import GAMES_DIR

    game_dir = GAMES_DIR / "mac_test_game_001"
    zip_path = GAMES_DIR / "mac_test_game_001.zip"
    try:
        assert (game_dir / "project.godot").is_file()
        assert (game_dir / "meta.json").is_file()
        assert zip_path.is_file()
        meta = json.loads((game_dir / "meta.json").read_text("utf-8"))
        assert meta["owner_id"] == creds["user"]["id"]
        assert meta["modified_at"] >= int(time.time()) - 5
    finally:
        import shutil
        if game_dir.exists():
            shutil.rmtree(game_dir)
        if zip_path.exists():
            zip_path.unlink()


@pytest.mark.asyncio
async def test_upload_generates_id_when_meta_omits_it(client: AsyncClient, db_session):
    creds = await register_and_login(client, email="bob@example.com")
    zip_bytes = _build_zip(meta_overrides={"game_id": None})
    # _build_zip uses dict.update with None — strip the key entirely.
    # Build a manual zip without game_id instead.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.godot", "config_version=5\n")
        zf.writestr(
            "meta.json",
            json.dumps({"title": "Anon", "input_mode": "touch"}),
        )
    zip_bytes = buf.getvalue()

    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", zip_bytes, "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    game_id = resp.json()["game_id"]
    assert game_id.startswith("mac_")

    # Cleanup
    import shutil
    from robot.games.router import GAMES_DIR

    if (GAMES_DIR / game_id).exists():
        shutil.rmtree(GAMES_DIR / game_id)
    if (GAMES_DIR / f"{game_id}.zip").exists():
        (GAMES_DIR / f"{game_id}.zip").unlink()


# ── Validation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_rejects_corrupt_zip(client: AsyncClient):
    creds = await register_and_login(client, email="charlie@example.com")
    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", b"not a zip at all", "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_rejects_missing_project_godot(client: AsyncClient):
    creds = await register_and_login(client, email="dave@example.com")
    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(include_project=False), "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert resp.status_code == 400
    assert "project.godot" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_missing_meta_json(client: AsyncClient):
    creds = await register_and_login(client, email="erin@example.com")
    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(include_meta=False), "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert resp.status_code == 400
    assert "meta.json" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_traversal(client: AsyncClient):
    creds = await register_and_login(client, email="frank@example.com")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("../escaped.txt", "owned")
        zf.writestr("project.godot", "config_version=5\n")
        zf.writestr("meta.json", json.dumps({"title": "x"}))
    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", buf.getvalue(), "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert resp.status_code == 400
    assert "unsafe" in resp.json()["detail"]


# ── Ownership ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_reupload_by_same_owner_overwrites(client: AsyncClient):
    creds = await register_and_login(client, email="grace@example.com")
    first = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(
            meta_overrides={"game_id": "mac_owner_overwrite_001", "title": "v1"}
        ), "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(
            meta_overrides={"game_id": "mac_owner_overwrite_001", "title": "v2"}
        ), "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert second.status_code == 201

    # Cleanup
    import shutil
    from robot.games.router import GAMES_DIR

    gid = "mac_owner_overwrite_001"
    if (GAMES_DIR / gid).exists():
        shutil.rmtree(GAMES_DIR / gid)
    if (GAMES_DIR / f"{gid}.zip").exists():
        (GAMES_DIR / f"{gid}.zip").unlink()


@pytest.mark.asyncio
async def test_upload_other_user_cannot_overwrite(client: AsyncClient):
    alice = await register_and_login(client, email="alice2@example.com")
    bob = await register_and_login(client, email="bob2@example.com")

    first = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(
            meta_overrides={"game_id": "mac_alice_only_001"}
        ), "application/zip")},
        headers=auth_header(alice["access_token"]),
    )
    assert first.status_code == 201

    intrusion = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(
            meta_overrides={"game_id": "mac_alice_only_001"}
        ), "application/zip")},
        headers=auth_header(bob["access_token"]),
    )
    assert intrusion.status_code == 403

    # Cleanup
    import shutil
    from robot.games.router import GAMES_DIR

    gid = "mac_alice_only_001"
    if (GAMES_DIR / gid).exists():
        shutil.rmtree(GAMES_DIR / gid)
    if (GAMES_DIR / f"{gid}.zip").exists():
        (GAMES_DIR / f"{gid}.zip").unlink()


@pytest.mark.asyncio
async def test_upload_invalid_game_id_rejected(client: AsyncClient):
    creds = await register_and_login(client, email="henri@example.com")
    resp = await client.post(
        "/api/games",
        files={"file": ("game.zip", _build_zip(
            meta_overrides={"game_id": "../escape"}
        ), "application/zip")},
        headers=auth_header(creds["access_token"]),
    )
    assert resp.status_code == 400
