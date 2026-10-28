"""Tests for GET /api/config/social-links — public client config."""

import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import SocialLink


async def _seed(db: AsyncSession, **overrides) -> SocialLink:
    data = {
        "id": str(uuid.uuid4()),
        "platform": overrides.get("platform", "test"),
        "label_key": overrides.get("label_key", "Label"),
        "icon_name": overrides.get("icon_name", "globe"),
        "url": overrides.get("url"),
        "sort_order": overrides.get("sort_order", 0),
        "enabled": overrides.get("enabled", True),
        "region": overrides.get("region", "all"),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    row = SocialLink(**data)
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_list_returns_empty_when_no_rows(client: AsyncClient):
    resp = await client.get("/api/config/social-links")
    assert resp.status_code == 200
    assert resp.json() == {"links": []}


@pytest.mark.asyncio
async def test_list_returns_enabled_rows_sorted(
    client: AsyncClient, db_session: AsyncSession
):
    await _seed(db_session, platform="x", label_key="Follow Us on X",
                icon_name="xmark", url="https://x.com/aippy_ai", sort_order=20)
    await _seed(db_session, platform="website", label_key="Official Website",
                icon_name="globe", url=None, sort_order=10)
    await _seed(db_session, platform="discord", label_key="Follow Us on Discord",
                icon_name="bubble.left.and.bubble.right",
                url="https://discord.gg/abcd", sort_order=30)

    resp = await client.get("/api/config/social-links")
    body = resp.json()
    assert resp.status_code == 200

    platforms = [l["platform"] for l in body["links"]]
    assert platforms == ["website", "x", "discord"]

    # url 字段如实返回（None 也保留）
    website_row = body["links"][0]
    assert website_row["url"] is None
    assert website_row["icon_name"] == "globe"
    assert website_row["label_key"] == "Official Website"


@pytest.mark.asyncio
async def test_list_excludes_disabled_rows(
    client: AsyncClient, db_session: AsyncSession
):
    await _seed(db_session, platform="x", url="https://x.com/a", enabled=True, sort_order=10)
    await _seed(db_session, platform="instagram", url=None, enabled=False, sort_order=20)

    resp = await client.get("/api/config/social-links")
    platforms = [l["platform"] for l in resp.json()["links"]]
    assert "x" in platforms
    assert "instagram" not in platforms


@pytest.mark.asyncio
async def test_region_all_always_visible(client: AsyncClient, db_session: AsyncSession):
    await _seed(db_session, platform="x", region="all", sort_order=10)
    resp = await client.get("/api/config/social-links")
    platforms = [l["platform"] for l in resp.json()["links"]]
    assert "x" in platforms


@pytest.mark.asyncio
async def test_region_filter_excludes_other_region(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """region != 'all' 且 != settings.region 的行应该被过滤掉。"""
    # 默认 settings.region = 'cn'（per config.py 默认）
    await _seed(db_session, platform="x", region="all", sort_order=10)
    await _seed(db_session, platform="bilibili", region="cn", sort_order=20)
    await _seed(db_session, platform="tiktok", region="overseas", sort_order=30)

    # 模拟海外环境
    from robot.config import settings as app_settings
    monkeypatch.setattr(app_settings, "region", "overseas")

    resp = await client.get("/api/config/social-links")
    platforms = [l["platform"] for l in resp.json()["links"]]
    assert "x" in platforms              # all 永远展示
    assert "tiktok" in platforms         # overseas 区匹配
    assert "bilibili" not in platforms   # cn 区被过滤
