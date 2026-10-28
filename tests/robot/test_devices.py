"""Tests for Devices module: list, sync, command."""

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, register_and_login


async def _setup_user(client: AsyncClient) -> dict:
    info = await register_and_login(client, email="dev@test.com", display_name="张三")
    return {
        "token": info["access_token"],
        "owner_id": info["user"]["id"],
        "user": info["user"],
    }


class TestSyncDevice:
    @pytest.mark.asyncio
    async def test_sync_device(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.post(
            f"/api/devices/sync?owner_id={ctx['owner_id']}",
            json={
                "homekit_id": "hk-light-001",
                "name": "客厅灯",
                "device_type": "light",
                "room": "客厅",
                "restricted": False,
            },
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "客厅灯"
        assert data["device_type"] == "light"
        assert data["room"] == "客厅"
        assert data["restricted"] is False

    @pytest.mark.asyncio
    async def test_sync_restricted_device(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.post(
            f"/api/devices/sync?owner_id={ctx['owner_id']}",
            json={
                "homekit_id": "hk-lock-001",
                "name": "前门锁",
                "device_type": "lock",
                "restricted": True,
            },
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["restricted"] is True


class TestListDevices:
    @pytest.mark.asyncio
    async def test_list_devices(self, client: AsyncClient):
        ctx = await _setup_user(client)
        for dev in [
            {"homekit_id": "hk-1", "name": "灯", "device_type": "light"},
            {"homekit_id": "hk-2", "name": "空调", "device_type": "thermostat"},
        ]:
            await client.post(
                f"/api/devices/sync?owner_id={ctx['owner_id']}",
                json=dev,
                headers=auth_header(ctx["token"]),
            )

        resp = await client.get(
            f"/api/devices?owner_id={ctx['owner_id']}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_list_empty(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.get(
            f"/api/devices?owner_id={ctx['owner_id']}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestDeviceCommand:
    @pytest.mark.asyncio
    async def test_command_not_found(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.post(
            f"/api/devices/nonexistent/command?owner_id={ctx['owner_id']}",
            json={"command": "turn_on"},
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 404
