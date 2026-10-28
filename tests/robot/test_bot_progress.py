"""Tests for robot/bot/progress.py (ws_manager-shaped progress bridge)."""

from __future__ import annotations

import asyncio

from robot.bot.progress import BotProgressBridge


def _payload(action="game_progress", message="正在生成"):
    return {"type": "robot_command", "data": {"action": action, "message": message}}


def test_routes_to_registered_sink():
    bridge = BotProgressBridge()
    seen = []

    async def sink(payload):
        seen.append(payload)

    bridge.register("u1", sink)
    asyncio.run(bridge.send_to_user("owner", "u1", _payload()))
    assert len(seen) == 1 and seen[0]["data"]["message"] == "正在生成"


def test_no_sink_is_noop():
    bridge = BotProgressBridge()
    # no registration → must not raise
    asyncio.run(bridge.send_to_user("owner", "nobody", _payload()))


def test_routing_is_per_user():
    bridge = BotProgressBridge()
    a, b = [], []
    bridge.register("ua", lambda p: _append(a, p))
    bridge.register("ub", lambda p: _append(b, p))
    asyncio.run(bridge.send_to_user("o", "ua", _payload(message="A")))
    assert [p["data"]["message"] for p in a] == ["A"]
    assert b == []


def test_unregister_stops_routing():
    bridge = BotProgressBridge()
    seen = []
    bridge.register("u1", lambda p: _append(seen, p))
    bridge.unregister("u1")
    asyncio.run(bridge.send_to_user("o", "u1", _payload()))
    assert seen == []


def test_sink_error_is_swallowed():
    bridge = BotProgressBridge()

    async def boom(payload):
        raise RuntimeError("sink broke")

    bridge.register("u1", boom)
    # must not raise — progress can never break a turn
    asyncio.run(bridge.send_to_user("o", "u1", _payload()))


async def _append(lst, payload):
    lst.append(payload)
