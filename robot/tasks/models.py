from datetime import datetime, timezone

from pydantic import BaseModel, field_serializer, field_validator

from robot.db.models import TaskStatus


def _to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


class CreateTaskRequest(BaseModel):
    title: str
    description: str | None = None
    assigned_to: str | None = None
    due_date: datetime | None = None
    priority: int = 3  # 1-5

    @field_validator("due_date", mode="after")
    @classmethod
    def normalize_to_utc(cls, v: datetime | None) -> datetime | None:
        return _to_utc_naive(v) if v else v


class UpdateTaskRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    assigned_to: str | None = None
    due_date: datetime | None = None
    priority: int | None = None
    status: TaskStatus | None = None

    @field_validator("due_date", mode="after")
    @classmethod
    def normalize_to_utc(cls, v: datetime | None) -> datetime | None:
        return _to_utc_naive(v) if v else v


class TaskResponse(BaseModel):
    id: str
    owner_id: str
    created_by: str
    assigned_to: str | None
    title: str
    description: str | None
    due_date: datetime | None
    priority: int
    status: TaskStatus
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("due_date", "completed_at", "created_at", "updated_at")
    def serialize_dt(self, dt: datetime | None, _info) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.replace(microsecond=0).isoformat()
