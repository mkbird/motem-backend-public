"""Creation gating for the bot (module ⑤).

Public creation runs the expensive generation agent, so before any create /
modify we enforce, in order:

  1. **Kill-switch** — ``settings.bot_creation_enabled`` (flip to False to halt
     all creation instantly when cost runs away).
  2. **Content guard** — cheap prompt screening (empty / over-length / a small
     starter denylist). This is a first-pass guard, not a full moderation
     system; swap ``screen_prompt`` for a real moderation API before scaling.
  3. **Per-requester daily quota** — Redis counter keyed on the chat-platform
     user (so each Discord user gets their own budget even though they share
     one backend identity). Reuses the existing ``check_ws_create_limit``.

Playing is never gated (public). Gating applies only to create / modify.
"""

from __future__ import annotations

from dataclasses import dataclass

from robot.config import settings
from robot.rate_limiter import check_ws_create_limit

_DAY_SECONDS = 86_400
_MAX_PROMPT_LEN = 2_000

# Starter denylist (lowercase substrings). Deliberately small — real
# moderation should replace this. Kept generic to avoid false positives.
_BANNED_TERMS: tuple[str, ...] = (
    "child porn", "cp ", "csam", "rape", "bestiality",
    "儿童色情", "强奸",
)


@dataclass
class GateResult:
    allowed: bool
    reason: str = ""  # machine key when blocked: disabled|empty|too_long|content_blocked|quota_exceeded


def screen_prompt(text: str) -> GateResult:
    """Cheap content guard. Returns allowed, or a blocked reason key."""
    t = (text or "").strip()
    if not t:
        return GateResult(False, "empty")
    if len(t) > _MAX_PROMPT_LEN:
        return GateResult(False, "too_long")
    low = t.lower()
    if any(term in low for term in _BANNED_TERMS):
        return GateResult(False, "content_blocked")
    return GateResult(True)


async def check_quota(requester_key: str, action: str) -> GateResult:
    """Per-requester daily quota via Redis. action: 'create' | 'modify'."""
    if action == "modify":
        limit, act = settings.bot_daily_modify_limit, "bot_game_modify"
    else:
        limit, act = settings.bot_daily_create_limit, "bot_game_create"
    allowed = await check_ws_create_limit(requester_key, act, limit, _DAY_SECONDS)
    return GateResult(allowed, "" if allowed else "quota_exceeded")


async def gate(text: str, *, requester_key: str, action: str) -> GateResult:
    """Full gate: kill-switch → content guard → quota. First failure wins."""
    if not settings.bot_creation_enabled:
        return GateResult(False, "disabled")
    screened = screen_prompt(text)
    if not screened.allowed:
        return screened
    return await check_quota(requester_key, action)
