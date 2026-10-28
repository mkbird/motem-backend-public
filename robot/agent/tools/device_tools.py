"""Device control tool — control_device."""

import logging

from langchain_core.tools import tool

from robot.agent.tools import _context as ctx
from robot.i18n import t as _t

logger = logging.getLogger(__name__)


@tool
async def control_device(
    device_name: str,
    command: str,
    value: str = "",
) -> str:
    """控制智能家居设备。
    device_name: 设备名称，如 "客厅灯"
    command: 操作，如 "turn_on", "turn_off", "set_brightness"
    value: 操作值，如 "80"（亮度百分比）
    """
    from sqlalchemy import select

    from robot.db.models import DeviceRegistry

    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    member_role = ctx._current_member_role.get()
    lang = ctx.get_user_lang()
    logger.info("[Tool] control_device: device=%r cmd=%s val=%s owner=%s user=%s role=%s", device_name, command, value, owner_id, user_id, member_role)

    async with ctx._db_session_factory() as db:
        result = await db.execute(
            select(DeviceRegistry).where(
                DeviceRegistry.owner_id == owner_id,
                DeviceRegistry.name == device_name,
            )
        )
        device = result.scalar_one_or_none()
        if not device:
            return _t(lang, "device_not_found", name=device_name)

        # Permission check
        if device.restricted and member_role == "child":
            return _t(lang, "device_restricted", name=device_name)

        # Send command via WebSocket to iOS client for HomeKit execution
        if ctx._ws_manager:
            await ctx._ws_manager.send_to_user(
                owner_id,
                user_id,
                {
                    "type": "device_command",
                    "data": {
                        "homekit_id": device.homekit_id,
                        "device_name": device_name,
                        "command": command,
                        "value": value,
                    },
                },
            )
            cmd_full = f"{command} {value}".strip() if value else command
            return _t(lang, "device_cmd_sent", name=device_name, cmd=cmd_full)
        return _t(lang, "device_no_online")
