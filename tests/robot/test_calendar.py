"""Tests for Calendar module: CRUD, participants, respond."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, register_and_login


async def _setup_user(client: AsyncClient) -> dict:
    """Helper: register user, return {token, owner_id, user}."""
    info = await register_and_login(client, email="cal@test.com", display_name="张三")
    return {
        "token": info["access_token"],
        "owner_id": info["user"]["id"],
        "user": info["user"],
    }


class TestCreateEvent:
    @pytest.mark.asyncio
    async def test_create_event(self, client: AsyncClient):
        ctx = await _setup_user(client)
        now = datetime.utcnow()
        resp = await client.post(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            json={
                "title": "家长会",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
                "description": "小明的家长会",
                "location": "学校",
                "reminder_minutes": 30,
            },
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "家长会"
        assert data["location"] == "学校"
        assert data["reminder_minutes"] == 30
        assert data["owner_id"] == ctx["owner_id"]
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_event_with_participants(self, client: AsyncClient):
        ctx = await _setup_user(client)
        # Add a second member
        mom = await register_and_login(
            client, email="mom_cal@test.com", display_name="李梅", device_id="d2"
        )
        now = datetime.utcnow()
        resp = await client.post(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            json={
                "title": "聚餐",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=2)).isoformat(),
                "participant_user_ids": [mom["user"]["id"]],
                "requires_confirmation": True,
            },
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 201
        data = resp.json()
        # Creator is auto-added as participant (accepted) + mom (pending)
        assert len(data["participants"]) == 2
        participant_ids = {p["user_id"] for p in data["participants"]}
        assert mom["user"]["id"] in participant_ids
        mom_p = next(p for p in data["participants"] if p["user_id"] == mom["user"]["id"])
        assert mom_p["status"] == "pending"


class TestListEvents:
    @pytest.mark.asyncio
    async def test_list_events(self, client: AsyncClient):
        ctx = await _setup_user(client)
        now = datetime.utcnow()
        # Create 2 events
        for title in ["事件A", "事件B"]:
            await client.post(
                f"/api/calendar/events?owner_id={ctx['owner_id']}",
                json={
                    "title": title,
                    "start_time": now.isoformat(),
                    "end_time": (now + timedelta(hours=1)).isoformat(),
                },
                headers=auth_header(ctx["token"]),
            )
        resp = await client.get(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestGetUpdateDeleteEvent:
    @pytest.mark.asyncio
    async def test_get_event(self, client: AsyncClient):
        ctx = await _setup_user(client)
        now = datetime.utcnow()
        create_resp = await client.post(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            json={
                "title": "测试事件",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
            headers=auth_header(ctx["token"]),
        )
        event_id = create_resp.json()["id"]

        resp = await client.get(
            f"/api/calendar/events/{event_id}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "测试事件"

    @pytest.mark.asyncio
    async def test_get_event_not_found(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.get(
            "/api/calendar/events/nonexistent",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_event(self, client: AsyncClient):
        ctx = await _setup_user(client)
        now = datetime.utcnow()
        create_resp = await client.post(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            json={
                "title": "旧标题",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
            headers=auth_header(ctx["token"]),
        )
        event_id = create_resp.json()["id"]

        resp = await client.put(
            f"/api/calendar/events/{event_id}",
            json={"title": "新标题", "location": "新地点"},
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "新标题"
        assert resp.json()["location"] == "新地点"

    @pytest.mark.asyncio
    async def test_delete_event(self, client: AsyncClient):
        ctx = await _setup_user(client)
        now = datetime.utcnow()
        create_resp = await client.post(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            json={
                "title": "待删除",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
            headers=auth_header(ctx["token"]),
        )
        event_id = create_resp.json()["id"]

        resp = await client.delete(
            f"/api/calendar/events/{event_id}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 204

        # Verify deleted
        resp = await client.get(
            f"/api/calendar/events/{event_id}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 404


class TestRespondToEvent:
    @pytest.mark.asyncio
    async def test_respond_accept(self, client: AsyncClient):
        ctx = await _setup_user(client)
        mom = await register_and_login(
            client, email="mom_resp@test.com", display_name="李梅", device_id="d3"
        )
        now = datetime.utcnow()
        create_resp = await client.post(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            json={
                "title": "需要确认的事件",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
                "participant_user_ids": [mom["user"]["id"]],
                "requires_confirmation": True,
            },
            headers=auth_header(ctx["token"]),
        )
        event_id = create_resp.json()["id"]

        resp = await client.post(
            f"/api/calendar/events/{event_id}/respond",
            json={"status": "accepted"},
            headers=auth_header(mom["access_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_respond_not_participant(self, client: AsyncClient):
        ctx = await _setup_user(client)
        # Create a second user who is NOT a participant
        other = await register_and_login(
            client, email="other_resp@test.com", display_name="路人", device_id="d-other"
        )
        now = datetime.utcnow()
        create_resp = await client.post(
            f"/api/calendar/events?owner_id={ctx['owner_id']}",
            json={
                "title": "无参与者",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
            headers=auth_header(ctx["token"]),
        )
        event_id = create_resp.json()["id"]

        # Other user is not a participant of this event
        resp = await client.post(
            f"/api/calendar/events/{event_id}/respond",
            json={"status": "accepted"},
            headers=auth_header(other["access_token"]),
        )
        assert resp.status_code == 404
