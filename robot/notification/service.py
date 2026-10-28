from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import NotificationCategory, NotificationRecord
from robot.i18n import get_locale, t


async def create_notification(
    db: AsyncSession,
    owner_id: str,
    target_user_id: str,
    category: NotificationCategory,
    title: str,
    body: str,
    params_json: dict | None = None,
) -> NotificationRecord:
    record = NotificationRecord(
        owner_id=owner_id,
        target_user_id=target_user_id,
        category=category,
        title=title,
        body=body,
        params_json=params_json,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


def render_notification(record: NotificationRecord, lang: str | None = None) -> dict:
    """Render a stored notification record into the API response shape."""
    locale = lang or get_locale()
    params = dict(record.params_json or {})
    if record.body == "TASK_DEADLINE_BODY" and isinstance(params.get("status"), str):
        params["status"] = t(locale, params["status"])
    return {
        "id": record.id,
        "owner_id": record.owner_id,
        "target_user_id": record.target_user_id,
        "category": record.category,
        "title": t(locale, record.title, **params),
        "body": t(locale, record.body, **params),
        "read": record.read,
        "sent_at": record.sent_at,
        "read_at": record.read_at,
    }


async def get_notifications(
    db: AsyncSession,
    user_id: str,
    unread_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    stmt = (
        select(NotificationRecord)
        .where(NotificationRecord.target_user_id == user_id)
        .order_by(NotificationRecord.sent_at.desc())
        .limit(limit)
    )
    if unread_only:
        stmt = stmt.where(NotificationRecord.read == False)
    result = await db.execute(stmt)
    return [render_notification(record) for record in result.scalars().all()]


async def mark_read(db: AsyncSession, notification_id: str) -> bool:
    result = await db.execute(
        select(NotificationRecord).where(NotificationRecord.id == notification_id)
    )
    record = result.scalar_one_or_none()
    if not record:
        return False
    record.read = True
    record.read_at = datetime.utcnow()
    await db.commit()
    return True


async def mark_all_read(db: AsyncSession, user_id: str) -> int:
    result = await db.execute(
        update(NotificationRecord)
        .where(
            NotificationRecord.target_user_id == user_id,
            NotificationRecord.read == False,
        )
        .values(read=True, read_at=datetime.utcnow())
    )
    await db.commit()
    return result.rowcount


async def get_unread_count(db: AsyncSession, user_id: str) -> int:
    result = await db.execute(
        select(NotificationRecord)
        .where(
            NotificationRecord.target_user_id == user_id,
            NotificationRecord.read == False,
        )
    )
    return len(result.scalars().all())
