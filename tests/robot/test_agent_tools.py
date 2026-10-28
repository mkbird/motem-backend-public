"""Tests for AI Agent tools — verify every tool executes correctly with real DB.

These tests caught the bug where LLM was passing usernames instead of UUIDs
for owner_id/user_id. Now tools use ContextVar so these values are always correct.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.agent.tools import (
    add_calendar_event,
    add_task,
    check_member_availability,
    control_device,
    get_tasks,
    get_events,
    get_weather,
    set_dependencies,
    set_request_context,
)
from robot.agent.tools._context import (
    _current_owner_id,
    _current_member_role,
    _current_user_id,
)
from robot.db.models import (
    CalendarEvent,
    DeviceRegistry,
    Task,
    User,
    gen_uuid,
)
from tests.conftest import test_session_factory


# ── Fixtures ──


@pytest.fixture
def ws_manager():
    """Mock WebSocket manager."""
    mgr = MagicMock()
    mgr.broadcast_to_group = AsyncMock()
    mgr.send_to_user = AsyncMock()
    mgr.is_user_online = MagicMock(return_value=False)
    return mgr


@pytest.fixture
async def user_ctx(db_session: AsyncSession):
    """Create a user, return context dict."""
    user = User(id=gen_uuid(), email="user@test.com", display_name="张三", auth_type="email")
    db_session.add(user)
    await db_session.commit()

    return {
        "owner_id": user.id,
        "user_id": user.id,
        "display_name": "张三",
    }


@pytest.fixture(autouse=True)
def setup_tools(db_session: AsyncSession, ws_manager):
    """Inject test DB and mock WS into tools before each test."""
    set_dependencies(test_session_factory, ws_manager)
    yield


def _set_ctx(user_ctx, role="admin"):
    """Shorthand to set request context."""
    set_request_context(user_ctx["owner_id"], user_ctx["user_id"], role, user_lang="zh-Hans")


# ── Calendar Tools ──


class TestGetTodayEvents:
    @pytest.mark.asyncio
    async def test_no_events(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        result = await get_events.ainvoke({})
        assert "没有日程" in result

    @pytest.mark.asyncio
    async def test_with_events(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        now = datetime.utcnow()
        event = CalendarEvent(
            owner_id=user_ctx["owner_id"],
            created_by=user_ctx["user_id"],
            title="开会",
            start_time=now,
            end_time=now + timedelta(hours=1),
        )
        db_session.add(event)
        await db_session.commit()

        result = await get_events.ainvoke({})
        assert "开会" in result


class TestAddCalendarEvent:
    @pytest.mark.asyncio
    async def test_create_event_success(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        start = tomorrow.strftime("%Y-%m-%d %H:%M")
        end = (tomorrow + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")

        result = await add_calendar_event.ainvoke({
            "title": "买菜",
            "start_time": start,
            "end_time": end,
        })
        assert "已创建日历事件" in result
        assert "买菜" in result

        rows = await db_session.execute(select(CalendarEvent))
        events = rows.scalars().all()
        assert len(events) == 1
        assert events[0].title == "买菜"
        assert events[0].created_by == user_ctx["user_id"]
        assert events[0].owner_id == user_ctx["owner_id"]

    @pytest.mark.asyncio
    async def test_create_event_bad_time_format(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        result = await add_calendar_event.ainvoke({
            "title": "坏时间",
            "start_time": "not-a-date",
            "end_time": "also-bad",
        })
        assert "格式" in result

    @pytest.mark.asyncio
    async def test_create_event_broadcasts_ws(self, db_session, user_ctx, ws_manager):
        _set_ctx(user_ctx)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        start = tomorrow.strftime("%Y-%m-%d %H:%M")
        end = (tomorrow + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")

        await add_calendar_event.ainvoke({
            "title": "通知测试",
            "start_time": start,
            "end_time": end,
        })
        ws_manager.broadcast_to_group.assert_called_once()
        call_args = ws_manager.broadcast_to_group.call_args
        assert call_args[0][0] == user_ctx["owner_id"]
        assert call_args[0][1]["type"] == "calendar_sync"


class TestCheckMemberAvailability:
    @pytest.mark.asyncio
    async def test_user_free(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        result = await check_member_availability.ainvoke({
            "start_time": tomorrow.strftime("%Y-%m-%d %H:%M"),
            "end_time": (tomorrow + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
        })
        assert "空闲" in result


# ── Task Tools ──


class TestAddTask:
    @pytest.mark.asyncio
    async def test_create_task_success(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        result = await add_task.ainvoke({"title": "去购物"})
        assert "已创建任务" in result
        assert "去购物" in result

        rows = await db_session.execute(select(Task))
        tasks = rows.scalars().all()
        assert len(tasks) == 1
        assert tasks[0].title == "去购物"
        assert tasks[0].created_by == user_ctx["user_id"]
        assert tasks[0].owner_id == user_ctx["owner_id"]

    @pytest.mark.asyncio
    async def test_create_task_with_due_date(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        result = await add_task.ainvoke({
            "title": "交报告",
            "due_date": "2026-03-25",
            "priority": 5,
        })
        assert "已创建任务" in result

        rows = await db_session.execute(select(Task))
        task = rows.scalars().first()
        assert task.priority == 5
        assert task.due_date is not None

    @pytest.mark.asyncio
    async def test_create_task_broadcasts_ws(self, db_session, user_ctx, ws_manager):
        _set_ctx(user_ctx)
        await add_task.ainvoke({"title": "WS测试"})
        ws_manager.broadcast_to_group.assert_called_once()
        call_args = ws_manager.broadcast_to_group.call_args
        assert call_args[0][1]["type"] == "task_sync"


class TestGetTasks:
    @pytest.mark.asyncio
    async def test_no_tasks(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        result = await get_tasks.ainvoke({})
        assert "没有待办" in result

    @pytest.mark.asyncio
    async def test_list_tasks(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        task = Task(
            owner_id=user_ctx["owner_id"],
            created_by=user_ctx["user_id"],
            title="测试任务",
            priority=3,
        )
        db_session.add(task)
        await db_session.commit()

        result = await get_tasks.ainvoke({})
        assert "测试任务" in result


# ── Device Control Tool ──


class TestControlDevice:
    @pytest.mark.asyncio
    async def test_device_not_found(self, db_session, user_ctx):
        _set_ctx(user_ctx)
        result = await control_device.ainvoke({
            "device_name": "不存在的灯",
            "command": "turn_on",
        })
        assert "没有找到" in result

    @pytest.mark.asyncio
    async def test_control_device_success(self, db_session, user_ctx, ws_manager):
        _set_ctx(user_ctx)
        device = DeviceRegistry(
            owner_id=user_ctx["owner_id"],
            homekit_id="hk-light-001",
            name="客厅灯",
            device_type="light",
        )
        db_session.add(device)
        await db_session.commit()

        result = await control_device.ainvoke({
            "device_name": "客厅灯",
            "command": "turn_on",
        })
        assert "客厅灯" in result
        assert "turn_on" in result
        ws_manager.send_to_user.assert_called_once()

    @pytest.mark.asyncio
    async def test_child_cannot_control_restricted(self, db_session, user_ctx):
        device = DeviceRegistry(
            owner_id=user_ctx["owner_id"],
            homekit_id="hk-lock-001",
            name="门锁",
            device_type="lock",
            restricted=True,
        )
        db_session.add(device)
        await db_session.commit()

        set_request_context(user_ctx["owner_id"], user_ctx["user_id"], "child", user_lang="zh-Hans")
        result = await control_device.ainvoke({
            "device_name": "门锁",
            "command": "unlock",
        })
        assert "受限" in result


# ── Weather Tool ──


class TestGetWeather:
    @pytest.mark.asyncio
    async def test_get_weather(self, db_session, user_ctx):
        """Weather tool should return result (may fail if no API key, but should not crash)."""
        _set_ctx(user_ctx)
        result = await get_weather.ainvoke({})
        assert isinstance(result, str)
        assert len(result) > 0


# ── Context Isolation ──


class TestContextIsolation:
    @pytest.mark.asyncio
    async def test_context_vars_set_correctly(self, db_session, user_ctx):
        """Verify ContextVars are set and tools read the right values."""
        set_request_context(user_ctx["owner_id"], user_ctx["user_id"], "admin")
        assert _current_owner_id.get() == user_ctx["owner_id"]
        assert _current_user_id.get() == user_ctx["user_id"]
        assert _current_member_role.get() == "admin"

    @pytest.mark.asyncio
    async def test_tool_uses_context_not_llm_param(self, db_session, user_ctx):
        """The critical test: tool should use ContextVar, not any LLM-provided ID."""
        _set_ctx(user_ctx)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        start = tomorrow.strftime("%Y-%m-%d %H:%M")
        end = (tomorrow + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")

        await add_calendar_event.ainvoke({
            "title": "上下文测试",
            "start_time": start,
            "end_time": end,
        })

        rows = await db_session.execute(select(CalendarEvent))
        event = rows.scalars().first()
        assert event is not None
        assert event.created_by == user_ctx["user_id"]
        assert event.owner_id == user_ctx["owner_id"]

        await add_task.ainvoke({"title": "上下文任务"})
        rows = await db_session.execute(select(Task))
        task = rows.scalars().first()
        assert task is not None
        assert task.created_by == user_ctx["user_id"]
        assert task.owner_id == user_ctx["owner_id"]
