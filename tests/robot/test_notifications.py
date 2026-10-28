"""Tests for Notifications module: list, unread count, mark read."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import NotificationCategory, NotificationRecord
from tests.conftest import auth_header, register_and_login


async def _create_notification(
    db: AsyncSession, owner_id: str, user_id: str, title: str, read: bool = False
) -> NotificationRecord:
    """Insert a notification directly via DB for testing."""
    record = NotificationRecord(
        owner_id=owner_id,
        target_user_id=user_id,
        category=NotificationCategory.reminder,
        title=title,
        body=f"Body of {title}",
        read=read,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def _setup(client: AsyncClient, db: AsyncSession) -> dict:
    info = await register_and_login(client, email="notif@test.com", display_name="张三")
    user_id = info["user"]["id"]
    return {
        "token": info["access_token"],
        "owner_id": user_id,
        "user_id": user_id,
    }


class TestListNotifications:
    @pytest.mark.asyncio
    async def test_list_notifications(self, client: AsyncClient, db_session: AsyncSession):
        ctx = await _setup(client, db_session)
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "通知1")
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "通知2")

        resp = await client.get(
            "/api/notifications",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_list_unread_only(self, client: AsyncClient, db_session: AsyncSession):
        ctx = await _setup(client, db_session)
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "未读", read=False)
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "已读", read=True)

        resp = await client.get(
            "/api/notifications?unread=true",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "未读"


class TestUnreadCount:
    @pytest.mark.asyncio
    async def test_unread_count(self, client: AsyncClient, db_session: AsyncSession):
        ctx = await _setup(client, db_session)
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "n1")
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "n2")
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "n3", read=True)

        resp = await client.get(
            "/api/notifications/unread-count",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2


class TestMarkRead:
    @pytest.mark.asyncio
    async def test_mark_read(self, client: AsyncClient, db_session: AsyncSession):
        ctx = await _setup(client, db_session)
        notif = await _create_notification(
            db_session, ctx["owner_id"], ctx["user_id"], "待读通知"
        )

        resp = await client.patch(
            f"/api/notifications/{notif.id}/read",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200

        # Verify unread count is 0
        count_resp = await client.get(
            "/api/notifications/unread-count",
            headers=auth_header(ctx["token"]),
        )
        assert count_resp.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_mark_read_not_found(self, client: AsyncClient, db_session: AsyncSession):
        ctx = await _setup(client, db_session)
        resp = await client.patch(
            "/api/notifications/nonexistent/read",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_mark_all_read(self, client: AsyncClient, db_session: AsyncSession):
        ctx = await _setup(client, db_session)
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "n1")
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "n2")
        await _create_notification(db_session, ctx["owner_id"], ctx["user_id"], "n3")

        resp = await client.post(
            "/api/notifications/read-all",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200

        count_resp = await client.get(
            "/api/notifications/unread-count",
            headers=auth_header(ctx["token"]),
        )
        assert count_resp.json()["count"] == 0
