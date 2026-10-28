"""Monkey-patch claude_code_sdk so unknown SSE event types don't kill the stream.

OpenRouter (and other proxies) sometimes inject events the SDK's parser
doesn't recognize. Empirically observed:

- `rate_limit_event` from OpenRouter when proxying qwen / glm / deepseek

The stock SDK raises `MessageParseError` and the `query()` async iterator
dies on the first occurrence, taking the entire generation with it (AI's
work so far is lost; widget_dir is still on `_template`).

This patch wraps `parse_message`: if the original raises with "Unknown
message type", we return a `SystemMessage(subtype="unknown_<type>")`
instead, so the consumer's `async for message in query(...)` keeps
flowing and AI work is not interrupted. Everything else still raises
(corrupted shape, missing fields, etc.).

Idempotent — `apply()` is a no-op after the first call.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCHED = False


def apply() -> None:
    """Install the unknown-message-type tolerance patch. Safe to call N times."""
    global _PATCHED
    if _PATCHED:
        return

    try:
        from claude_code_sdk._internal import message_parser as _mp
        from claude_code_sdk.types import SystemMessage
        from claude_code_sdk._errors import MessageParseError
    except ImportError as e:
        logger.warning("[SDKPatch] claude_code_sdk not installed; skipping patch: %s", e)
        _PATCHED = True
        return

    original = _mp.parse_message

    def tolerant_parse(data):
        try:
            return original(data)
        except MessageParseError as exc:
            msg = str(exc)
            # Only swallow "Unknown message type: ..." — keep raising on
            # genuine schema corruption (missing required fields, wrong
            # types, etc.) so real bugs still surface.
            if msg.startswith("Unknown message type:"):
                unknown_type = ""
                if isinstance(data, dict):
                    unknown_type = str(data.get("type") or "unknown")
                logger.info("[SDKPatch] swallowing unknown event type=%s (kept stream alive)",
                            unknown_type)
                return SystemMessage(
                    subtype=f"unknown_{unknown_type}",
                    data=data if isinstance(data, dict) else {},
                )
            raise

    _mp.parse_message = tolerant_parse

    # Also patch the symbol that client.py imported into its own namespace
    # at module load time — `from .message_parser import parse_message`
    # creates a separate binding that won't see our patch otherwise.
    try:
        from claude_code_sdk._internal import client as _client
        if hasattr(_client, "parse_message"):
            _client.parse_message = tolerant_parse
    except ImportError:
        pass

    _PATCHED = True
    logger.info("[SDKPatch] installed claude_code_sdk parse_message tolerance patch")


# Auto-apply on import so callers don't need to remember.
apply()
