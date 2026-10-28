"""APScheduler-based reminder and notification scheduler.

Runs periodic jobs to:
1. Check for upcoming calendar events and send reminders
2. Check for overdue tasks and send notifications
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from robot.db.models import (
    CalendarEvent,
    EventParticipant,
    EventParticipantStatus,
    NotificationCategory,
    Task,
    TaskStatus,
    User,
)
from robot.i18n import t
from robot.notification.push_service import send_push
from robot.notification.service import create_notification
from aichat.websocket.manager import ConnectionManager

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _user_locale(db: AsyncSession, user_id: str) -> str:
    result = await db.execute(select(User.locale).where(User.id == user_id))
    return result.scalar_one_or_none() or "en"


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


def start_scheduler(db_session_factory: async_sessionmaker, ws_manager: ConnectionManager):
    """Start the notification scheduler with periodic jobs."""
    scheduler = get_scheduler()

    async def check_calendar_reminders():
        await _check_calendar_reminders(db_session_factory, ws_manager)

    async def check_task_deadlines():
        await _check_task_deadlines(db_session_factory, ws_manager)

    # Check every minute for calendar reminders
    scheduler.add_job(check_calendar_reminders, "interval", minutes=1, id="calendar_reminders")
    # Check every 5 minutes for task deadlines
    scheduler.add_job(check_task_deadlines, "interval", minutes=5, id="task_deadlines")

    scheduler.start()
    logger.info("Notification scheduler started")


async def _check_calendar_reminders(
    db_session_factory: async_sessionmaker, ws_manager: ConnectionManager
):
    """Find events whose reminder time is now and send notifications."""
    now = datetime.utcnow()

    async with db_session_factory() as db:
        # Find events starting within the next 60 minutes
        window_end = now + timedelta(minutes=60)
        result = await db.execute(
            select(CalendarEvent)
            .where(
                and_(
                    CalendarEvent.start_time > now,
                    CalendarEvent.start_time <= window_end,
                )
            )
            .options(selectinload(CalendarEvent.participants))
        )
        events = result.scalars().all()

        for event in events:
            reminder_time = event.start_time - timedelta(minutes=event.reminder_minutes)
            # Check if we're within 1 minute of the reminder time
            if abs((now - reminder_time).total_seconds()) > 60:
                continue

            for participant in event.participants:
                if participant.status == EventParticipantStatus.declined:
                    continue

                params = {
                    "title": event.title,
                    "minutes": event.reminder_minutes,
                    "time": event.start_time.strftime("%H:%M"),
                    "event_id": event.id,
                }
                title_key = "CALENDAR_REMINDER"
                body_key = "CALENDAR_REMINDER_BODY"

                # Save notification record
                await create_notification(
                    db,
                    owner_id=event.owner_id,
                    target_user_id=participant.user_id,
                    category=NotificationCategory.reminder,
                    title=title_key,
                    body=body_key,
                    params_json=params,
                )
                locale = await _user_locale(db, participant.user_id)
                title = t(locale, title_key, **params)
                body = t(locale, body_key, **params)

                # APNs banner 始终发 —— 不管用户当前是否 WS 在线。
                # 在线时 iOS 端 willPresent 让前台也弹横幅；不在线时走系统通知中心。
                # 之前的 if online: WS else: push 分支会让 app 前台时系统层完全
                # 静默，用户体验是"我开着 app 却收不到提醒"。
                await send_push(
                    db,
                    participant.user_id,
                    title=title,
                    body=body,
                    category="calendar_reminder",
                    data={"event_id": event.id},
                )
                # WebSocket 通知仍然发：让 in-app UI（通知中心红点 / 列表刷新）
                # 实时更新，不依赖 push payload 解析。
                if ws_manager.is_user_online(event.owner_id, participant.user_id):
                    await ws_manager.send_to_user(
                        event.owner_id,
                        participant.user_id,
                        {
                            "type": "notification",
                            "data": {
                                "category": "calendar_reminder",
                                "title": title,
                                "body": body,
                                "event_id": event.id,
                            },
                        },
                    )

        logger.debug("Calendar reminder check done, processed %d events", len(events))


async def _check_task_deadlines(
    db_session_factory: async_sessionmaker, ws_manager: ConnectionManager
):
    """Find tasks that are due soon or overdue and notify assignees."""
    now = datetime.utcnow()
    soon = now + timedelta(hours=1)

    async with db_session_factory() as db:
        result = await db.execute(
            select(Task).where(
                and_(
                    Task.status != TaskStatus.completed,
                    Task.due_date.isnot(None),
                    Task.due_date <= soon,
                    Task.assigned_to.isnot(None),
                )
            )
        )
        tasks = result.scalars().all()

        for task in tasks:
            is_overdue = task.due_date < now
            title_key = "TASK_OVERDUE" if is_overdue else "TASK_DUE_SOON"
            status_key = "TASK_OVERDUE_STATUS" if is_overdue else "TASK_DUE_SOON_STATUS"
            locale = await _user_locale(db, task.assigned_to)
            params = {
                "title": task.title,
                "status": t(locale, status_key),
                "task_id": task.id,
            }
            body_key = "TASK_DEADLINE_BODY"

            await create_notification(
                db,
                owner_id=task.owner_id,
                target_user_id=task.assigned_to,
                category=NotificationCategory.task,
                title=title_key,
                body=body_key,
                params_json={
                    "title": task.title,
                    "status": status_key,
                    "task_id": task.id,
                },
            )
            title = t(locale, title_key, **params)
            body = t(locale, body_key, **params)

            # 同 calendar 路径：APNs 始终发，WS 在线时附加给 in-app UI 用。
            await send_push(
                db,
                task.assigned_to,
                title=title,
                body=body,
                category="task_due",
                data={"task_id": task.id},
            )
            if ws_manager.is_user_online(task.owner_id, task.assigned_to):
                await ws_manager.send_to_user(
                    task.owner_id,
                    task.assigned_to,
                    {
                        "type": "notification",
                        "data": {
                            "category": "task_due",
                            "title": title,
                            "body": body,
                            "task_id": task.id,
                        },
                    },
                )

        logger.debug("Task deadline check done, processed %d tasks", len(tasks))
