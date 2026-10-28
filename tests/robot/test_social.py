"""Phase B social router 测试。

覆盖：
- GET /api/users/{id_or_handle} —— self / other / 不存在 / 按 handle 查
- POST /api/users/{id}/follow + DELETE 幂等性
- GET /api/users/{id}/followers + /following 分页 + is_following 状态
- 不能关注自己
- PATCH /api/auth/me 更新 username（合法 / 非法 / 冲突）
- POST /api/auth/me/avatar 上传（成功 / 类型不支持）

不覆盖：avatar 5MB 大小上限（IO 慢，单独跑），429 速率限制。
"""

import io
import json
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import AIGame, GameLike, UserGameView
from tests.conftest import auth_header, register_and_login


async def _register(client: AsyncClient, email: str, name: str = "User") -> tuple[str, str]:
    """返回 (user_id, access_token)。conftest.register_and_login 返 dict，
    我们解出 access_token + user.id 让测试更紧凑。"""
    bundle = await register_and_login(client, email=email, display_name=name)
    return bundle["user"]["id"], bundle["access_token"]


# =====================================================================
# GET /api/users/{id_or_handle}
# =====================================================================

class TestGetUserProfile:
    @pytest.mark.asyncio
    async def test_get_other_profile_by_id(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "social_alice1@test.com", "Alice")
        bob_id, bob_token = await _register(client, "social_bob1@test.com", "Bob")
        resp = await client.get(f"/api/users/{bob_id}", headers=auth_header(alice_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == bob_id
        assert data["display_name"] == "Bob"
        assert data["is_following"] is False
        assert data["follows_me"] is False
        assert data["followers_count"] == 0
        assert data["following_count"] == 0
        assert "email" not in data
        assert "timezone" not in data
        assert "locale" not in data

    @pytest.mark.asyncio
    async def test_get_self_profile_via_users_endpoint(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "social_self1@test.com", "Self")
        resp = await client.get(f"/api/users/{alice_id}", headers=auth_header(alice_token))
        assert resp.status_code == 200
        # is_following / follows_me 对自己始终 false
        assert resp.json()["is_following"] is False
        assert resp.json()["follows_me"] is False

    @pytest.mark.asyncio
    async def test_get_user_by_handle(self, client: AsyncClient):
        _, token = await _register(client, "social_handle1@test.com", "Handle")
        # 设置 username
        patch_resp = await client.patch(
            "/api/auth/me",
            json={"username": "alicehandle1"},
            headers=auth_header(token),
        )
        assert patch_resp.status_code == 200
        # 再注册 bob 并按 handle 查 alice
        _, bob_token = await _register(client, "social_handle1_bob@test.com", "Bob")
        resp = await client.get("/api/users/alicehandle1", headers=auth_header(bob_token))
        assert resp.status_code == 200
        assert resp.json()["username"] == "alicehandle1"

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, client: AsyncClient):
        _, token = await _register(client, "social_404@test.com", "U")
        resp = await client.get("/api/users/nonexistent_handle_xyz", headers=auth_header(token))
        assert resp.status_code == 404


# =====================================================================
# GET /api/users/{id_or_handle}/games
# =====================================================================

class TestUserProfileGames:
    @pytest.mark.asyncio
    async def test_created_games_privacy_for_self_and_other(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        alice_id, alice_token = await _register(client, "profile_games_a@test.com", "Alice")
        _, bob_token = await _register(client, "profile_games_b@test.com", "Bob")
        now = datetime.utcnow()
        for game_id, visibility in [
            ("ai_profile_private", "private"),
            ("ai_profile_public", "public_view_only"),
        ]:
            db_session.add(AIGame(
                game_id=game_id,
                owner_id=alice_id,
                title=game_id,
                title_i18n=json.dumps({"en": game_id, "zh-Hans": game_id}),
                description="",
                description_i18n=json.dumps({"en": "", "zh-Hans": ""}),
                icon="🎮",
                input_mode="touch",
                kind="ai",
                visibility=visibility,
                original_game_id=None,
                template_from="",
                session_id=None,
                like_count=0,
                favorite_count=0,
                share_count=0,
                remix_count=0,
                created_at=now,
                updated_at=now,
            ))
            db_session.add(UserGameView(
                user_id=alice_id,
                game_id=game_id,
                view_type="created",
                added_at=now,
            ))
        await db_session.commit()

        own = await client.get(
            f"/api/users/{alice_id}/games?tab=created",
            headers=auth_header(alice_token),
        )
        assert own.status_code == 200
        assert {g["game_id"] for g in own.json()["games"]} == {
            "ai_profile_private",
            "ai_profile_public",
        }

        other = await client.get(
            f"/api/users/{alice_id}/games?tab=created",
            headers=auth_header(bob_token),
        )
        assert other.status_code == 200
        assert [g["game_id"] for g in other.json()["games"]] == ["ai_profile_public"]

    @pytest.mark.asyncio
    async def test_other_favorited_games_are_private(
        self, client: AsyncClient
    ):
        alice_id, _alice_token = await _register(client, "profile_fav_a@test.com", "Alice")
        _, bob_token = await _register(client, "profile_fav_b@test.com", "Bob")
        resp = await client.get(
            f"/api/users/{alice_id}/games?tab=favorited",
            headers=auth_header(bob_token),
        )
        assert resp.status_code == 200
        assert resp.json()["games"] == []

    @pytest.mark.asyncio
    async def test_liked_games_only_return_public_for_other(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        alice_id, _alice_token = await _register(client, "profile_liked_a@test.com", "Alice")
        owner_id, _owner_token = await _register(client, "profile_liked_owner@test.com", "Owner")
        _, bob_token = await _register(client, "profile_liked_b@test.com", "Bob")
        now = datetime.utcnow()
        for game_id, visibility in [
            ("ai_liked_private", "private"),
            ("ai_liked_public", "public_remixable"),
        ]:
            db_session.add(AIGame(
                game_id=game_id,
                owner_id=owner_id,
                title=game_id,
                title_i18n=json.dumps({"en": game_id, "zh-Hans": game_id}),
                description="",
                description_i18n=json.dumps({"en": "", "zh-Hans": ""}),
                icon="🎮",
                input_mode="touch",
                kind="ai",
                visibility=visibility,
                original_game_id=None,
                template_from="",
                session_id=None,
                like_count=1,
                favorite_count=0,
                share_count=0,
                remix_count=0,
                created_at=now,
                updated_at=now,
            ))
            db_session.add(GameLike(user_id=alice_id, game_id=game_id, created_at=now))
        await db_session.commit()

        resp = await client.get(
            f"/api/users/{alice_id}/games?tab=liked",
            headers=auth_header(bob_token),
        )
        assert resp.status_code == 200
        assert [g["game_id"] for g in resp.json()["games"]] == ["ai_liked_public"]


# =====================================================================
# Follow / Unfollow
# =====================================================================

class TestFollowToggle:
    @pytest.mark.asyncio
    async def test_follow_then_unfollow(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "fol_a@test.com", "Alice")
        bob_id, _ = await _register(client, "fol_b@test.com", "Bob")
        # alice follows bob
        resp = await client.post(f"/api/users/{bob_id}/follow", headers=auth_header(alice_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_following"] is True
        assert data["followers_count"] == 1

        # alice GET bob -> is_following=true
        get_resp = await client.get(f"/api/users/{bob_id}", headers=auth_header(alice_token))
        assert get_resp.json()["is_following"] is True

        # alice unfollows
        del_resp = await client.delete(f"/api/users/{bob_id}/follow", headers=auth_header(alice_token))
        assert del_resp.status_code == 200
        assert del_resp.json()["is_following"] is False
        assert del_resp.json()["followers_count"] == 0

    @pytest.mark.asyncio
    async def test_follow_idempotent(self, client: AsyncClient):
        _, alice_token = await _register(client, "fol_idem_a@test.com", "Alice")
        bob_id, _ = await _register(client, "fol_idem_b@test.com", "Bob")
        for _ in range(3):
            resp = await client.post(f"/api/users/{bob_id}/follow", headers=auth_header(alice_token))
            assert resp.status_code == 200
            assert resp.json()["followers_count"] == 1   # 只该是 1，不重复入库

    @pytest.mark.asyncio
    async def test_unfollow_idempotent(self, client: AsyncClient):
        _, alice_token = await _register(client, "ufol_idem_a@test.com", "Alice")
        bob_id, _ = await _register(client, "ufol_idem_b@test.com", "Bob")
        # 还没关注就 DELETE，应仍 200 并返 0 / false
        resp = await client.delete(f"/api/users/{bob_id}/follow", headers=auth_header(alice_token))
        assert resp.status_code == 200
        assert resp.json()["is_following"] is False
        assert resp.json()["followers_count"] == 0

    @pytest.mark.asyncio
    async def test_cannot_follow_self(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "self_fol@test.com", "Self")
        resp = await client.post(f"/api/users/{alice_id}/follow", headers=auth_header(alice_token))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_follows_me_flag(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "fme_a@test.com", "Alice")
        bob_id, bob_token = await _register(client, "fme_b@test.com", "Bob")
        # bob follows alice
        await client.post(f"/api/users/{alice_id}/follow", headers=auth_header(bob_token))
        # alice 看 bob → follows_me 应为 true
        resp = await client.get(f"/api/users/{bob_id}", headers=auth_header(alice_token))
        assert resp.json()["follows_me"] is True
        assert resp.json()["is_following"] is False  # alice 没关注 bob

    @pytest.mark.asyncio
    async def test_delete_account_cleans_follow_edges(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "fol_delete_a@test.com", "Alice")
        bob_id, bob_token = await _register(client, "fol_delete_b@test.com", "Bob")
        await client.post(f"/api/users/{bob_id}/follow", headers=auth_header(alice_token))
        await client.post(f"/api/users/{alice_id}/follow", headers=auth_header(bob_token))

        deleted = await client.delete("/api/auth/me", headers=auth_header(alice_token))
        assert deleted.status_code == 204

        bob = await client.get(f"/api/users/{bob_id}", headers=auth_header(bob_token))
        assert bob.status_code == 200
        assert bob.json()["followers_count"] == 0
        assert bob.json()["following_count"] == 0


# =====================================================================
# Followers / Following lists
# =====================================================================

class TestFollowLists:
    @pytest.mark.asyncio
    async def test_list_followers_after_follow(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "list_a@test.com", "Alice")
        _, bob_token = await _register(client, "list_b@test.com", "Bob")
        await client.post(f"/api/users/{alice_id}/follow", headers=auth_header(bob_token))
        resp = await client.get(f"/api/users/{alice_id}/followers", headers=auth_header(alice_token))
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["display_name"] == "Bob"

    @pytest.mark.asyncio
    async def test_list_following(self, client: AsyncClient):
        alice_id, alice_token = await _register(client, "ling_a@test.com", "Alice")
        bob_id, _ = await _register(client, "ling_b@test.com", "Bob")
        await client.post(f"/api/users/{bob_id}/follow", headers=auth_header(alice_token))
        resp = await client.get(f"/api/users/{alice_id}/following", headers=auth_header(alice_token))
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["display_name"] == "Bob"

    @pytest.mark.asyncio
    async def test_followers_is_following_flag(self, client: AsyncClient):
        """alice 看自己粉丝列表 —— 列表里的 user 是否 alice 已关注。"""
        alice_id, alice_token = await _register(client, "rec_a@test.com", "Alice")
        bob_id, bob_token = await _register(client, "rec_b@test.com", "Bob")
        # bob follows alice
        await client.post(f"/api/users/{alice_id}/follow", headers=auth_header(bob_token))
        # alice 也回关 bob
        await client.post(f"/api/users/{bob_id}/follow", headers=auth_header(alice_token))
        resp = await client.get(f"/api/users/{alice_id}/followers", headers=auth_header(alice_token))
        items = resp.json()["items"]
        assert len(items) == 1
        # alice 已经关注 bob 了
        assert items[0]["is_following"] is True


# =====================================================================
# PATCH /api/auth/me — username field
# =====================================================================

class TestPatchUsername:
    @pytest.mark.asyncio
    async def test_set_valid_username(self, client: AsyncClient):
        _, token = await _register(client, "uname_ok@test.com", "U")
        resp = await client.patch(
            "/api/auth/me",
            json={"username": "validuser_01"},
            headers=auth_header(token),
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "validuser_01"

    @pytest.mark.asyncio
    async def test_invalid_format_rejected(self, client: AsyncClient):
        _, token = await _register(client, "uname_inv@test.com", "U")
        # 数字开头
        resp = await client.patch(
            "/api/auth/me",
            json={"username": "1user"},
            headers=auth_header(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_too_short_rejected(self, client: AsyncClient):
        _, token = await _register(client, "uname_short@test.com", "U")
        resp = await client.patch(
            "/api/auth/me",
            json={"username": "ab"},
            headers=auth_header(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_uppercase_normalized_to_lowercase(self, client: AsyncClient):
        """uppercase 输入由 router strip().lower() 规范化 —— 通过校验，不报错。"""
        _, token = await _register(client, "uname_up@test.com", "U")
        resp = await client.patch(
            "/api/auth/me",
            json={"username": "UpperCase"},
            headers=auth_header(token),
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "uppercase"

    @pytest.mark.asyncio
    async def test_username_collision_409(self, client: AsyncClient):
        _, alice_token = await _register(client, "uname_colla@test.com", "A")
        _, bob_token = await _register(client, "uname_collb@test.com", "B")
        # alice 占用 handle
        await client.patch(
            "/api/auth/me",
            json={"username": "shared_handle"},
            headers=auth_header(alice_token),
        )
        # bob 想用同一个 handle
        resp = await client.patch(
            "/api/auth/me",
            json={"username": "shared_handle"},
            headers=auth_header(bob_token),
        )
        assert resp.status_code == 409


# =====================================================================
# Avatar upload
# =====================================================================

class TestAvatarUpload:
    @pytest.mark.asyncio
    async def test_upload_jpeg(self, client: AsyncClient, tmp_path, monkeypatch):
        from robot.config import settings

        monkeypatch.setattr(settings, "avatar_dir", str(tmp_path))
        _, token = await _register(client, "av_jpeg@test.com", "A")
        files = {"image": ("avatar.jpg", io.BytesIO(b"\xff\xd8\xff\xe0fake"), "image/jpeg")}
        resp = await client.post(
            "/api/auth/me/avatar",
            files=files,
            headers=auth_header(token),
        )
        assert resp.status_code == 200
        avatar_url = resp.json()["avatar_url"]
        assert avatar_url.startswith("/static/avatars/")
        assert (tmp_path / avatar_url.rsplit("/", 1)[-1]).exists()

    @pytest.mark.asyncio
    async def test_upload_unsupported_type(self, client: AsyncClient):
        _, token = await _register(client, "av_bad@test.com", "A")
        files = {"image": ("avatar.svg", io.BytesIO(b"<svg/>"), "image/svg+xml")}
        resp = await client.post(
            "/api/auth/me/avatar",
            files=files,
            headers=auth_header(token),
        )
        assert resp.status_code == 415

    @pytest.mark.asyncio
    async def test_upload_persists_to_user_avatar_url(self, client: AsyncClient, tmp_path, monkeypatch):
        from robot.config import settings

        monkeypatch.setattr(settings, "avatar_dir", str(tmp_path))
        _, token = await _register(client, "av_persist@test.com", "A")
        files = {"image": ("avatar.png", io.BytesIO(b"fakepng"), "image/png")}
        upload_resp = await client.post(
            "/api/auth/me/avatar",
            files=files,
            headers=auth_header(token),
        )
        assert upload_resp.status_code == 200
        # GET /me 应能拿到刚上传的 avatar_url
        me = await client.get("/api/auth/me", headers=auth_header(token))
        assert me.json()["avatar_url"] == upload_resp.json()["avatar_url"]
