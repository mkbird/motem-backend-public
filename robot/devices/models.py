from datetime import datetime

from pydantic import BaseModel


class DeviceRegistryResponse(BaseModel):
    id: str
    owner_id: str
    homekit_id: str
    name: str
    device_type: str
    room: str | None
    restricted: bool
    last_seen: datetime

    model_config = {"from_attributes": True}


class SyncDeviceRequest(BaseModel):
    homekit_id: str
    name: str
    device_type: str
    room: str | None = None
    restricted: bool = False


class DeviceCommandRequest(BaseModel):
    command: str  # turn_on, turn_off, set_brightness, etc.
    value: str | None = None
