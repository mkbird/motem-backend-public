from datetime import datetime, timezone

from pydantic import BaseModel, field_serializer, field_validator

from robot.db.models import EventParticipantStatus


def _to_utc_naive(dt: datetime) -> datetime:
    """Convert any datetime to UTC naive (for DB storage)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


class CreateEventRequest(BaseModel):
    title: str
    start_time: datetime
    end_time: datetime

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def normalize_to_utc(cls, v: datetime) -> datetime:
        return _to_utc_naive(v)
    description: str | None = None
    all_day: bool = False
    location: str | None = None
    recurrence_rule: str | None = None
    reminder_minutes: int = 15
    color: str | None = None
    participant_user_ids: list[str] = []
    requires_confirmation: bool = False


class UpdateEventRequest(BaseModel):
    title: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def normalize_to_utc(cls, v: datetime | None) -> datetime | None:
        return _to_utc_naive(v) if v else v
    description: str | None = None
    all_day: bool | None = None
    location: str | None = None
    recurrence_rule: str | None = None
    reminder_minutes: int | None = None
    color: str | None = None


class EventParticipantResponse(BaseModel):
    user_id: str
    status: EventParticipantStatus

    model_config = {"from_attributes": True}


class EventResponse(BaseModel):
    id: str
    owner_id: str
    created_by: str
    title: str
    description: str | None
    start_time: datetime
    end_time: datetime
    all_day: bool
    location: str | None
    recurrence_rule: str | None
    reminder_minutes: int
    color: str | None
    participants: list[EventParticipantResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("start_time", "end_time", "created_at", "updated_at")
    def serialize_dt(self, dt: datetime, _info) -> str:
        """Naive datetime → UTC ISO8601 (no fractional seconds for iOS compatibility)."""
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.replace(microsecond=0).isoformat()


class RespondEventRequest(BaseModel):
    status: EventParticipantStatus
