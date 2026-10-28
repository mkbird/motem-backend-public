"""Tests for Tasks module: CRUD, complete, filter."""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, register_and_login


async def _setup_user(client: AsyncClient) -> dict:
    info = await register_and_login(client, email="task@test.com", display_name="张三")
    return {
        "token": info["access_token"],
        "owner_id": info["user"]["id"],
        "user": info["user"],
    }


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_create_task(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={
                "title": "买牛奶",
                "description": "全脂牛奶",
                "priority": 2,
            },
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "买牛奶"
        assert data["priority"] == 2
        assert data["status"] == "pending"
        assert data["owner_id"] == ctx["owner_id"]

    @pytest.mark.asyncio
    async def test_create_task_with_assignment(self, client: AsyncClient):
        ctx = await _setup_user(client)
        son = await register_and_login(
            client, email="son_task@test.com", display_name="小明", device_id="d2"
        )
        due = (datetime.utcnow() + timedelta(days=1)).isoformat()
        resp = await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={
                "title": "做作业",
                "assigned_to": son["user"]["id"],
                "due_date": due,
                "priority": 1,
            },
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 201
        assert resp.json()["assigned_to"] == son["user"]["id"]


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_tasks(self, client: AsyncClient):
        ctx = await _setup_user(client)
        for title in ["任务A", "任务B", "任务C"]:
            await client.post(
                f"/api/tasks?owner_id={ctx['owner_id']}",
                json={"title": title},
                headers=auth_header(ctx["token"]),
            )
        resp = await client.get(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    @pytest.mark.asyncio
    async def test_list_tasks_filter_status(self, client: AsyncClient):
        ctx = await _setup_user(client)
        create_resp = await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={"title": "已完成任务"},
            headers=auth_header(ctx["token"]),
        )
        task_id = create_resp.json()["id"]
        await client.patch(
            f"/api/tasks/{task_id}/complete",
            headers=auth_header(ctx["token"]),
        )
        await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={"title": "未完成任务"},
            headers=auth_header(ctx["token"]),
        )

        resp = await client.get(
            f"/api/tasks?owner_id={ctx['owner_id']}&status=pending",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "未完成任务"


class TestGetUpdateDeleteTask:
    @pytest.mark.asyncio
    async def test_get_task(self, client: AsyncClient):
        ctx = await _setup_user(client)
        create_resp = await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={"title": "获取测试"},
            headers=auth_header(ctx["token"]),
        )
        task_id = create_resp.json()["id"]

        resp = await client.get(
            f"/api/tasks/{task_id}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "获取测试"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.get(
            "/api/tasks/nonexistent",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_task(self, client: AsyncClient):
        ctx = await _setup_user(client)
        create_resp = await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={"title": "旧标题", "priority": 3},
            headers=auth_header(ctx["token"]),
        )
        task_id = create_resp.json()["id"]

        resp = await client.put(
            f"/api/tasks/{task_id}",
            json={"title": "新标题", "priority": 1},
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "新标题"
        assert resp.json()["priority"] == 1

    @pytest.mark.asyncio
    async def test_delete_task(self, client: AsyncClient):
        ctx = await _setup_user(client)
        create_resp = await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={"title": "待删除"},
            headers=auth_header(ctx["token"]),
        )
        task_id = create_resp.json()["id"]

        resp = await client.delete(
            f"/api/tasks/{task_id}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 204

        resp = await client.get(
            f"/api/tasks/{task_id}",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 404


class TestCompleteTask:
    @pytest.mark.asyncio
    async def test_complete_task(self, client: AsyncClient):
        ctx = await _setup_user(client)
        create_resp = await client.post(
            f"/api/tasks?owner_id={ctx['owner_id']}",
            json={"title": "待完成"},
            headers=auth_header(ctx["token"]),
        )
        task_id = create_resp.json()["id"]

        resp = await client.patch(
            f"/api/tasks/{task_id}/complete",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_complete_nonexistent_task(self, client: AsyncClient):
        ctx = await _setup_user(client)
        resp = await client.patch(
            "/api/tasks/nonexistent/complete",
            headers=auth_header(ctx["token"]),
        )
        assert resp.status_code == 404
