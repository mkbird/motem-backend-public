"""Camera tool — take_photo: 通过 WebSocket 向 iOS 客户端发送拍照指令。"""

import logging

from langchain_core.tools import tool

from robot.agent.tools import _context as ctx
from robot.i18n import t as _t

logger = logging.getLogger(__name__)


@tool
async def take_photo() -> str:
    """拍照：让设备前置摄像头倒计时3秒后拍一张照片并保存到相册。
    当用户说"帮我拍照"、"拍一张"、"照相"等表达拍照意图时调用此工具。
    """
    owner_id = ctx._current_owner_id.get()
    user_id = ctx._current_user_id.get()
    lang = ctx.get_user_lang()
    logger.info("[Tool] take_photo: owner=%s user=%s", owner_id, user_id)

    if not ctx._ws_manager:
        return _t(lang, "camera_no_device")

    await ctx._ws_manager.send_to_user(
        owner_id,
        user_id,
        {
            "type": "robot_command",
            "data": {
                "action": "take_photo",
            },
        },
    )
    return ""
