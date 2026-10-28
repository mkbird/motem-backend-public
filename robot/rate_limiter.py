"""Shared rate limiter instances used by HTTP routers and WebSocket handlers."""
from slowapi import Limiter
from slowapi.util import get_remote_address

# HTTP rate limiter (IP-based, via slowapi)
http_limiter = Limiter(key_func=get_remote_address)


async def check_ws_create_limit(user_id: str, action: str, limit: int, window: int) -> bool:
    """Redis-based per-user rate limit for WebSocket create actions.

    Returns True if the request is allowed, False if rate-limited.
    Falls back to allow on Redis error to avoid blocking legitimate users.
    """
    try:
        from robot.db.redis import redis_client
        key = f"ratelimit:{action}:{user_id}"
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, window)
        return int(count) <= limit
    except Exception:
        return True  # Redis unavailable → degrade gracefully
