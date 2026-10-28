"""Tests for Auth module: register, login, refresh, me."""

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, register_and_login


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_success(self, client: AsyncClient):
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": "alice@test.com",
                "password": "pass123",
                "display_name": "Alice",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "alice@test.com"
        assert data["display_name"] == "Alice"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_register_duplicate_email(self, client: AsyncClient):
        await client.post(
            "/api/auth/register",
            json={
                "email": "dup@test.com",
                "password": "pass123",
                "display_name": "User1",
            },
        )
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": "dup@test.com",
                "password": "pass456",
                "display_name": "User2",
            },
        )
        assert resp.status_code == 400


class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success(self, client: AsyncClient):
        await client.post(
            "/api/auth/register",
            json={
                "email": "bob@test.com",
                "password": "pass123",
                "display_name": "Bob",
            },
        )
        resp = await client.post(
            "/api/auth/login",
            json={
                "email": "bob@test.com",
                "password": "pass123",
                "device_id": "device-001",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client: AsyncClient):
        await client.post(
            "/api/auth/register",
            json={
                "email": "carol@test.com",
                "password": "correct",
                "display_name": "Carol",
            },
        )
        resp = await client.post(
            "/api/auth/login",
            json={
                "email": "carol@test.com",
                "password": "wrong",
                "device_id": "device-001",
            },
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, client: AsyncClient):
        resp = await client.post(
            "/api/auth/login",
            json={
                "email": "nobody@test.com",
                "password": "pass",
                "device_id": "device-001",
            },
        )
        assert resp.status_code == 401


class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_success(self, client: AsyncClient):
        info = await register_and_login(client)
        resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": info["refresh_token"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_refresh_new_token_works(self, client: AsyncClient):
        """刷新后的新 access_token 能正常访问 /me"""
        info = await register_and_login(client, email="refresh_new@test.com")
        resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": info["refresh_token"]},
        )
        new_tokens = resp.json()
        me_resp = await client.get(
            "/api/auth/me", headers=auth_header(new_tokens["access_token"])
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "refresh_new@test.com"

    @pytest.mark.asyncio
    async def test_refresh_returns_new_refresh_token(self, client: AsyncClient):
        """刷新后返回新的 refresh_token，且新 refresh_token 也能继续刷新"""
        info = await register_and_login(client, email="chain@test.com")
        resp1 = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": info["refresh_token"]},
        )
        new_refresh = resp1.json()["refresh_token"]
        # 用新的 refresh_token 再次刷新
        resp2 = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": new_refresh},
        )
        assert resp2.status_code == 200
        assert "access_token" in resp2.json()

    @pytest.mark.asyncio
    async def test_refresh_invalid_token(self, client: AsyncClient):
        resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": "invalid-token"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_with_access_token_fails(self, client: AsyncClient):
        """用 access_token 去刷新应该失败（type 不匹配）"""
        info = await register_and_login(client, email="wrongtype@test.com")
        resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": info["access_token"]},
        )
        assert resp.status_code == 401


class TestMe:
    @pytest.mark.asyncio
    async def test_me_success(self, client: AsyncClient):
        info = await register_and_login(client, email="me@test.com")
        resp = await client.get(
            "/api/auth/me", headers=auth_header(info["access_token"])
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "me@test.com"

    @pytest.mark.asyncio
    async def test_me_no_token(self, client: AsyncClient):
        resp = await client.get("/api/auth/me")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_me_invalid_token(self, client: AsyncClient):
        resp = await client.get(
            "/api/auth/me", headers=auth_header("invalid-token")
        )
        assert resp.status_code == 401
