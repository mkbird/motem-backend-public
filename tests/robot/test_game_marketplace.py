"""Game marketplace tests — explore / mine / like / favorite / remix / visibility.

参考 widget marketplace 测试结构组织，覆盖矩阵：
- visibility 状态机 + builtin 锁
- explore / mine 端点筛选
- like / favorite toggle + 计数
- remix 全流程 + 拒绝条件 + Godot 工程隔离 + .godot/ 剔除
- 作者修改不新建（service 层）
- /files / /download 访问鉴权
- 缓存失效（monkeypatch redis）
- 链式 A→B→C 血缘
- 端到端 user journey
- input_mode 透传

测试用 SQLite in-memory（tests/conftest.py），不动到生产 DB。
Redis 被 conftest mock 成 AsyncMock，cache 路径在 service 内被 try/except 兜住。
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import (
    AIGame,
    ExploreGameView,
    GameLike,
    UserGameView,
)
from tests.conftest import auth_header, register_and_login


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_games_dir(tmp_path, monkeypatch):
    """Redirect every module-level `GAMES_DIR` copy to a per-test tmp dir.

    Why: 历史上这些测试直接写真实仓库 `games/`，crash 时残留 stub 污染 worktree。
    每个 `from .service import GAMES_DIR` 副本都得单独 patch，否则测试代码里
    `game_service.GAMES_DIR` 和 service 内部 `GAMES_DIR` 会指到不同路径。
    """
    games_dir = tmp_path / "games"
    games_dir.mkdir()
    for target in (
        "robot.games.service.GAMES_DIR",
        "robot.games.router.GAMES_DIR",
        "robot.games.share_router.GAMES_DIR",
        "robot.agent.tools.game_tools.GAMES_DIR",
    ):
        monkeypatch.setattr(target, games_dir, raising=True)


@pytest_asyncio.fixture
async def builtin_seed(db_session: AsyncSession) -> str:
    """Seed 1 个 builtin（kind='builtin', input_mode='body_pose'）。"""
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id="builtin_shooter",
        owner_id=None,
        title="Space Shooter",
        title_i18n=json.dumps({"en": "Space Shooter", "zh-Hans": "太空射手"}),
        description="Shoot work enemies",
        description_i18n=json.dumps({"en": "Shoot work enemies", "zh-Hans": "射击工作敌人"}),
        icon="🚀",
        input_mode="body_pose",
        kind="builtin",
        visibility="public_view_only",
        original_game_id=None,
        template_from="",
        session_id=None,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    db_session.add(ExploreGameView(game_id="builtin_shooter", added_at=now))
    await db_session.commit()
    return "builtin_shooter"


@pytest_asyncio.fixture
async def alice_game(db_session: AsyncSession, client: AsyncClient) -> dict:
    """登录 Alice + 直接造一个 Alice 拥有的 AI 游戏（私有）。

    避开 Claude SDK，直接 ORM 写 ai_games。
    """
    auth = await register_and_login(
        client, email="alice_g@example.com", device_id="dev-a-g"
    )
    game_id = "ai_test_alice_g_001"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=auth["user"]["id"],
        title="Alice's Game",
        title_i18n=json.dumps({"en": "Alice's Game", "zh-Hans": "爱丽丝游戏"}),
        description="by alice",
        description_i18n=json.dumps({"en": "by alice", "zh-Hans": "爱丽丝制作"}),
        icon="🎮",
        input_mode="body_pose",
        kind="ai",
        visibility="private",
        original_game_id=None,
        template_from="",
        session_id="alice-session-1",
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    db_session.add(UserGameView(
        user_id=auth["user"]["id"], game_id=game_id,
        view_type="created", added_at=now,
    ))
    await db_session.commit()
    return {**auth, "game_id": game_id}


@pytest_asyncio.fixture
async def alice_public_game(
    db_session: AsyncSession, alice_game: dict
) -> dict:
    """Alice 把游戏设为 public_remixable + 进 explore index。"""
    from robot.games import service as game_service

    await game_service.set_visibility(
        db_session, alice_game["game_id"], "public_remixable"
    )
    await db_session.commit()
    return alice_game


@pytest_asyncio.fixture
async def bob(client: AsyncClient) -> dict:
    return await register_and_login(
        client, email="bob_g@example.com", device_id="dev-b-g"
    )


@pytest_asyncio.fixture
async def alice_game_with_dir(
    db_session: AsyncSession, client: AsyncClient, alice_public_game: dict,
):
    """让 alice_public_game 在 fs 上有 Godot 工程目录，能被 clone_game 拷贝。"""
    from robot.games import service as game_service

    src = game_service.GAMES_DIR / alice_public_game["game_id"]
    src.mkdir(parents=True, exist_ok=True)
    (src / "project.godot").write_text(
        '[application]\nconfig/name="Alice"\n', encoding="utf-8"
    )
    (src / "scenes").mkdir(exist_ok=True)
    (src / "scenes" / "main.tscn").write_text(
        "[gd_scene format=3]\n[node name=\"Root\" type=\"Node2D\"]", encoding="utf-8"
    )
    (src / "scenes" / "main.gd").write_text(
        "extends Node2D\nfunc _ready(): pass\n", encoding="utf-8"
    )
    # ★ 故意造一个 .godot/ 编辑器缓存目录 — clone 必须剔除
    (src / ".godot").mkdir(exist_ok=True)
    (src / ".godot" / "editor_cache.txt").write_text("cache content")
    (src / "meta.json").write_text(
        json.dumps({
            "game_id": alice_public_game["game_id"],
            "title": "Alice's Game",
            "input_mode": "body_pose",
        }),
        encoding="utf-8",
    )

    yield alice_public_game

    # 清理 — 删 src + 所有 remix 出去的副本
    import shutil
    if src.is_dir():
        shutil.rmtree(src, ignore_errors=True)
    src_zip = game_service.GAMES_DIR / f"{alice_public_game['game_id']}.zip"
    if src_zip.exists():
        src_zip.unlink()
    rows = (
        await db_session.execute(
            select(AIGame).where(
                AIGame.original_game_id == alice_public_game["game_id"]
            )
        )
    ).scalars().all()
    for r in rows:
        d = game_service.GAMES_DIR / r.game_id
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        z = game_service.GAMES_DIR / f"{r.game_id}.zip"
        if z.exists():
            z.unlink()


# ─────────────────────────────────────────────────────────────────────
# Section A — visibility 状态机
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_default_visibility_private_no_explore(
    db_session: AsyncSession, alice_game: dict
):
    row = (
        await db_session.execute(
            select(AIGame).where(AIGame.game_id == alice_game["game_id"])
        )
    ).scalar_one()
    assert row.visibility == "private"
    explore_row = (
        await db_session.execute(
            select(ExploreGameView).where(
                ExploreGameView.game_id == alice_game["game_id"]
            )
        )
    ).scalar_one_or_none()
    assert explore_row is None


@pytest.mark.asyncio
async def test_visibility_private_to_public_inserts_explore(
    db_session: AsyncSession, client: AsyncClient, alice_game: dict
):
    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    explore_row = (
        await db_session.execute(
            select(ExploreGameView).where(
                ExploreGameView.game_id == alice_game["game_id"]
            )
        )
    ).scalar_one_or_none()
    assert explore_row is not None


@pytest.mark.asyncio
async def test_visibility_public_to_private_removes_explore(
    db_session: AsyncSession, client: AsyncClient, alice_public_game: dict
):
    resp = await client.patch(
        f"/api/games/{alice_public_game['game_id']}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_public_game["access_token"]),
    )
    assert resp.status_code == 200
    explore_row = (
        await db_session.execute(
            select(ExploreGameView).where(
                ExploreGameView.game_id == alice_public_game["game_id"]
            )
        )
    ).scalar_one_or_none()
    assert explore_row is None


@pytest.mark.asyncio
async def test_visibility_public_to_public_keeps_explore(
    db_session: AsyncSession, client: AsyncClient, alice_public_game: dict
):
    resp = await client.patch(
        f"/api/games/{alice_public_game['game_id']}/visibility",
        json={"visibility": "public_view_only"},
        headers=auth_header(alice_public_game["access_token"]),
    )
    assert resp.status_code == 200
    rows = (
        await db_session.execute(
            select(ExploreGameView).where(
                ExploreGameView.game_id == alice_public_game["game_id"]
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_visibility_invalid_400(
    client: AsyncClient, alice_game: dict
):
    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/visibility",
        json={"visibility": "haha_random"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_visibility_non_owner_403(
    client: AsyncClient, alice_game: dict, bob: dict
):
    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_visibility_builtin_locked(
    client: AsyncClient, builtin_seed: str, alice_game: dict
):
    resp = await client.patch(
        f"/api/games/{builtin_seed}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────
# Section B — Explore / Mine 端点
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explore_includes_public_excludes_private(
    client: AsyncClient, alice_game: dict, bob: dict
):
    r1 = await client.get(
        "/api/games/explore", headers=auth_header(bob["access_token"])
    )
    ids = [g["game_id"] for g in r1.json()["games"]]
    assert alice_game["game_id"] not in ids

    await client.patch(
        f"/api/games/{alice_game['game_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_game["access_token"]),
    )
    r2 = await client.get(
        "/api/games/explore", headers=auth_header(bob["access_token"])
    )
    ids2 = [g["game_id"] for g in r2.json()["games"]]
    assert alice_game["game_id"] in ids2


@pytest.mark.asyncio
async def test_explore_includes_builtin(
    client: AsyncClient, builtin_seed: str
):
    resp = await client.get("/api/games/explore")
    assert resp.status_code == 200
    ids = [g["game_id"] for g in resp.json()["games"]]
    assert builtin_seed in ids


@pytest.mark.asyncio
async def test_explore_input_mode_field_present(
    client: AsyncClient, builtin_seed: str
):
    """input_mode 必须透传给 iOS（决定是否启动 FaceDetector）。"""
    resp = await client.get("/api/games/explore")
    games = {g["game_id"]: g for g in resp.json()["games"]}
    assert games[builtin_seed]["input_mode"] == "body_pose"


@pytest.mark.asyncio
async def test_mine_includes_owned_regardless_of_visibility(
    client: AsyncClient, alice_game: dict
):
    resp = await client.get(
        "/api/games/mine", headers=auth_header(alice_game["access_token"])
    )
    games = {g["game_id"]: g for g in resp.json()["games"]}
    assert alice_game["game_id"] in games
    assert games[alice_game["game_id"]]["view_type"] == "created"
    assert games[alice_game["game_id"]]["is_owner"] is True


@pytest.mark.asyncio
async def test_mine_excludes_others_private(
    client: AsyncClient, alice_game: dict, bob: dict
):
    resp = await client.get(
        "/api/games/mine", headers=auth_header(bob["access_token"])
    )
    ids = [g["game_id"] for g in resp.json()["games"]]
    assert alice_game["game_id"] not in ids


@pytest.mark.asyncio
async def test_mine_includes_favorited(
    client: AsyncClient, alice_public_game: dict, bob: dict
):
    await client.post(
        f"/api/games/{alice_public_game['game_id']}/favorite",
        headers=auth_header(bob["access_token"]),
    )
    r = await client.get(
        "/api/games/mine", headers=auth_header(bob["access_token"])
    )
    games = {g["game_id"]: g for g in r.json()["games"]}
    assert games[alice_public_game["game_id"]]["view_type"] == "favorited"


@pytest.mark.asyncio
async def test_mine_unauthenticated_401(client: AsyncClient):
    resp = await client.get("/api/games/mine")
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Section C — Like / Favorite
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_like_toggle_idempotent(
    client: AsyncClient, alice_public_game: dict, bob: dict
):
    gid = alice_public_game["game_id"]
    headers = auth_header(bob["access_token"])
    r1 = await client.post(f"/api/games/{gid}/like", headers=headers)
    assert r1.json() == {"liked_by_me": True, "like_count": 1}
    r2 = await client.post(f"/api/games/{gid}/like", headers=headers)
    assert r2.json() == {"liked_by_me": False, "like_count": 0}
    r3 = await client.post(f"/api/games/{gid}/like", headers=headers)
    assert r3.json() == {"liked_by_me": True, "like_count": 1}


@pytest.mark.asyncio
async def test_favorite_own_toggle(client: AsyncClient, alice_game: dict):
    """owner 收藏自己的游戏 — 走 ai_games.owner_favorited 列；toggle 两次复原。"""
    gid = alice_game["game_id"]
    h = auth_header(alice_game["access_token"])

    r1 = await client.post(f"/api/games/{gid}/favorite", headers=h)
    assert r1.status_code == 200
    assert r1.json() == {"favorited_by_me": True, "favorite_count": 1}

    # mine 端点反映 favorited_by_me=True；同时 view_type 仍是 created（不重复行）
    mine = (await client.get("/api/games/mine", headers=h)).json()["games"]
    me_row = next(g for g in mine if g["game_id"] == gid)
    assert me_row["favorited_by_me"] is True
    assert me_row["view_type"] == "created"
    assert me_row["is_owner"] is True

    # 再 toggle 一次 → 取消
    r2 = await client.post(f"/api/games/{gid}/favorite", headers=h)
    assert r2.status_code == 200
    assert r2.json() == {"favorited_by_me": False, "favorite_count": 0}

    mine2 = (await client.get("/api/games/mine", headers=h)).json()["games"]
    me_row2 = next(g for g in mine2 if g["game_id"] == gid)
    assert me_row2["favorited_by_me"] is False


@pytest.mark.asyncio
async def test_favorite_toggle_count(
    client: AsyncClient, alice_public_game: dict, bob: dict
):
    gid = alice_public_game["game_id"]
    h = auth_header(bob["access_token"])
    r1 = await client.post(f"/api/games/{gid}/favorite", headers=h)
    assert r1.json() == {"favorited_by_me": True, "favorite_count": 1}
    r2 = await client.post(f"/api/games/{gid}/favorite", headers=h)
    assert r2.json() == {"favorited_by_me": False, "favorite_count": 0}


@pytest.mark.asyncio
async def test_explore_liked_by_me_flag(
    client: AsyncClient, alice_public_game: dict, bob: dict
):
    gid = alice_public_game["game_id"]
    h = auth_header(bob["access_token"])
    await client.post(f"/api/games/{gid}/like", headers=h)
    resp = await client.get("/api/games/explore", headers=h)
    games = {g["game_id"]: g for g in resp.json()["games"]}
    assert games[gid]["liked_by_me"] is True


@pytest.mark.asyncio
async def test_share_increment(client: AsyncClient, alice_public_game: dict):
    gid = alice_public_game["game_id"]
    r1 = await client.post(f"/api/games/{gid}/share")
    assert r1.json()["share_count"] == 1
    r2 = await client.post(f"/api/games/{gid}/share")
    assert r2.json()["share_count"] == 2


# ─────────────────────────────────────────────────────────────────────
# Section D — Remix (含 Godot 工程隔离 + .godot/ 剔除)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remix_creates_new_row(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_dir: dict, bob: dict,
):
    src = alice_game_with_dir["game_id"]
    resp = await client.post(
        f"/api/games/{src}/remix", headers=auth_header(bob["access_token"])
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["game_id"]
    assert new_id != src
    assert resp.json()["original_game_id"] == src
    assert resp.json()["visibility"] == "private"
    assert resp.json()["input_mode"] == "body_pose"  # ★ 透传

    new_row = (
        await db_session.execute(
            select(AIGame).where(AIGame.game_id == new_id)
        )
    ).scalar_one()
    assert new_row.owner_id == bob["user"]["id"]
    assert new_row.input_mode == "body_pose"


@pytest.mark.asyncio
async def test_remix_drops_godot_cache_dir(
    client: AsyncClient, alice_game_with_dir: dict, bob: dict,
):
    """★ Godot 工程隔离的关键 — clone 必须剔除 .godot/ 编辑器缓存目录，
    否则副本会带着源的编辑器状态污染（路径引用、唯一 ID 等）。"""
    from robot.games import service as game_service

    src = alice_game_with_dir["game_id"]
    resp = await client.post(
        f"/api/games/{src}/remix", headers=auth_header(bob["access_token"])
    )
    new_id = resp.json()["game_id"]
    new_dir = game_service.GAMES_DIR / new_id

    # 副本目录里**不能**有 .godot/
    assert not (new_dir / ".godot").exists(), \
        ".godot/ 编辑器缓存必须被 clone_game 剔除"
    # 但其他文件应原样在
    assert (new_dir / "project.godot").exists()
    assert (new_dir / "scenes" / "main.tscn").exists()
    assert (new_dir / "scenes" / "main.gd").exists()
    assert (new_dir / "meta.json").exists()


@pytest.mark.asyncio
async def test_remix_main_gd_byte_identical_to_source(
    client: AsyncClient, alice_game_with_dir: dict, bob: dict,
):
    """★ 关键代码文件（main.gd / main.tscn）字节级一致 — 业务上 remix 是
    "复制后用户再编辑"，初始状态必须等于源。"""
    from robot.games import service as game_service

    src = alice_game_with_dir["game_id"]
    src_dir = game_service.GAMES_DIR / src
    src_main_bytes = (src_dir / "scenes" / "main.gd").read_bytes()

    resp = await client.post(
        f"/api/games/{src}/remix", headers=auth_header(bob["access_token"])
    )
    new_id = resp.json()["game_id"]
    new_dir = game_service.GAMES_DIR / new_id
    new_main_bytes = (new_dir / "scenes" / "main.gd").read_bytes()

    assert src_main_bytes == new_main_bytes, "main.gd 应字节级一致"


@pytest.mark.asyncio
async def test_remix_meta_json_overrides(
    client: AsyncClient, alice_game_with_dir: dict, bob: dict,
):
    """★ meta.json 的 game_id / owner_id / template_from / original_game_id /
    history 必须改写；input_mode 透传。"""
    from robot.games import service as game_service

    src = alice_game_with_dir["game_id"]
    resp = await client.post(
        f"/api/games/{src}/remix", headers=auth_header(bob["access_token"])
    )
    new_id = resp.json()["game_id"]
    new_meta = json.loads(
        (game_service.GAMES_DIR / new_id / "meta.json").read_text()
    )
    assert new_meta["game_id"] == new_id
    assert new_meta["owner_id"] == bob["user"]["id"]
    assert new_meta["template_from"] == src
    assert new_meta["original_game_id"] == src
    assert new_meta["input_mode"] == "body_pose"  # 透传源
    assert new_meta["visibility"] == "private"
    # history 重置为单条 system 消息（不带 alice 的对话历史）
    assert len(new_meta["history"]) == 1
    assert "Remixed from" in new_meta["history"][0]["content"]


@pytest.mark.asyncio
async def test_remix_view_only_rejected(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_dir: dict, bob: dict,
):
    src = alice_game_with_dir["game_id"]
    await client.patch(
        f"/api/games/{src}/visibility",
        json={"visibility": "public_view_only"},
        headers=auth_header(alice_game_with_dir["access_token"]),
    )
    resp = await client.post(
        f"/api/games/{src}/remix", headers=auth_header(bob["access_token"])
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_private_rejected(
    client: AsyncClient, alice_game: dict, bob: dict,
):
    resp = await client.post(
        f"/api/games/{alice_game['game_id']}/remix",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_builtin_rejected(
    client: AsyncClient, builtin_seed: str, alice_game: dict,
):
    resp = await client.post(
        f"/api/games/{builtin_seed}/remix",
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_own_400(
    client: AsyncClient, alice_game_with_dir: dict
):
    resp = await client.post(
        f"/api/games/{alice_game_with_dir['game_id']}/remix",
        headers=auth_header(alice_game_with_dir["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_increments_source_count(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_dir: dict, bob: dict,
):
    src = alice_game_with_dir["game_id"]
    await client.post(
        f"/api/games/{src}/remix", headers=auth_header(bob["access_token"])
    )
    src_row = (
        await db_session.execute(
            select(AIGame).where(AIGame.game_id == src)
        )
    ).scalar_one()
    await db_session.refresh(src_row)
    assert src_row.remix_count == 1


@pytest.mark.asyncio
async def test_remix_idempotent_for_same_user(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_dir: dict, bob: dict,
):
    """同一用户对同一原作多次 remix → 返回已存在的副本，不再创建新行。

    防御 in-game ✏️ 路径用户反复点 → /api/games/.../remix → mine 列表里同一原作
    堆出十几份副本的 bug。"""
    src = alice_game_with_dir["game_id"]
    h = auth_header(bob["access_token"])

    r1 = await client.post(f"/api/games/{src}/remix", headers=h)
    assert r1.status_code == 200
    new_id_1 = r1.json()["game_id"]

    # 第二次 remix 同一原作 — 应该返回同一 game_id
    r2 = await client.post(f"/api/games/{src}/remix", headers=h)
    assert r2.status_code == 200
    new_id_2 = r2.json()["game_id"]
    assert new_id_2 == new_id_1, "second remix should return existing copy, not create new"

    # 第三次也一样
    r3 = await client.post(f"/api/games/{src}/remix", headers=h)
    assert r3.json()["game_id"] == new_id_1

    # 原作 remix_count 只 +1（idempotent 不再 ++）
    src_row = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == src))
    ).scalar_one()
    await db_session.refresh(src_row)
    assert src_row.remix_count == 1, f"remix_count should stay 1, got {src_row.remix_count}"

    # bob 的 mine 里只应该有一份这个 source 的副本
    mine = (await client.get("/api/games/mine", headers=h)).json()["games"]
    bob_copies = [g for g in mine if g.get("original_game_id") == src]
    assert len(bob_copies) == 1, f"bob's mine should have exactly 1 copy of {src}, got {len(bob_copies)}"


# ─────────────────────────────────────────────────────────────────────
# Section E — 作者修改不新建（service 层）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modify_via_upsert_no_new_row(
    db_session: AsyncSession, alice_public_game: dict
):
    """模拟 modify_game 调 upsert_game_row — 行数不变、game_id 不变、
    visibility 保持 public_remixable 不被覆盖回 private。"""
    from robot.games import service as game_service

    before = (await db_session.execute(select(AIGame))).scalars().all()
    before_count = len(before)

    await game_service.upsert_game_row(
        game_id=alice_public_game["game_id"],
        owner_id=alice_public_game["user"]["id"],
        title="Updated Title",
        title_i18n={"en": "Updated", "zh-Hans": "已更新"},
        description="v2",
        description_i18n={"en": "v2", "zh-Hans": "v2"},
        icon="🎮",
        input_mode="body_pose",
        db=db_session,
    )

    after = (await db_session.execute(select(AIGame))).scalars().all()
    assert len(after) == before_count

    row = next(r for r in after if r.game_id == alice_public_game["game_id"])
    await db_session.refresh(row)
    assert row.visibility == "public_remixable", \
        "modify 不传 visibility 不应覆盖回默认 private（widget 同款 bug）"
    assert json.loads(row.title_i18n)["en"] == "Updated"


# ─────────────────────────────────────────────────────────────────────
# Section F — /files / /download 鉴权
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_files_others_cannot_read_private(
    client: AsyncClient, alice_game: dict, bob: dict
):
    resp = await client.get(
        f"/api/games/{alice_game['game_id']}/files",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_files_builtin_returns_empty(
    client: AsyncClient, builtin_seed: str
):
    resp = await client.get(f"/api/games/{builtin_seed}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "builtin"
    assert body["files"] == {}


@pytest.mark.asyncio
async def test_download_builtin_404(
    client: AsyncClient, builtin_seed: str
):
    resp = await client.get(f"/api/games/{builtin_seed}/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_private_others_404(
    client: AsyncClient, alice_game: dict, bob: dict,
):
    """Private 别人下不到 — 404 不暴露存在性。"""
    resp = await client.get(
        f"/api/games/{alice_game['game_id']}/download",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_public_anyone(
    client: AsyncClient, alice_game_with_dir: dict, bob: dict,
):
    """Public_remixable 任何登录用户都能下。"""
    resp = await client.get(
        f"/api/games/{alice_game_with_dir['game_id']}/download",
        headers=auth_header(bob["access_token"]),
    )
    # 200 zip OR 404（如果 fs 上没 zip 也没 dir 触发 fallback path 失败）
    # alice_game_with_dir 有 dir，所以应该 200（zip 不存在时 fallback make_archive）
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_download_concurrent_zip_repackage_lock(
    monkeypatch, client: AsyncClient, alice_game_with_dir: dict, bob: dict,
):
    """P3 #3 回归：两个并发 download 命中 zip 缺失分支时，per-game asyncio.Lock
    保证 ``make_archive`` 只被调用一次（第二个 waiter 走 double-check 直接返回）。

    没有锁的话，两个请求会同时 ``make_archive`` 写到同一路径，第二个 truncate
    第一个的输出，客户端解压失败误报"下载失败"。
    """
    import asyncio
    import shutil
    from robot.games import router as games_router
    from robot.games import service as game_service

    game_id = alice_game_with_dir["game_id"]
    zip_path = game_service.GAMES_DIR / f"{game_id}.zip"
    if zip_path.exists():
        zip_path.unlink()

    # 把 make_archive 包一层 sleep 拉宽 race window，并计数被调用次数。
    # 没有锁的话两个并发请求都会进 make_archive；有锁则只有一个进，第二个
    # 拿锁后 zip_path.exists()=True 直接 skip。
    call_count = 0
    real_make_archive = shutil.make_archive

    def slow_make_archive(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # 模拟大游戏 zip 打包的 100~500ms
        import time
        time.sleep(0.3)
        return real_make_archive(*args, **kwargs)

    monkeypatch.setattr(games_router.shutil, "make_archive", slow_make_archive)

    headers = auth_header(bob["access_token"])
    # Concurrent 5 个并发请求 —— 没锁时极大概率多次重打
    results = await asyncio.gather(*[
        client.get(f"/api/games/{game_id}/download", headers=headers)
        for _ in range(5)
    ])
    for r in results:
        assert r.status_code == 200, f"download failed: {r.status_code} {r.text}"
        # zip 内容非空（race condition 下会出现 truncated zip / 0 bytes）
        assert len(r.content) > 100

    assert call_count == 1, (
        f"make_archive should be called exactly once under lock, got {call_count}"
    )


# ─────────────────────────────────────────────────────────────────────
# Section G — Cache 失效
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_invalidation_on_visibility_change(
    monkeypatch, client: AsyncClient, alice_game: dict
):
    deleted: list[str] = []

    async def fake_delete(*keys):
        deleted.extend(keys)
        return len(keys)

    async def fake_setex(*a, **kw): return True
    async def fake_get(key): return None

    from robot.games import service as game_service
    monkeypatch.setattr(game_service.redis_client, "delete", fake_delete)
    monkeypatch.setattr(game_service.redis_client, "setex", fake_setex)
    monkeypatch.setattr(game_service.redis_client, "get", fake_get)

    await client.patch(
        f"/api/games/{alice_game['game_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert any("games:explore" in k for k in deleted)
    assert any(f"games:meta:{alice_game['game_id']}" in k for k in deleted)
    assert any(f"games:mine:{alice_game['user']['id']}" in k for k in deleted)


# ─────────────────────────────────────────────────────────────────────
# Section H — 链式 remix + lifecycle
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_lifecycle(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_dir: dict, bob: dict,
):
    """E2E：B 看到 A 公开游戏 → like → favorite → remix → 副本设公开 →
    unlike → unfavorite。每步 DB 状态正确。"""
    src = alice_game_with_dir["game_id"]
    bh = auth_header(bob["access_token"])
    bid = bob["user"]["id"]

    # 1. like
    r1 = await client.post(f"/api/games/{src}/like", headers=bh)
    assert r1.json()["like_count"] == 1

    # 2. favorite
    r2 = await client.post(f"/api/games/{src}/favorite", headers=bh)
    assert r2.json()["favorite_count"] == 1

    # 3. mine 含 favorited
    mine1 = await client.get("/api/games/mine", headers=bh)
    m1 = {g["game_id"]: g for g in mine1.json()["games"]}
    assert m1[src]["view_type"] == "favorited"

    # 4. remix
    r4 = await client.post(f"/api/games/{src}/remix", headers=bh)
    new_id = r4.json()["game_id"]

    # 5. mine 含 src(favorited) + new(created)
    mine2 = await client.get("/api/games/mine", headers=bh)
    m2 = {g["game_id"]: g for g in mine2.json()["games"]}
    assert m2[src]["view_type"] == "favorited"
    assert m2[new_id]["view_type"] == "created"

    # 6. unlike
    r6 = await client.post(f"/api/games/{src}/like", headers=bh)
    assert r6.json()["like_count"] == 0
    assert r6.json()["liked_by_me"] is False

    # 7. unfavorite
    r7 = await client.post(f"/api/games/{src}/favorite", headers=bh)
    assert r7.json()["favorited_by_me"] is False

    mine3 = await client.get("/api/games/mine", headers=bh)
    m3_ids = [g["game_id"] for g in mine3.json()["games"]]
    assert src not in m3_ids  # unfavorited 后不在 mine
    assert new_id in m3_ids   # remix 副本仍在（owner 是 B）


# ─────────────────────────────────────────────────────────────────────
# Section I — 边界 / 并发 / 链式 / 缓存命中 / i18n / 防御性视图表
# ─────────────────────────────────────────────────────────────────────
#
# 这一节是上一轮 ad-hoc 写测试时漏的"看起来不重要直到出 bug"场景。
# 与 widget marketplace 的 Section I 一一对照（pattern 复用）。


@pytest.mark.asyncio
async def test_chain_remix_lineage_and_isolated_counts(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_dir: dict, bob: dict,
):
    """A → B → C 三层 remix。验证每一层 original_game_id 指向**直接**上游
    （不是链顶），上游 remix_count 只 +1（不级联）。"""
    A = alice_game_with_dir["game_id"]
    bh = auth_header(bob["access_token"])
    ah = auth_header(alice_game_with_dir["access_token"])

    # B = bob remix A
    r1 = await client.post(f"/api/games/{A}/remix", headers=bh)
    assert r1.status_code == 200
    B = r1.json()["game_id"]

    # B 必须 visibility=public_remixable 才能再被 remix
    await client.patch(
        f"/api/games/{B}/visibility",
        json={"visibility": "public_remixable"},
        headers=bh,
    )

    # C = alice remix B（注意 alice 不是 B 的 owner —— B 的 owner 是 bob）
    r2 = await client.post(f"/api/games/{B}/remix", headers=ah)
    assert r2.status_code == 200, r2.text
    C = r2.json()["game_id"]

    # 取最新行
    rows = {
        gid: (await db_session.execute(
            select(AIGame).where(AIGame.game_id == gid)
        )).scalar_one()
        for gid in [A, B, C]
    }
    for r in rows.values():
        await db_session.refresh(r)

    # original_game_id 链：C → B → A → None
    assert rows[A].original_game_id is None
    assert rows[B].original_game_id == A
    assert rows[C].original_game_id == B

    # remix_count 不级联
    assert rows[A].remix_count == 1, "A 只被 B 直接 remix 过，不应该因 C 而 +2"
    assert rows[B].remix_count == 1
    assert rows[C].remix_count == 0


@pytest.mark.asyncio
async def test_rapid_serial_like_toggles_terminal_state(
    client: AsyncClient, alice_public_game: dict, bob: dict,
):
    """同一用户对同一游戏快速串行 toggle 6 次（偶数次 → 终态 0 / 奇数次 → 1）。

    注意：SQLite + 共享 test db_session 不支持真并发（async client 在
    同一 session 上并发请求会抛 IllegalStateChangeError）。生产 MySQL
    会用独立连接并发，行级锁保证安全 — 这里测代码路径在快速 toggle
    下不死锁 / 不抛 / 计数不漂负就够。
    """
    gid = alice_public_game["game_id"]
    h = auth_header(bob["access_token"])

    counts = []
    for _ in range(6):
        r = await client.post(f"/api/games/{gid}/like", headers=h)
        assert r.status_code == 200
        counts.append(r.json()["like_count"])
        # 每次 count 必须 ∈ {0,1}（绝不 -1 / 2）
        assert r.json()["like_count"] in (0, 1)

    # 6 次 toggle → 终态应该是 0（偶数次回到原点）
    assert counts[-1] == 0
    # 序列必须严格交替 1, 0, 1, 0, 1, 0
    assert counts == [1, 0, 1, 0, 1, 0]


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_data_skip_db(
    monkeypatch, db_session: AsyncSession, client: AsyncClient,
    alice_public_game: dict, bob: dict,
):
    """缓存命中时不应再 JOIN ai_games × explore_game_view。

    用 monkeypatch 给 redis.get 返回预制 JSON，断言端点直接返这份数据
    而不是从 DB 查（DB 里 alice_public_game 真有但缓存里没有 → 不出现
    在响应里；缓存里有 fake_id 但 DB 里没有 → 出现在响应里）。
    """
    from robot.games import service as game_service

    fake_payload = [{
        "game_id": "ai_cache_only_game",
        "owner_id": "u-cache",
        "title": "Cache Only",
        "title_i18n_raw": json.dumps({"en": "Cache Only", "zh-Hans": "纯缓存"}),
        "description": "",
        "description_i18n_raw": json.dumps({"en": "", "zh-Hans": ""}),
        "icon": "💾",
        "input_mode": "touch",
        "kind": "ai",
        "visibility": "public_remixable",
        "original_game_id": None,
        "template_from": "",
        "session_id": None,
        "like_count": 99,
        "favorite_count": 0,
        "share_count": 0,
        "remix_count": 0,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }]

    async def fake_get(key):
        if "games:explore" in key:
            return json.dumps(fake_payload)
        return None

    async def fake_setex(*a, **kw): return True

    monkeypatch.setattr(game_service.redis_client, "get", fake_get)
    monkeypatch.setattr(game_service.redis_client, "setex", fake_setex)

    resp = await client.get(
        "/api/games/explore", headers=auth_header(bob["access_token"])
    )
    assert resp.status_code == 200
    ids = [g["game_id"] for g in resp.json()["games"]]
    # 缓存里有但 DB 里不存在 → 出现，证明端点读了缓存
    assert "ai_cache_only_game" in ids
    # DB 里真有的 alice_public_game **不**出现 → 证明端点没走 DB JOIN
    assert alice_public_game["game_id"] not in ids


@pytest.mark.asyncio
async def test_explore_pagination_limit_200(
    db_session: AsyncSession, client: AsyncClient, bob: dict,
):
    """造 250 个 public 行，调 /explore 应正好返回 200，且按 added_at desc 稳定排序。"""
    now = datetime.utcnow()
    for i in range(250):
        ts = now.replace(microsecond=i)
        gid = f"ai_pagi_{i:03d}"
        db_session.add(AIGame(
            game_id=gid,
            owner_id=None,  # builtin 简化（避开 owner FK）
            title=f"Pagi {i}",
            title_i18n=json.dumps({"en": f"Pagi {i}", "zh-Hans": f"分{i}"}),
            description="",
            description_i18n=json.dumps({"en": "", "zh-Hans": ""}),
            icon="📦",
            input_mode="body_pose",
            kind="builtin",
            visibility="public_view_only",
            original_game_id=None,
            template_from="",
            session_id=None,
            like_count=0, favorite_count=0, share_count=0, remix_count=0,
            created_at=ts, updated_at=ts,
        ))
        db_session.add(ExploreGameView(
            game_id=gid, sort_score=0, added_at=ts
        ))
    await db_session.commit()

    resp = await client.get(
        "/api/games/explore", headers=auth_header(bob["access_token"])
    )
    assert resp.status_code == 200
    games = resp.json()["games"]
    assert len(games) == 200, f"limit=200 应该截断，但拿到 {len(games)}"

    # 排序断言：added_at desc → 最大 i 在前
    pagi_ids = [g["game_id"] for g in games if g["game_id"].startswith("ai_pagi_")]
    nums = [int(g.split("_")[-1]) for g in pagi_ids]
    assert nums == sorted(nums, reverse=True), "排序应该是 added_at desc"


@pytest.mark.asyncio
async def test_accept_language_renders_i18n_title(
    client: AsyncClient, builtin_seed: str
):
    """`Accept-Language: en` 返英文 title，`zh-Hans` 返中文 title。"""
    # 英文请求
    r_en = await client.get(
        "/api/games/explore",
        headers={"Accept-Language": "en"},
    )
    games_en = {g["game_id"]: g for g in r_en.json()["games"]}
    assert games_en[builtin_seed]["title"] == "Space Shooter"

    # 中文请求
    r_zh = await client.get(
        "/api/games/explore",
        headers={"Accept-Language": "zh-Hans"},
    )
    games_zh = {g["game_id"]: g for g in r_zh.json()["games"]}
    assert games_zh[builtin_seed]["title"] == "太空射手"


@pytest.mark.asyncio
async def test_explore_excludes_rows_without_view_index(
    db_session: AsyncSession, client: AsyncClient, alice_game: dict
):
    """防御性测试：手动把 visibility 改成 public_view_only 但**不**更新
    `explore_game_view` —— 模拟数据漂移。/explore 端点不应"猜"visibility 列，
    只认 explore_game_view 索引行。

    这条保证"事件驱动维护两表"约束被强制：宁可少返一行也不脏读。
    """
    # 不走 PATCH 端点，直接 ORM mutate visibility
    row = (
        await db_session.execute(
            select(AIGame).where(AIGame.game_id == alice_game["game_id"])
        )
    ).scalar_one()
    row.visibility = "public_view_only"
    await db_session.commit()

    resp = await client.get("/api/games/explore")
    ids = [g["game_id"] for g in resp.json()["games"]]
    assert alice_game["game_id"] not in ids, \
        "没有 explore_game_view 索引行的 game 不应出现在 explore"


@pytest.mark.asyncio
async def test_visibility_change_invalidates_cached_explore(
    monkeypatch, client: AsyncClient, alice_game: dict
):
    """改 visibility 必须 DEL games:explore，否则 60s 缓存 TTL 内其他用户
    看不到新公开的游戏（或仍能看到下架的）。"""
    from robot.games import service as game_service

    deleted: list[str] = []

    async def fake_delete(*keys):
        deleted.extend(keys)
        return len(keys)

    async def fake_setex(*a, **kw): return True
    async def fake_get(key): return None

    monkeypatch.setattr(game_service.redis_client, "delete", fake_delete)
    monkeypatch.setattr(game_service.redis_client, "setex", fake_setex)
    monkeypatch.setattr(game_service.redis_client, "get", fake_get)

    # private → public_remixable
    await client.patch(
        f"/api/games/{alice_game['game_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert any("games:explore" in k for k in deleted), \
        "visibility 切换必须失效全局 explore key"

    # public_remixable → private（再切回去）
    deleted.clear()
    await client.patch(
        f"/api/games/{alice_game['game_id']}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert any("games:explore" in k for k in deleted), \
        "下架时也必须失效 explore key"


@pytest.mark.asyncio
async def test_user_game_view_pk_prevents_dual_view_type(
    db_session: AsyncSession, alice_public_game: dict
):
    """同一 (user_id, game_id) 复合 PK 保证只能存在一种 view_type。

    业务上：作者不能再 favorite 自己（端点已拒）；Schema 层防御也要测。
    """
    from sqlalchemy.exc import IntegrityError

    aid = alice_public_game["user"]["id"]
    gid = alice_public_game["game_id"]

    # Alice 已经有 view_type='created'（fixture 创建）
    existing = (
        await db_session.execute(
            select(UserGameView).where(
                UserGameView.user_id == aid,
                UserGameView.game_id == gid,
            )
        )
    ).scalar_one()
    assert existing.view_type == "created"

    # 试图再插入 favorited 行 — PK 冲突
    db_session.add(UserGameView(
        user_id=aid, game_id=gid,
        view_type="favorited", added_at=datetime.utcnow(),
    ))
    try:
        await db_session.commit()
        await db_session.rollback()
        assert False, "复合 PK 冲突应抛 IntegrityError"
    except IntegrityError:
        await db_session.rollback()


@pytest.mark.asyncio
async def test_input_mode_propagates_through_remix(
    client: AsyncClient, alice_game_with_dir: dict, bob: dict,
):
    """source.input_mode='body_pose' → 副本 input_mode='body_pose'。
    这是 Godot 单例约束的关键链路：副本必须延续源 input_mode，
    否则 body_pose 副本被切成 touch 后 FaceDetector 不启动 → 黑屏。
    """
    src = alice_game_with_dir["game_id"]
    resp = await client.post(
        f"/api/games/{src}/remix", headers=auth_header(bob["access_token"])
    )
    assert resp.status_code == 200
    # API 响应直接返 input_mode，前端不需要再 fetch
    assert resp.json()["input_mode"] == "body_pose"

    # mine 端点也应该看到副本带正确 input_mode
    mine = await client.get(
        "/api/games/mine", headers=auth_header(bob["access_token"])
    )
    new_id = resp.json()["game_id"]
    g = next(g for g in mine.json()["games"] if g["game_id"] == new_id)
    assert g["input_mode"] == "body_pose", \
        "副本必须延续源 input_mode（防黑屏关键）"
