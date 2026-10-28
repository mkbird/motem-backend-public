"""Tests for /api/widget-data router (per-user widget data store)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from robot.widgets.data_router import WIDGET_DATA_CHANNELS, _read_channel, _upsert_channel


class TestUpsertChannel:
    @pytest.mark.asyncio
    async def test_executes_atomic_upsert(self):
        # MySQL atomic INSERT...ON DUPLICATE KEY UPDATE → 一条 SQL，
        # 不再有 SELECT-then-INSERT race condition。验证 db.execute 被调
        # 一次且 commit 跟上。
        from sqlalchemy.ext.asyncio import AsyncSession
        db = AsyncMock(spec=AsyncSession)
        await _upsert_channel(db, "uuid-1", "weather", {"temp": 20})
        db.execute.assert_awaited_once()
        db.commit.assert_awaited_once()
        # add() 不应再被调用 — 整条变 INSERT...ON DUPLICATE KEY 了
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_channel_rejected(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        from fastapi import HTTPException
        db = AsyncMock(spec=AsyncSession)
        with pytest.raises(HTTPException) as exc:
            await _upsert_channel(db, "uuid-1", "evil", {})
        assert exc.value.status_code == 400
        assert "unknown channel" in exc.value.detail
        db.execute.assert_not_called()  # 不该走到 SQL


class TestReadChannel:
    @pytest.mark.asyncio
    async def test_returns_decoded_payload(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        from robot.db.models import UserWidgetData

        db = AsyncMock(spec=AsyncSession)
        row = UserWidgetData(user_id="u", channel="weather", payload='{"temp": 25}')
        result = AsyncMock()
        result.scalar_one_or_none = lambda: row
        db.execute = AsyncMock(return_value=result)

        out = await _read_channel(db, "u", "weather")
        assert out == {"temp": 25}

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        db = AsyncMock(spec=AsyncSession)
        result = AsyncMock()
        result.scalar_one_or_none = lambda: None
        db.execute = AsyncMock(return_value=result)
        assert await _read_channel(db, "u", "weather") is None

    @pytest.mark.asyncio
    async def test_corrupt_payload_returns_none(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        from robot.db.models import UserWidgetData
        db = AsyncMock(spec=AsyncSession)
        row = UserWidgetData(user_id="u", channel="weather", payload="not json")
        result = AsyncMock()
        result.scalar_one_or_none = lambda: row
        db.execute = AsyncMock(return_value=result)
        assert await _read_channel(db, "u", "weather") is None


class TestChannelWhitelist:
    def test_known_channels(self):
        assert "weather" in WIDGET_DATA_CHANNELS
        assert "todos" in WIDGET_DATA_CHANNELS
        assert "pomodoro" in WIDGET_DATA_CHANNELS

    def test_no_unbounded_growth(self):
        # 安全检查：不允许 widget 通过 channel 名走私任意 key
        # 添加新 channel 必须改 WIDGET_DATA_CHANNELS 白名单 + 这条 assertion 一起
        assert WIDGET_DATA_CHANNELS == {"weather", "todos", "pomodoro"}, \
            f"channel whitelist changed; update test if intentional: {WIDGET_DATA_CHANNELS}"
