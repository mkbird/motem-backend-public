from datetime import datetime, timezone

from pydantic import BaseModel, field_serializer

from robot.db.models import NotificationCategory


class NotificationResponse(BaseModel):
    id: str
    owner_id: str
    target_user_id: str
    category: NotificationCategory
    title: str
    body: str
    read: bool
    sent_at: datetime
    read_at: datetime | None

    model_config = {"from_attributes": True}

    @field_serializer("sent_at", "read_at")
    def serialize_dt(self, dt: datetime | None, _info) -> str | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.replace(microsecond=0).isoformat()


class CreateNotificationRequest(BaseModel):
    owner_id: str
    target_user_id: str
    category: NotificationCategory
    title: str
    body: str
