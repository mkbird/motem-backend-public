"""Progress bridge for the bot.

The game tools push create/modify progress (and game_ready / game_patch) via
``ctx._ws_manager.send_to_user(owner, user, payload)``. The iOS app receives
these over its WebSocket; the bot has no socket, so by default they're skipped.

This bridge is shaped like ws_manager (same ``send_to_user`` signature) but
routes each event to a per-user async sink registered by the Discord handler —
so the bot can surface live progress (like the app) by editing a status
message, instead of just a typing indicator. The existing tool code is
unchanged: passing this object as ws_manager just makes the ``if
ctx._ws_manager:`` paths fire.

Routing is by ``user_id`` (the Discord user id), and each user has at most one
in-flight request (serialized by the per-user lock), so a plain dict is safe
under concurrency.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

ProgressSink = Callable[[dict], Awaitable[None]]


class BotProgressBridge:
    def __init__(self) -> None:
        self._sinks: dict[str, ProgressSink] = {}

    def register(self, user_id: str, sink: ProgressSink) -> None:
        self._sinks[str(user_id)] = sink

    def unregister(self, user_id: str) -> None:
        self._sinks.pop(str(user_id), None)

    async def send_to_user(self, owner_id: str, user_id: str, payload: dict) -> None:
        """ws_manager-compatible: route the event to the user's sink, if any."""
        sink = self._sinks.get(str(user_id))
        if sink is None:
            return
        try:
            await sink(payload or {})
        except Exception as e:  # noqa: BLE001 - progress must never break a turn
            logger.debug("[bot] progress sink error: %s", e)
