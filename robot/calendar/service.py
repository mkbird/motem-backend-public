from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from robot.db.models import (
    CalendarEvent,
    EventParticipant,
    EventParticipantStatus,
)


async def create_event(
    db: AsyncSession,
    owner_id: str,
    user_id: str,
    title: str,
    start_time: datetime,
    end_time: datetime,
    description: str | None = None,
    all_day: bool = False,
    location: str | None = None,
    recurrence_rule: str | None = None,
    reminder_minutes: int = 15,
    color: str | None = None,
    participant_user_ids: list[str] | None = None,
    requires_confirmation: bool = False,
) -> CalendarEvent:
    event = CalendarEvent(
        owner_id=owner_id,
        created_by=user_id,
        title=title,
        start_time=start_time,
        end_time=end_time,
        description=description,
        all_day=all_day,
        location=location,
        recurrence_rule=recurrence_rule,
        reminder_minutes=reminder_minutes,
        color=color,
    )
    db.add(event)
    await db.flush()

    # Add creator as accepted participant
    db.add(
        EventParticipant(
            event_id=event.id,
            user_id=user_id,
            status=EventParticipantStatus.accepted,
        )
    )

    # Add other participants
    default_status = (
        EventParticipantStatus.pending
        if requires_confirmation
        else EventParticipantStatus.accepted
    )
    for uid in participant_user_ids or []:
        if uid != user_id:
            db.add(
                EventParticipant(
                    event_id=event.id, user_id=uid, status=default_status
                )
            )

    await db.commit()
    return await get_event(db, event.id)


async def get_event(db: AsyncSession, event_id: str) -> CalendarEvent | None:
    result = await db.execute(
        select(CalendarEvent)
        .where(CalendarEvent.id == event_id)
        .options(selectinload(CalendarEvent.participants))
    )
    return result.scalar_one_or_none()


async def list_events(
    db: AsyncSession,
    owner_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[CalendarEvent]:
    stmt = (
        select(CalendarEvent)
        .where(CalendarEvent.owner_id == owner_id)
        .options(selectinload(CalendarEvent.participants))
        .order_by(CalendarEvent.start_time)
    )
    if start:
        stmt = stmt.where(CalendarEvent.end_time >= start)
    if end:
        stmt = stmt.where(CalendarEvent.start_time <= end)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def update_event(
    db: AsyncSession, event_id: str, **kwargs
) -> CalendarEvent | None:
    event = await get_event(db, event_id)
    if not event:
        return None
    for key, value in kwargs.items():
        if value is not None and hasattr(event, key):
            setattr(event, key, value)
    await db.commit()
    return await get_event(db, event_id)


async def delete_event(db: AsyncSession, event_id: str) -> bool:
    event = await get_event(db, event_id)
    if not event:
        return False
    await db.delete(event)
    await db.commit()
    return True


async def respond_to_event(
    db: AsyncSession, event_id: str, user_id: str, status: EventParticipantStatus
) -> EventParticipant | None:
    result = await db.execute(
        select(EventParticipant).where(
            and_(
                EventParticipant.event_id == event_id,
                EventParticipant.user_id == user_id,
            )
        )
    )
    participant = result.scalar_one_or_none()
    if not participant:
        return None
    participant.status = status
    await db.commit()
    return participant


async def check_availability(
    db: AsyncSession,
    owner_id: str,
    user_id: str,
    start_time: datetime,
    end_time: datetime,
) -> list[CalendarEvent]:
    """Return conflicting events for a user in a time range."""
    stmt = (
        select(CalendarEvent)
        .join(EventParticipant)
        .where(
            and_(
                CalendarEvent.owner_id == owner_id,
                EventParticipant.user_id == user_id,
                EventParticipant.status != EventParticipantStatus.declined,
                CalendarEvent.start_time < end_time,
                CalendarEvent.end_time > start_time,
            )
        )
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
