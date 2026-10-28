from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user
from robot.db.database import get_db
from robot.db.models import User
from robot.i18n import _
from robot.notification.models import NotificationResponse
from robot.notification.service import (
    get_notifications,
    get_unread_count,
    mark_all_read,
    mark_read,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    unread: bool = Query(False),
    limit: int = Query(50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_notifications(db, user.id, unread_only=unread, limit=limit)


@router.get("/unread-count")
async def unread_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await get_unread_count(db, user.id)
    return {"count": count}


@router.patch("/{notification_id}/read")
async def read_one(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await mark_read(db, notification_id):
        raise HTTPException(status_code=404, detail=_("NOTIFICATION_NOT_FOUND"))
    return {"message": _("MARKED_AS_READ")}


@router.post("/read-all")
async def read_all(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = await mark_all_read(db, user.id)
    return {"message": _("MARKED_NOTIFICATIONS_AS_READ", count=count)}
