from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user
from robot.db.database import get_db
from robot.db.models import User
from robot.devices.models import (
    DeviceCommandRequest,
    DeviceRegistryResponse,
    SyncDeviceRequest,
)
from robot.devices.service import get_device, get_devices, sync_device
from robot.i18n import _
from robot.websocket import ws_manager

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("", response_model=list[DeviceRegistryResponse])
async def list_devices(
    owner_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_devices(db, owner_id)


@router.post("/sync", response_model=DeviceRegistryResponse)
async def sync(
    owner_id: str = Query(...),
    req: SyncDeviceRequest = ...,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await sync_device(
        db,
        owner_id=owner_id,
        homekit_id=req.homekit_id,
        name=req.name,
        device_type=req.device_type,
        room=req.room,
        restricted=req.restricted,
    )


@router.post("/{device_id}/command")
async def send_command(
    device_id: str,
    owner_id: str = Query(...),
    req: DeviceCommandRequest = ...,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    device = await get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail=_("DEVICE_NOT_FOUND_HTTP"))

    # Forward command to user's iOS device via WebSocket for HomeKit execution
    await ws_manager.send_to_user(
        owner_id,
        user.id,
        {
            "type": "device_command",
            "data": {
                "homekit_id": device.homekit_id,
                "device_name": device.name,
                "command": req.command,
                "value": req.value,
            },
        },
    )
    return {"message": _("COMMAND_SENT", name=device.name, cmd=req.command)}
