"""Agent tools — re-export all tools and context management functions."""

from aichat.tools.vision_tool import vision_tool
from robot.agent.tools._context import set_dependencies, set_request_context
from robot.agent.tools.calendar_tools import (
    add_calendar_event,
    check_member_availability,
    get_events,
)
from robot.agent.tools.camera_tools import take_photo
from robot.agent.tools.device_tools import control_device
from robot.agent.tools.game_tools import create_game, modify_game
from robot.agent.tools.task_tools import add_task, get_tasks
from robot.agent.tools.weather_tools import get_weather, real_weather_tool
from robot.agent.tools.web_search_tools import web_search_tool
from robot.agent.tools.widget_tools import create_widget, modify_widget

all_tools = [
    get_events,
    add_calendar_event,
    check_member_availability,
    add_task,
    get_tasks,
    get_weather,
    real_weather_tool,
    web_search_tool,
    control_device,
    take_photo,
    vision_tool,
    create_game,
    modify_game,
    create_widget,
    modify_widget,
]

__all__ = [
    "all_tools",
    "set_dependencies",
    "set_request_context",
]
