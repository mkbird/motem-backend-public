"""Text-to-image stub endpoint tests — POST /api/games/text-to-image.

Currently always returns 501 Not Implemented; tests pin the contract so
clients implementing against the stub don't drift.
"""

import pytest
from httpx import AsyncClient

from tests.conftest import auth_header, register_and_login


@pytest.mark.asyncio
async def test_text_to_image_returns_501_for_authenticated_user(client: AsyncClient):
    user = await register_and_login(client, email="t2i_user@example.com")
    resp = await client.post(
        "/api/games/text-to-image",
        json={"prompt": "a sci-fi spaceship"},
        headers=auth_header(user["access_token"]),
    )
    assert resp.status_code == 501
    body = resp.json()
    assert body.get("detail") == "not_implemented"


@pytest.mark.asyncio
async def test_text_to_image_requires_auth(client: AsyncClient):
    """No token → 401 (FastAPI auth dependency before reaching the 501 raise)."""
    resp = await client.post(
        "/api/games/text-to-image",
        json={"prompt": "anything"},
    )
    # Without auth the get_current_user dep returns 401
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_text_to_image_validates_prompt_field(client: AsyncClient):
    user = await register_and_login(client, email="t2i_validate@example.com")
    # Missing required `prompt` field → 422 from Pydantic
    resp = await client.post(
        "/api/games/text-to-image",
        json={},
        headers=auth_header(user["access_token"]),
    )
    assert resp.status_code == 422
