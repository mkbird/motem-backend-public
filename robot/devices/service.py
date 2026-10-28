from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import DeviceRegistry


async def sync_device(
    db: AsyncSession,
    owner_id: str,
    homekit_id: str,
    name: str,
    device_type: str,
    room: str | None = None,
    restricted: bool = False,
) -> DeviceRegistry:
    """Sync a device from HomeKit — create or update."""
    result = await db.execute(
        select(DeviceRegistry).where(
            DeviceRegistry.owner_id == owner_id,
            DeviceRegistry.homekit_id == homekit_id,
        )
    )
    device = result.scalar_one_or_none()
    if device:
        device.name = name
        device.device_type = device_type
        device.room = room
        device.restricted = restricted
        device.last_seen = datetime.utcnow()
    else:
        device = DeviceRegistry(
            owner_id=owner_id,
            homekit_id=homekit_id,
            name=name,
            device_type=device_type,
            room=room,
            restricted=restricted,
        )
        db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


async def get_devices(db: AsyncSession, owner_id: str) -> list[DeviceRegistry]:
    result = await db.execute(
        select(DeviceRegistry).where(DeviceRegistry.owner_id == owner_id)
    )
    return list(result.scalars().all())


async def get_device(db: AsyncSession, device_id: str) -> DeviceRegistry | None:
    result = await db.execute(
        select(DeviceRegistry).where(DeviceRegistry.id == device_id)
    )
    return result.scalar_one_or_none()
