"""Tests for DELETE /api/auth/me — hard-delete current account + cascading data."""

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import (
    CalendarEvent,
    GameLike,
    NotificationCategory,
    NotificationRecord,
    Task,
    User,
    UserDevice,
)
from tests.conftest import auth_header, register_and_login


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _create_fake_ai_game(owner_id: str, base_dir: Path) -> str:
    """在临时 games 目录里造一个 AI 游戏文件（dir + meta.json + zip 占位）。
    返回 game_id。"""
    game_id = f"ai_test_{uuid.uuid4().hex[:8]}"
    gd = base_dir / game_id
    gd.mkdir(parents=True, exist_ok=True)
    (gd / "meta.json").write_text(
        json.dumps({"game_id": game_id, "title": "T", "owner_id": owner_id}),
        encoding="utf-8",
    )
    zip_path = base_dir / f"{game_id}.zip"
    zip_path.write_bytes(b"fake-zip")
    return game_id


@pytest.fixture
def temp_games_dir(monkeypatch, tmp_path: Path) -> Path:
    """把 service 模块里的 _GAMES_DIR 重定向到临时目录，避免污染真实 games/。"""
    games_dir = tmp_path / "games"
    games_dir.mkdir()
    monkeypatch.setattr("robot.auth.service._GAMES_DIR", games_dir)
    return games_dir


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_me_returns_204(client: AsyncClient, temp_games_dir: Path):
    auth = await register_and_login(client, email="quitter@example.com")
    resp = await client.delete("/api/auth/me", headers=auth_header(auth["access_token"]))
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_me_unauthenticated(client: AsyncClient, temp_games_dir: Path):
    resp = await client.delete("/api/auth/me")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_token_invalid_after_delete(client: AsyncClient, temp_games_dir: Path):
    auth = await register_and_login(client, email="ghost@example.com")
    headers = auth_header(auth["access_token"])

    me_before = await client.get("/api/auth/me", headers=headers)
    assert me_before.status_code == 200

    await client.delete("/api/auth/me", headers=headers)

    me_after = await client.get("/api/auth/me", headers=headers)
    assert me_after.status_code == 401


@pytest.mark.asyncio
async def test_delete_cleans_user_devices_and_likes(
    client: AsyncClient,
    db_session: AsyncSession,
    temp_games_dir: Path,
):
    auth = await register_and_login(client, email="lk@example.com", device_id="dev-lk")
    user_id = auth["user"]["id"]
    headers = auth_header(auth["access_token"])

    # 直接插一条 GameLike（绕过 /api/games router，避免 games_dir 路径耦合）
    db_session.add(GameLike(user_id=user_id, game_id="ai_some_other"))
    await db_session.commit()

    # 删账号
    await client.delete("/api/auth/me", headers=headers)

    # 验证 DB 中没有该用户的 UserDevice / GameLike
    devices = (await db_session.execute(
        select(UserDevice).where(UserDevice.user_id == user_id)
    )).scalars().all()
    assert devices == []

    likes = (await db_session.execute(
        select(GameLike).where(GameLike.user_id == user_id)
    )).scalars().all()
    assert likes == []

    # 用户行也应该没了
    user = (await db_session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    assert user is None


@pytest.mark.asyncio
async def test_delete_cleans_calendar_tasks_notifications(
    client: AsyncClient,
    db_session: AsyncSession,
    temp_games_dir: Path,
):
    auth = await register_and_login(client, email="hasdata@example.com")
    user_id = auth["user"]["id"]
    headers = auth_header(auth["access_token"])

    # 直接往 DB 里塞数据（避免依赖业务路由）
    db_session.add(CalendarEvent(
        owner_id=user_id, created_by=user_id, title="meeting",
        start_time=datetime.utcnow(), end_time=datetime.utcnow(),
    ))
    db_session.add(Task(
        owner_id=user_id, created_by=user_id, title="todo",
    ))
    db_session.add(NotificationRecord(
        owner_id=user_id, target_user_id=user_id,
        category=NotificationCategory.system, title="hi", body="b",
    ))
    await db_session.commit()

    # 删账号
    resp = await client.delete("/api/auth/me", headers=headers)
    assert resp.status_code == 204

    events = (await db_session.execute(
        select(CalendarEvent).where(CalendarEvent.owner_id == user_id)
    )).scalars().all()
    tasks = (await db_session.execute(
        select(Task).where(Task.owner_id == user_id)
    )).scalars().all()
    notifs = (await db_session.execute(
        select(NotificationRecord).where(NotificationRecord.owner_id == user_id)
    )).scalars().all()
    assert events == []
    assert tasks == []
    assert notifs == []


@pytest.mark.asyncio
async def test_delete_user_ai_games_only(
    client: AsyncClient,
    temp_games_dir: Path,
):
    auth_a = await register_and_login(client, email="a@example.com", device_id="dev-a")
    auth_b = await register_and_login(client, email="b@example.com", device_id="dev-b")
    user_a = auth_a["user"]["id"]
    user_b = auth_b["user"]["id"]

    # A 拥有 2 个游戏，B 拥有 1 个
    a1 = _create_fake_ai_game(owner_id=user_a, base_dir=temp_games_dir)
    a2 = _create_fake_ai_game(owner_id=user_a, base_dir=temp_games_dir)
    b1 = _create_fake_ai_game(owner_id=user_b, base_dir=temp_games_dir)

    # A 删账号
    resp = await client.delete(
        "/api/auth/me", headers=auth_header(auth_a["access_token"])
    )
    assert resp.status_code == 204

    # A 的游戏目录 + zip 都删了
    assert not (temp_games_dir / a1).exists()
    assert not (temp_games_dir / f"{a1}.zip").exists()
    assert not (temp_games_dir / a2).exists()
    assert not (temp_games_dir / f"{a2}.zip").exists()

    # B 的不受影响
    assert (temp_games_dir / b1).is_dir()
    assert (temp_games_dir / f"{b1}.zip").exists()


@pytest.mark.asyncio
async def test_delete_does_not_touch_other_users(
    client: AsyncClient,
    db_session: AsyncSession,
    temp_games_dir: Path,
):
    auth_a = await register_and_login(client, email="aa@example.com", device_id="dev-aa")
    auth_b = await register_and_login(client, email="bb@example.com", device_id="dev-bb")
    user_b = auth_b["user"]["id"]

    # B 自有一些数据
    db_session.add(Task(owner_id=user_b, created_by=user_b, title="b's todo"))
    await db_session.commit()

    # A 删账号
    await client.delete("/api/auth/me", headers=auth_header(auth_a["access_token"]))

    # B 的数据不受影响
    b_user = (await db_session.execute(
        select(User).where(User.id == user_b)
    )).scalar_one_or_none()
    assert b_user is not None

    b_tasks = (await db_session.execute(
        select(Task).where(Task.owner_id == user_b)
    )).scalars().all()
    assert len(b_tasks) >= 1

    # B 仍能访问 /me
    me = await client.get("/api/auth/me", headers=auth_header(auth_b["access_token"]))
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_re_register_after_delete(client: AsyncClient, temp_games_dir: Path):
    """删账号后用同邮箱重新注册应该成功（旧用户行已经没了）。"""
    email = "comeback@example.com"
    auth = await register_and_login(client, email=email)
    await client.delete("/api/auth/me", headers=auth_header(auth["access_token"]))

    # 重新注册同邮箱 — 200 / 201（被认为是新账号）
    resp = await client.post(
        "/api/auth/register",
        json={"email": email, "password": "newpass", "display_name": "Reborn"},
    )
    assert resp.status_code == 201
