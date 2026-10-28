"""Cross-user WebSocket requests must be rejected before accepting a connection."""
from unittest.mock import AsyncMock

import pytest

from robot.auth.scope import resolve_websocket_group


@pytest.mark.parametrize("requested", ["", "alice"])
def test_group_is_derived_from_authenticated_user(requested):
    assert resolve_websocket_group("alice", requested) == "alice"


def test_other_users_group_is_not_authorized_by_query_parameter():
    with pytest.raises(PermissionError):
        resolve_websocket_group("alice", "bob")


@pytest.mark.parametrize("identity", [None, "", " ", 1])
def test_missing_or_invalid_identity_is_rejected(identity):
    with pytest.raises(ValueError):
        resolve_websocket_group(identity)


@pytest.mark.asyncio
async def test_cross_user_request_rejected_before_accept_or_database(monkeypatch):
    import robot.main as main

    monkeypatch.setattr(main, "decode_token", lambda _: {"sub": "alice", "type": "access", "exp": 9999999999})
    connect = AsyncMock()
    monkeypatch.setattr(main.ws_manager, "connect", connect)
    websocket = AsyncMock()
    await main.websocket_endpoint(websocket, "unit-test-only", "device", "bob", "")
    websocket.close.assert_awaited_once_with(code=4003, reason="Group access denied")
    connect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["", "alice"])
async def test_allowed_group_is_canonicalized_before_accept(monkeypatch, requested):
    import robot.main as main

    class StopBeforeDatabase(Exception):
        pass

    monkeypatch.setattr(main, "decode_token", lambda _: {"sub": "alice", "type": "access", "exp": 9999999999})
    connect = AsyncMock(side_effect=StopBeforeDatabase)
    monkeypatch.setattr(main.ws_manager, "connect", connect)
    websocket = AsyncMock()
    with pytest.raises(StopBeforeDatabase):
        await main.websocket_endpoint(websocket, "unit-test-only", "device", requested, "")
    connect.assert_awaited_once_with(websocket, "alice", "alice", "device")


@pytest.mark.asyncio
async def test_signed_token_without_subject_is_rejected(monkeypatch):
    import robot.main as main

    monkeypatch.setattr(main, "decode_token", lambda _: {"type": "access", "exp": 9999999999})
    websocket = AsyncMock()
    await main.websocket_endpoint(websocket, "unit-test-only", "device", "", "")
    websocket.close.assert_awaited_once_with(code=4001, reason="Invalid token")
