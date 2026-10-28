"""Shared test fixtures — uses SQLite in-memory for isolation."""

import os
import secrets
import sys
from unittest.mock import AsyncMock, MagicMock

# Tests never use a developer's or deployment's real signing key.
os.environ["ROBOT_JWT_SECRET_KEY"] = secrets.token_hex(32)

# ── Mock proto stubs early ──
_PROTO_MOCKS = [
    "speech_pb2", "speech_pb2_grpc",
    "src.proto", "src.proto.speech_pb2", "src.proto.speech_pb2_grpc",
    "aichat.proto", "aichat.proto.speech_pb2", "aichat.proto.speech_pb2_grpc",
    "grpc", "grpc.aio",
    "opuslib",
]
for _mod in _PROTO_MOCKS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# ── Mock Redis to avoid needing a running Redis server ──
_redis_mock = MagicMock()
_redis_mock.redis_client = AsyncMock(return_value=None)
_redis_mock.redis_client.get = AsyncMock(return_value=None)
_redis_mock.redis_client.set = AsyncMock(return_value=None)
_redis_mock.get_redis = AsyncMock(return_value=_redis_mock.redis_client)
for _mod in ["robot.db.redis", "robot.memory.redis_client"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = _redis_mock

import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from robot.db.models import Base


# ── In-memory SQLite engine (no MySQL needed for tests) ──

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
test_session_factory = async_sessionmaker(
    test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest_asyncio.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create tables and provide a fresh DB session per test."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with test_session_factory() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient with the test DB injected."""
    from robot.db.database import get_db
    from robot.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ── Helper: register + login, return token and user info ──


async def register_and_login(
    client: AsyncClient,
    email: str = "test@example.com",
    password: str = "testpass123",
    display_name: str = "测试用户",
    device_id: str = "test-device-001",
) -> dict:
    """Register a user and login, return {access_token, refresh_token, user}."""
    await client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "display_name": display_name},
    )
    resp = await client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": password,
            "device_id": device_id,
            "device_name": "Test iPhone",
        },
    )
    tokens = resp.json()

    me_resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "user": me_resp.json(),
    }


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
