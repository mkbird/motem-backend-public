from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user
from robot.calendar.models import (
    CreateEventRequest,
    EventResponse,
    RespondEventRequest,
    UpdateEventRequest,
)
from robot.calendar.service import (
    create_event,
    delete_event,
    get_event,
    list_events,
    respond_to_event,
    update_event,
)
from robot.db.database import get_db
from robot.db.models import User
from robot.i18n import _

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


@router.get("/events", response_model=list[EventResponse])
async def get_events(
    owner_id: str = Query(...),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_events(db, owner_id, start, end)


@router.post("/events", response_model=EventResponse, status_code=201)
async def create(
    owner_id: str = Query(...),
    req: CreateEventRequest = ...,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await create_event(
        db,
        owner_id=owner_id,
        user_id=user.id,
        title=req.title,
        start_time=req.start_time,
        end_time=req.end_time,
        description=req.description,
        all_day=req.all_day,
        location=req.location,
        recurrence_rule=req.recurrence_rule,
        reminder_minutes=req.reminder_minutes,
        color=req.color,
        participant_user_ids=req.participant_user_ids,
        requires_confirmation=req.requires_confirmation,
    )


@router.get("/events/{event_id}", response_model=EventResponse)
async def get_one(
    event_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event = await get_event(db, event_id)
    if not event:
        raise HTTPException(status_code=404, detail=_("EVENT_NOT_FOUND"))
    return event


@router.put("/events/{event_id}", response_model=EventResponse)
async def update(
    event_id: str,
    req: UpdateEventRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event = await update_event(db, event_id, **req.model_dump(exclude_none=True))
    if not event:
        raise HTTPException(status_code=404, detail=_("EVENT_NOT_FOUND"))
    return event


@router.delete("/events/{event_id}", status_code=204)
async def delete(
    event_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not await delete_event(db, event_id):
        raise HTTPException(status_code=404, detail=_("EVENT_NOT_FOUND"))


@router.post("/events/{event_id}/respond")
async def respond(
    event_id: str,
    req: RespondEventRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    participant = await respond_to_event(db, event_id, user.id, req.status)
    if not participant:
        raise HTTPException(status_code=404, detail=_("NOT_EVENT_PARTICIPANT"))
    return {"message": _("RESPONSE_RECORDED"), "status": participant.status}
