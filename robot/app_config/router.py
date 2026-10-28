"""Public client-config endpoints (anonymous, no auth).

Currently exposes the social_links table to iOS clients so they can
populate the Settings → Follow Us section dynamically. Operations can
INSERT / UPDATE / disable rows in `social_links` without an App Store
release.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.config import settings
from robot.db.database import get_db
from robot.db.models import SocialLink

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/social-links")
async def list_social_links(db: AsyncSession = Depends(get_db)):
    """匿名公开 — 按 enabled + region 过滤、sort_order 排序返回链接数组。"""
    rows = (
        await db.execute(
            select(SocialLink)
            .where(SocialLink.enabled.is_(True))
            .where(or_(SocialLink.region == "all", SocialLink.region == settings.region))
            .order_by(SocialLink.sort_order)
        )
    ).scalars().all()
    return {
        "links": [
            {
                "platform": r.platform,
                "label_key": r.label_key,
                "icon_name": r.icon_name,
                "url": r.url,
            }
            for r in rows
        ]
    }
