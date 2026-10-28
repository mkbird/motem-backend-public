"""Widget marketplace tests — explore / mine / like / favorite / remix / visibility.

覆盖范围：
- visibility 状态机 + builtin 锁
- explore / mine 端点筛选
- like / favorite toggle + 计数
- remix 全流程 + 拒绝条件
- 作者修改不新建（service 层）
- /files 访问鉴权
- builtin 种子可被 fixture 注入

测试用 SQLite in-memory（`tests/conftest.py`），不会动到生产 DB。Redis
被 conftest mock 成 AsyncMock，cache 路径在 service 内被 try/except 兜住。
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
    AIWidget,
    ExploreWidgetView,
    UserWidgetView,
    WidgetLike,
)
from tests.conftest import auth_header, register_and_login


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def builtin_seed(db_session: AsyncSession) -> str:
    """Seed 1 个 builtin (kind='builtin', visibility='public_view_only')。

    模仿 alembic 迁移里的种子写法。返回 widget_id。
    """
    now = datetime.utcnow()
    row = AIWidget(
        widget_id="builtin_time",
        owner_id=None,
        title_i18n=json.dumps({"en": "Time", "zh-Hans": "时间"}),
        description_i18n=json.dumps({"en": "Current time", "zh-Hans": "当前时间"}),
        supported_sizes=json.dumps(["small", "medium", "large", "xlarge"]),
        base_template_id="",
        visibility="public_view_only",
        kind="builtin",
        original_widget_id=None,
        like_count=0,
        favorite_count=0,
        remix_count=0,
        created_at=now,
        updated_at=now,
    )
    db_session.add(row)
    db_session.add(ExploreWidgetView(widget_id="builtin_time", added_at=now))
    await db_session.commit()
    return "builtin_time"


@pytest_asyncio.fixture
async def alice_widget(db_session: AsyncSession, client: AsyncClient) -> dict:
    """登录 Alice + 直接在 DB 里造一个 Alice 拥有的 AI widget。

    避开 Claude SDK，用 service.upsert_widget_row 模拟 create 路径。
    """
    auth = await register_and_login(
        client, email="alice@example.com", device_id="dev-a"
    )
    from robot.widgets import service as widget_service

    widget_id = "aw_test_alice_001"
    # 直接在 db_session 里写 — bypass async_session 因为 conftest 用的是 fixture 的 session
    now = datetime.utcnow()
    db_session.add(AIWidget(
        widget_id=widget_id,
        owner_id=auth["user"]["id"],
        title_i18n=json.dumps({"en": "Alice Widget", "zh-Hans": "爱丽丝小组件"}),
        description_i18n=json.dumps({"en": "by alice", "zh-Hans": "爱丽丝制作"}),
        supported_sizes=json.dumps(["small", "medium"]),
        base_template_id="",
        visibility="private",
        kind="ai",
        original_widget_id=None,
        like_count=0, favorite_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    db_session.add(UserWidgetView(
        user_id=auth["user"]["id"], widget_id=widget_id,
        view_type="created", added_at=now,
    ))
    await db_session.commit()
    return {**auth, "widget_id": widget_id}


@pytest_asyncio.fixture
async def alice_public_widget(
    db_session: AsyncSession, alice_widget: dict
) -> dict:
    """Alice 把她的 widget 设为 public_remixable + 进 explore index。"""
    from robot.widgets import service as widget_service

    await widget_service.set_visibility(
        db_session, alice_widget["widget_id"], "public_remixable"
    )
    await db_session.commit()
    return alice_widget


@pytest_asyncio.fixture
async def bob(client: AsyncClient) -> dict:
    return await register_and_login(
        client, email="bob@example.com", device_id="dev-b"
    )


# ─────────────────────────────────────────────────────────────────────
# Section A — visibility 状态机
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_default_visibility_private_no_explore(
    db_session: AsyncSession, alice_widget: dict
):
    """新建组件默认 private，不在 explore_widget_view。"""
    row = (
        await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == alice_widget["widget_id"])
        )
    ).scalar_one()
    assert row.visibility == "private"

    explore_row = (
        await db_session.execute(
            select(ExploreWidgetView).where(
                ExploreWidgetView.widget_id == alice_widget["widget_id"]
            )
        )
    ).scalar_one_or_none()
    assert explore_row is None


@pytest.mark.asyncio
async def test_visibility_private_to_public_inserts_explore(
    db_session: AsyncSession, client: AsyncClient, alice_widget: dict
):
    resp = await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_widget["access_token"]),
    )
    assert resp.status_code == 200, resp.text

    explore_row = (
        await db_session.execute(
            select(ExploreWidgetView).where(
                ExploreWidgetView.widget_id == alice_widget["widget_id"]
            )
        )
    ).scalar_one_or_none()
    assert explore_row is not None


@pytest.mark.asyncio
async def test_visibility_public_to_public_keeps_explore(
    db_session: AsyncSession, client: AsyncClient, alice_public_widget: dict
):
    resp = await client.patch(
        f"/api/widgets/{alice_public_widget['widget_id']}/visibility",
        json={"visibility": "public_view_only"},
        headers=auth_header(alice_public_widget["access_token"]),
    )
    assert resp.status_code == 200

    rows = (
        await db_session.execute(
            select(ExploreWidgetView).where(
                ExploreWidgetView.widget_id == alice_public_widget["widget_id"]
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_visibility_public_to_private_removes_explore(
    db_session: AsyncSession, client: AsyncClient, alice_public_widget: dict
):
    resp = await client.patch(
        f"/api/widgets/{alice_public_widget['widget_id']}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_public_widget["access_token"]),
    )
    assert resp.status_code == 200

    explore_row = (
        await db_session.execute(
            select(ExploreWidgetView).where(
                ExploreWidgetView.widget_id == alice_public_widget["widget_id"]
            )
        )
    ).scalar_one_or_none()
    assert explore_row is None


@pytest.mark.asyncio
async def test_visibility_invalid_enum_400(
    client: AsyncClient, alice_widget: dict
):
    resp = await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/visibility",
        json={"visibility": "haha_random_value"},
        headers=auth_header(alice_widget["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_visibility_non_owner_403(
    client: AsyncClient, alice_widget: dict, bob: dict
):
    resp = await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_visibility_builtin_locked_403(
    client: AsyncClient, builtin_seed: str, alice_widget: dict
):
    """内置组件不允许任何用户修改 visibility（即使是 admin 也不行）。"""
    resp = await client.patch(
        f"/api/widgets/{builtin_seed}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_widget["access_token"]),
    )
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────
# Section B — explore / mine 端点
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explore_includes_public_excludes_private(
    client: AsyncClient, alice_widget: dict, bob: dict
):
    """Explore 返回 public，不返 private。"""
    # alice_widget 默认 private — 不应该在 explore
    r1 = await client.get(
        "/api/widgets/explore", headers=auth_header(bob["access_token"])
    )
    assert r1.status_code == 200
    ids = [w["widget_id"] for w in r1.json()["widgets"]]
    assert alice_widget["widget_id"] not in ids

    # 公开后 — 应该在 explore
    await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_widget["access_token"]),
    )
    r2 = await client.get(
        "/api/widgets/explore", headers=auth_header(bob["access_token"])
    )
    ids2 = [w["widget_id"] for w in r2.json()["widgets"]]
    assert alice_widget["widget_id"] in ids2


@pytest.mark.asyncio
async def test_explore_includes_builtin(
    client: AsyncClient, builtin_seed: str
):
    """匿名用户也能看到 builtin。"""
    resp = await client.get("/api/widgets/explore")
    assert resp.status_code == 200
    ids = [w["widget_id"] for w in resp.json()["widgets"]]
    assert builtin_seed in ids


@pytest.mark.asyncio
async def test_explore_owner_flag(
    client: AsyncClient, alice_public_widget: dict
):
    """Alice 自己 GET explore 看到自己 widget 时 is_owner=True。"""
    resp = await client.get(
        "/api/widgets/explore",
        headers=auth_header(alice_public_widget["access_token"]),
    )
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    assert widgets[alice_public_widget["widget_id"]]["is_owner"] is True


@pytest.mark.asyncio
async def test_mine_includes_owned_regardless_of_visibility(
    client: AsyncClient, alice_widget: dict
):
    """Alice 在 Mine 看到自己的 private widget。"""
    resp = await client.get(
        "/api/widgets/mine",
        headers=auth_header(alice_widget["access_token"]),
    )
    assert resp.status_code == 200
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    assert alice_widget["widget_id"] in widgets
    assert widgets[alice_widget["widget_id"]]["view_type"] == "created"


@pytest.mark.asyncio
async def test_mine_excludes_others_private(
    client: AsyncClient, alice_widget: dict, bob: dict
):
    """Bob 看不到 Alice 的 private widget。"""
    resp = await client.get(
        "/api/widgets/mine", headers=auth_header(bob["access_token"])
    )
    ids = [w["widget_id"] for w in resp.json()["widgets"]]
    assert alice_widget["widget_id"] not in ids


@pytest.mark.asyncio
async def test_mine_includes_favorited_with_correct_view_type(
    client: AsyncClient, alice_public_widget: dict, bob: dict
):
    """Bob 收藏 Alice 公开 widget 后，在 Bob Mine 里 view_type='favorited'。"""
    r1 = await client.post(
        f"/api/widgets/{alice_public_widget['widget_id']}/favorite",
        headers=auth_header(bob["access_token"]),
    )
    assert r1.status_code == 200
    assert r1.json()["favorited_by_me"] is True

    r2 = await client.get(
        "/api/widgets/mine", headers=auth_header(bob["access_token"])
    )
    widgets = {w["widget_id"]: w for w in r2.json()["widgets"]}
    assert alice_public_widget["widget_id"] in widgets
    assert widgets[alice_public_widget["widget_id"]]["view_type"] == "favorited"


@pytest.mark.asyncio
async def test_mine_unauthenticated_401(client: AsyncClient):
    resp = await client.get("/api/widgets/mine")
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Section C — Like / Favorite toggling + counters
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_like_toggle_idempotent_and_count(
    client: AsyncClient, alice_public_widget: dict, bob: dict
):
    """Bob 点赞 → +1，再点 → -1。"""
    wid = alice_public_widget["widget_id"]
    headers = auth_header(bob["access_token"])

    r1 = await client.post(f"/api/widgets/{wid}/like", headers=headers)
    assert r1.json() == {"liked_by_me": True, "like_count": 1}

    r2 = await client.post(f"/api/widgets/{wid}/like", headers=headers)
    assert r2.json() == {"liked_by_me": False, "like_count": 0}

    r3 = await client.post(f"/api/widgets/{wid}/like", headers=headers)
    assert r3.json() == {"liked_by_me": True, "like_count": 1}


@pytest.mark.asyncio
async def test_favorite_toggle_count(
    client: AsyncClient, alice_public_widget: dict, bob: dict
):
    wid = alice_public_widget["widget_id"]
    headers = auth_header(bob["access_token"])

    r1 = await client.post(f"/api/widgets/{wid}/favorite", headers=headers)
    assert r1.json() == {"favorited_by_me": True, "favorite_count": 1}

    r2 = await client.post(f"/api/widgets/{wid}/favorite", headers=headers)
    assert r2.json() == {"favorited_by_me": False, "favorite_count": 0}


@pytest.mark.asyncio
async def test_favorite_own_widget_allowed(
    client: AsyncClient, alice_widget: dict
):
    """作者可以 favorite 自己创建的组件（widget_favorites 与 user_widget_view 解耦后）。
    第一次 toggle → 加入收藏；再 toggle → 取消收藏。"""
    headers = auth_header(alice_widget["access_token"])
    r1 = await client.post(
        f"/api/widgets/{alice_widget['widget_id']}/favorite", headers=headers
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["favorited_by_me"] is True
    assert body["favorite_count"] == 1

    r2 = await client.post(
        f"/api/widgets/{alice_widget['widget_id']}/favorite", headers=headers
    )
    assert r2.json() == {"favorited_by_me": False, "favorite_count": 0}


@pytest.mark.asyncio
async def test_unfavorite_removes_from_mine(
    client: AsyncClient, alice_public_widget: dict, bob: dict
):
    wid = alice_public_widget["widget_id"]
    headers = auth_header(bob["access_token"])

    await client.post(f"/api/widgets/{wid}/favorite", headers=headers)
    # Mine has it
    mine1 = await client.get("/api/widgets/mine", headers=headers)
    assert wid in [w["widget_id"] for w in mine1.json()["widgets"]]

    # Unfavorite
    await client.post(f"/api/widgets/{wid}/favorite", headers=headers)
    mine2 = await client.get("/api/widgets/mine", headers=headers)
    assert wid not in [w["widget_id"] for w in mine2.json()["widgets"]]


@pytest.mark.asyncio
async def test_explore_liked_by_me_flag(
    client: AsyncClient, alice_public_widget: dict, bob: dict
):
    wid = alice_public_widget["widget_id"]
    bob_headers = auth_header(bob["access_token"])

    await client.post(f"/api/widgets/{wid}/like", headers=bob_headers)
    resp = await client.get("/api/widgets/explore", headers=bob_headers)
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    assert widgets[wid]["liked_by_me"] is True


# ─────────────────────────────────────────────────────────────────────
# Section D — Remix
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def alice_widget_with_dir(
    db_session: AsyncSession, client: AsyncClient, alice_public_widget: dict,
    tmp_path, monkeypatch,
):
    """让 alice_public_widget 在 filesystem 上也有目录可以被 clone。

    把 widget_service.WIDGETS_DIR monkeypatch 到 pytest 的 `tmp_path` —
    避免测试写到真实的 motem-backend-widget/widgets/ 污染生产数据。
    历史上没 monkeypatch 时，clone_widget 在测试中创建的 remix 副本目录
    残留在仓库（aw_*** Alice 一堆孤儿），还得手动 rm。
    """
    from robot.widgets import service as widget_service
    from robot.widgets import router as widget_router

    test_widgets_dir = tmp_path / "widgets"
    test_widgets_dir.mkdir(parents=True, exist_ok=True)
    # service 和 router 都 import WIDGETS_DIR 到自己 module，要分别 patch
    monkeypatch.setattr(widget_service, "WIDGETS_DIR", test_widgets_dir)
    monkeypatch.setattr(widget_router, "WIDGETS_DIR", test_widgets_dir)

    src = test_widgets_dir / alice_public_widget["widget_id"]
    src.mkdir(parents=True, exist_ok=True)
    (src / "index.html").write_text("<html></html>", encoding="utf-8")
    (src / "app.js").write_text("// alice", encoding="utf-8")
    (src / "meta.json").write_text(
        json.dumps({"widget_id": alice_public_widget["widget_id"], "title": "Alice"}),
        encoding="utf-8",
    )
    yield alice_public_widget
    # tmp_path 的清理由 pytest 管 — 测试结束自动删，不需要显式 rmtree


@pytest.mark.asyncio
async def test_remix_public_remixable_creates_new_row(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    src_id = alice_widget_with_dir["widget_id"]
    resp = await client.post(
        f"/api/widgets/{src_id}/remix",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["widget_id"]
    assert new_id != src_id
    assert resp.json()["original_widget_id"] == src_id
    assert resp.json()["visibility"] == "private"

    new_row = (
        await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == new_id)
        )
    ).scalar_one()
    assert new_row.owner_id == bob["user"]["id"]
    assert new_row.original_widget_id == src_id
    assert new_row.visibility == "private"

    # Bob's UserWidgetView has the new widget as 'created'
    view_row = (
        await db_session.execute(
            select(UserWidgetView).where(
                UserWidgetView.user_id == bob["user"]["id"],
                UserWidgetView.widget_id == new_id,
            )
        )
    ).scalar_one()
    assert view_row.view_type == "created"


@pytest.mark.asyncio
async def test_remix_increments_source_remix_count(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    src_id = alice_widget_with_dir["widget_id"]
    await client.post(
        f"/api/widgets/{src_id}/remix",
        headers=auth_header(bob["access_token"]),
    )
    # Refresh
    await db_session.commit()
    src_row = (
        await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == src_id)
        )
    ).scalar_one()
    await db_session.refresh(src_row)
    assert src_row.remix_count == 1


@pytest.mark.asyncio
async def test_remix_view_only_rejected(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    src_id = alice_widget_with_dir["widget_id"]
    # Alice 把可见性改为 view_only
    await client.patch(
        f"/api/widgets/{src_id}/visibility",
        json={"visibility": "public_view_only"},
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )

    resp = await client.post(
        f"/api/widgets/{src_id}/remix",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_private_rejected(
    client: AsyncClient, alice_widget: dict, bob: dict,
):
    resp = await client.post(
        f"/api/widgets/{alice_widget['widget_id']}/remix",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_builtin_rejected(
    client: AsyncClient, builtin_seed: str, alice_widget: dict,
):
    resp = await client.post(
        f"/api/widgets/{builtin_seed}/remix",
        headers=auth_header(alice_widget["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_own_widget_400(
    client: AsyncClient, alice_widget_with_dir: dict,
):
    """作者不能 remix 自己 — 应该用 modify_widget 工具。"""
    resp = await client.post(
        f"/api/widgets/{alice_widget_with_dir['widget_id']}/remix",
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_remix_creates_independent_copy(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    """Remix 后原作 / 副本独立 — 改一份不影响另一份的 visibility / counts。"""
    src_id = alice_widget_with_dir["widget_id"]
    resp = await client.post(
        f"/api/widgets/{src_id}/remix",
        headers=auth_header(bob["access_token"]),
    )
    new_id = resp.json()["widget_id"]

    # Bob 把副本设为 public
    r = await client.patch(
        f"/api/widgets/{new_id}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(bob["access_token"]),
    )
    assert r.status_code == 200

    # Alice 原作仍然 public_remixable（不应被 Bob 的修改影响）
    src = (
        await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == src_id)
        )
    ).scalar_one()
    await db_session.refresh(src)
    assert src.visibility == "public_remixable"


# ─────────────────────────────────────────────────────────────────────
# Section E — 作者 modify 不新建（service 层）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_modify_via_upsert_no_new_row(
    db_session: AsyncSession, alice_public_widget: dict
):
    """模拟 modify_widget 工具调 upsert_widget_row — 行数不变、widget_id 不变、
    visibility 保持 public_remixable。"""
    from robot.widgets import service as widget_service

    before = (
        await db_session.execute(select(AIWidget))
    ).scalars().all()
    before_ids = {r.widget_id for r in before}
    before_count = len(before)

    # 调用 upsert（模拟 modify path — 不传 visibility，传 db 使用 test session）
    await widget_service.upsert_widget_row(
        widget_id=alice_public_widget["widget_id"],
        owner_id=alice_public_widget["user"]["id"],
        title_i18n={"en": "Updated", "zh-Hans": "已更新"},
        description_i18n={"en": "v2", "zh-Hans": "v2"},
        supported_sizes=["small", "medium", "large"],
        base_template_id="",
        db=db_session,
    )

    after = (
        await db_session.execute(select(AIWidget))
    ).scalars().all()
    after_ids = {r.widget_id for r in after}
    assert len(after) == before_count
    assert after_ids == before_ids

    # visibility 不变
    row = next(r for r in after if r.widget_id == alice_public_widget["widget_id"])
    await db_session.refresh(row)
    assert row.visibility == "public_remixable"
    # title 已更新
    assert json.loads(row.title_i18n)["en"] == "Updated"


# ─────────────────────────────────────────────────────────────────────
# Section F — /files 访问鉴权
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_files_owner_can_read_private(
    client: AsyncClient, alice_widget_with_dir: dict
):
    """Alice 能读自己的 widget — 即使设回 private。"""
    # 先改回 private
    await client.patch(
        f"/api/widgets/{alice_widget_with_dir['widget_id']}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    resp = await client.get(
        f"/api/widgets/{alice_widget_with_dir['widget_id']}/files",
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_explore_returns_updated_at_field(
    client: AsyncClient, alice_public_widget: dict
):
    """`/explore` 必须回传 `updated_at` ISO 字符串 — iOS 用它做内容同步比对。"""
    resp = await client.get("/api/widgets/explore")
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    row = widgets[alice_public_widget["widget_id"]]
    assert "updated_at" in row
    assert isinstance(row["updated_at"], str) and row["updated_at"]


@pytest.mark.asyncio
async def test_mine_returns_updated_at_field(
    client: AsyncClient, alice_widget: dict
):
    resp = await client.get(
        "/api/widgets/mine", headers=auth_header(alice_widget["access_token"])
    )
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    row = widgets[alice_widget["widget_id"]]
    assert isinstance(row["updated_at"], str) and row["updated_at"]


@pytest.mark.asyncio
async def test_modify_bumps_updated_at(
    db_session: AsyncSession, alice_public_widget: dict
):
    """作者改 widget → ai_widgets.updated_at 必须变 — 否则 iOS 比对永远相等
    没法触发跨设备 / 跨用户的内容同步。"""
    from robot.widgets import service as widget_service

    row = (
        await db_session.execute(
            select(AIWidget).where(
                AIWidget.widget_id == alice_public_widget["widget_id"]
            )
        )
    ).scalar_one()
    before = row.updated_at

    # 故意 sleep 一点，确保 datetime.utcnow() 拿到的不会和 before 同毫秒
    import asyncio
    await asyncio.sleep(0.01)

    await widget_service.upsert_widget_row(
        widget_id=alice_public_widget["widget_id"],
        owner_id=alice_public_widget["user"]["id"],
        title_i18n={"en": "v2", "zh-Hans": "v2"},
        description_i18n={"en": "", "zh-Hans": ""},
        supported_sizes=["small"],
        base_template_id="",
        db=db_session,
    )

    refreshed = (
        await db_session.execute(
            select(AIWidget).where(
                AIWidget.widget_id == alice_public_widget["widget_id"]
            )
        )
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.updated_at > before, \
        f"modify 后 updated_at 没增长: before={before} after={refreshed.updated_at}"


@pytest.mark.asyncio
async def test_files_others_cannot_read_private(
    client: AsyncClient, alice_widget: dict, bob: dict
):
    """Bob 不能访问 Alice 的 private widget — 返回 404 不暴露存在性。"""
    resp = await client.get(
        f"/api/widgets/{alice_widget['widget_id']}/files",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_files_others_can_read_public(
    client: AsyncClient, alice_widget_with_dir: dict, bob: dict
):
    resp = await client.get(
        f"/api/widgets/{alice_widget_with_dir['widget_id']}/files",
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_files_builtin_returns_empty_kind(
    client: AsyncClient, builtin_seed: str
):
    resp = await client.get(f"/api/widgets/{builtin_seed}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "builtin"
    assert body["files"] == {}


@pytest.mark.asyncio
async def test_unfavorite_after_visibility_to_private(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    """收藏后作者改 private → Bob 仍在 Mine 里看到（保留收藏关系），但 /files 返 404。

    收藏关系不被自动清理 — 作者可能临时下架后再公开，强行删收藏对用户不友好；
    iOS 端打开 widget 时被 /files 404 拦截即可让 widget 显示 "unavailable" 占位。
    """
    wid = alice_widget_with_dir["widget_id"]
    bob_headers = auth_header(bob["access_token"])
    alice_headers = auth_header(alice_widget_with_dir["access_token"])

    # Bob 收藏
    await client.post(f"/api/widgets/{wid}/favorite", headers=bob_headers)

    # Alice 转 private
    await client.patch(
        f"/api/widgets/{wid}/visibility",
        json={"visibility": "private"},
        headers=alice_headers,
    )

    # Bob 的 Mine 仍包含
    mine = await client.get("/api/widgets/mine", headers=bob_headers)
    assert wid in [w["widget_id"] for w in mine.json()["widgets"]]

    # 但 /files 拒绝
    files_resp = await client.get(
        f"/api/widgets/{wid}/files", headers=bob_headers
    )
    assert files_resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Section G — 缓存失效（用 monkeypatch 替换 redis）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_invalidation_on_visibility_change(
    monkeypatch, db_session: AsyncSession, client: AsyncClient, alice_widget: dict
):
    """改 visibility 必须 DEL widgets:explore + widgets:mine:{owner}。"""
    deleted_keys: list[str] = []
    setex_keys: list[str] = []

    async def fake_delete(*keys):
        for k in keys:
            deleted_keys.append(k)
        return len(keys)

    async def fake_setex(key, ttl, value):
        setex_keys.append(key)
        return True

    async def fake_get(key):
        return None  # 永远 miss

    from robot.widgets import service as widget_service
    monkeypatch.setattr(widget_service.redis_client, "delete", fake_delete)
    monkeypatch.setattr(widget_service.redis_client, "setex", fake_setex)
    monkeypatch.setattr(widget_service.redis_client, "get", fake_get)

    await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_widget["access_token"]),
    )

    assert any("widgets:explore" in k for k in deleted_keys)
    assert any(
        f"widgets:mine:{alice_widget['user']['id']}" in k for k in deleted_keys
    )
    assert any(f"widgets:meta:{alice_widget['widget_id']}" in k for k in deleted_keys)


# ─────────────────────────────────────────────────────────────────────
# Section H — Builtin seeding（验证 fixture 注入正确）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_builtin_in_explore_with_correct_metadata(
    client: AsyncClient, builtin_seed: str
):
    resp = await client.get("/api/widgets/explore")
    assert resp.status_code == 200
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    assert builtin_seed in widgets
    bw = widgets[builtin_seed]
    assert bw["kind"] == "builtin"
    assert bw["visibility"] == "public_view_only"
    # i18n 解析
    assert "en" in bw["title_i18n"]
    assert "zh-Hans" in bw["title_i18n"]


# ─────────────────────────────────────────────────────────────────────
# Section I — Remix chain（A→B→C 血缘 + 各自计数）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remix_chain_lineage_independent(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    """A 公开 → B remix 出 B'，B 把 B' 也公开 → C remix B' 出 C'。
    断言：
    - B'.original_widget_id == A.widget_id
    - C'.original_widget_id == B'.widget_id（不是 A —— 血缘只记一层 source）
    - A.remix_count == 1（B 来过一次）
    - B'.remix_count == 1（C 来过一次）
    - C'.remix_count == 0
    - 三者 widget_id 互不相同，目录互不相同
    """
    a_id = alice_widget_with_dir["widget_id"]

    # B remix A → B'
    r1 = await client.post(
        f"/api/widgets/{a_id}/remix",
        headers=auth_header(bob["access_token"]),
    )
    assert r1.status_code == 200
    b_prime_id = r1.json()["widget_id"]

    # B 把 B' 设为 public_remixable（让 C 能 remix）
    r2 = await client.patch(
        f"/api/widgets/{b_prime_id}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(bob["access_token"]),
    )
    assert r2.status_code == 200

    # C 注册 + remix B'
    carol = await register_and_login(
        client, email="carol@example.com", device_id="dev-c"
    )
    r3 = await client.post(
        f"/api/widgets/{b_prime_id}/remix",
        headers=auth_header(carol["access_token"]),
    )
    assert r3.status_code == 200
    c_prime_id = r3.json()["widget_id"]

    # 三个 id 互不相同
    assert len({a_id, b_prime_id, c_prime_id}) == 3

    # Refresh DB
    await db_session.commit()
    rows = (
        await db_session.execute(
            select(AIWidget).where(
                AIWidget.widget_id.in_([a_id, b_prime_id, c_prime_id])
            )
        )
    ).scalars().all()
    by_id = {r.widget_id: r for r in rows}
    for r in rows:
        await db_session.refresh(r)

    # 血缘
    assert by_id[b_prime_id].original_widget_id == a_id
    assert by_id[c_prime_id].original_widget_id == b_prime_id

    # remix_count
    assert by_id[a_id].remix_count == 1
    assert by_id[b_prime_id].remix_count == 1
    assert by_id[c_prime_id].remix_count == 0


# ─────────────────────────────────────────────────────────────────────
# Section J — 删除组件 / 删除用户的级联清理
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_widget_removes_dir_and_zip(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict,
):
    """Delete 组件 → 文件目录 + zip 都被擦。"""
    from robot.widgets import service as widget_service

    wid = alice_widget_with_dir["widget_id"]
    src_dir = widget_service.WIDGETS_DIR / wid
    src_zip = widget_service.WIDGETS_DIR / f"{wid}.zip"
    # 模拟有一个 zip
    src_zip.write_bytes(b"fake zip")
    assert src_dir.is_dir()
    assert src_zip.exists()

    resp = await client.delete(
        f"/api/widgets/{wid}",
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    assert resp.status_code == 200

    assert not src_dir.is_dir()
    assert not src_zip.exists()


@pytest.mark.asyncio
async def test_delete_widget_clears_view_tables(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    """Delete → user_widget_view + explore_widget_view + widget_likes 中的相关行
    都消失（不依赖 SQLite FK CASCADE，直接验证）。"""
    wid = alice_widget_with_dir["widget_id"]
    bob_h = auth_header(bob["access_token"])

    # Bob 点赞 + 收藏 → 在 widget_likes 和 user_widget_view 都有行
    await client.post(f"/api/widgets/{wid}/like", headers=bob_h)
    await client.post(f"/api/widgets/{wid}/favorite", headers=bob_h)

    # Alice 删
    resp = await client.delete(
        f"/api/widgets/{wid}",
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    assert resp.status_code == 200

    # Force refresh from DB
    await db_session.commit()

    # 三张表中相关行应当不再存在（SQLAlchemy ORM cascade 处理 ai_widgets 子表）
    explore_rows = (
        await db_session.execute(
            select(ExploreWidgetView).where(
                ExploreWidgetView.widget_id == wid
            )
        )
    ).scalars().all()
    view_rows = (
        await db_session.execute(
            select(UserWidgetView).where(
                UserWidgetView.widget_id == wid
            )
        )
    ).scalars().all()
    like_rows = (
        await db_session.execute(
            select(WidgetLike).where(WidgetLike.widget_id == wid)
        )
    ).scalars().all()

    assert explore_rows == [], "explore_widget_view 残留"
    assert view_rows == [], "user_widget_view 残留 — 会让 mine 端点出现幽灵行"
    assert like_rows == [], "widget_likes 残留"


@pytest.mark.asyncio
async def test_delete_widget_rows_for_owner(
    db_session: AsyncSession, client: AsyncClient, alice_widget: dict
):
    """注销账号场景：service.delete_widget_rows_for_owner 删掉用户所有 widget。"""
    from robot.widgets import service as widget_service

    deleted_ids = await widget_service.delete_widget_rows_for_owner(
        alice_widget["user"]["id"], db=db_session,
    )
    assert alice_widget["widget_id"] in deleted_ids

    # widget 行没了
    rows = (
        await db_session.execute(
            select(AIWidget).where(
                AIWidget.widget_id == alice_widget["widget_id"]
            )
        )
    ).scalars().all()
    assert rows == []


# ─────────────────────────────────────────────────────────────────────
# Section K — Multi-language（Accept-Language 渲染）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explore_renders_accept_language_zh(
    client: AsyncClient, builtin_seed: str
):
    """Accept-Language: zh-Hans 时 builtin 的 title 是中文。"""
    resp = await client.get(
        "/api/widgets/explore",
        headers={"Accept-Language": "zh-Hans"},
    )
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    bw = widgets[builtin_seed]
    # title 字段是 localized 后的字符串，应该是中文
    assert bw["title"] == "时间", f"got: {bw['title']}"


@pytest.mark.asyncio
async def test_explore_renders_accept_language_en(
    client: AsyncClient, builtin_seed: str
):
    resp = await client.get(
        "/api/widgets/explore",
        headers={"Accept-Language": "en"},
    )
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    bw = widgets[builtin_seed]
    assert bw["title"] == "Time"


# ─────────────────────────────────────────────────────────────────────
# Section L — Download endpoint 与 /files 一致的鉴权
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_others_private_returns_404(
    client: AsyncClient, alice_widget: dict, bob: dict
):
    """Bob 不能 download Alice 的 private widget zip。"""
    resp = await client.get(
        f"/api/widgets/{alice_widget['widget_id']}/download",
        headers=auth_header(bob["access_token"]),
    )
    # 既不暴露存在 (404)，也不是 403
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_owner_private_works(
    client: AsyncClient, alice_widget_with_dir: dict
):
    """Alice 即使把组件设回 private 也能 download 自己的 zip。"""
    wid = alice_widget_with_dir["widget_id"]
    await client.patch(
        f"/api/widgets/{wid}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    resp = await client.get(
        f"/api/widgets/{wid}/download",
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


@pytest.mark.asyncio
async def test_download_builtin_returns_404(
    client: AsyncClient, builtin_seed: str
):
    """Builtin 没有 zip — download 返 404。"""
    resp = await client.get(f"/api/widgets/{builtin_seed}/download")
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────
# Section M — 空用户 / 匿名用户场景
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explore_anonymous_user_works(
    client: AsyncClient, builtin_seed: str
):
    """匿名用户调 explore 应该 200，返回 builtin（liked_by_me 全 false）。"""
    resp = await client.get("/api/widgets/explore")
    assert resp.status_code == 200
    widgets = {w["widget_id"]: w for w in resp.json()["widgets"]}
    bw = widgets[builtin_seed]
    assert bw["liked_by_me"] is False
    assert bw["favorited_by_me"] is False
    assert bw["is_owner"] is False


@pytest.mark.asyncio
async def test_mine_brand_new_user_returns_empty(client: AsyncClient):
    """新注册用户 mine 应该 200 + empty array，不是 404。"""
    auth = await register_and_login(
        client, email="fresh@example.com", device_id="dev-fresh"
    )
    resp = await client.get(
        "/api/widgets/mine", headers=auth_header(auth["access_token"])
    )
    assert resp.status_code == 200
    assert resp.json()["widgets"] == []


# ─────────────────────────────────────────────────────────────────────
# Section N — Cache invalidation 完整矩阵
# ─────────────────────────────────────────────────────────────────────


def _install_cache_spies(monkeypatch) -> dict:
    """Helper: 拦截 redis.delete / setex / get，返回记录列表 dict。"""
    log = {"deleted": [], "setex": []}

    async def fake_delete(*keys):
        for k in keys:
            log["deleted"].append(k)
        return len(keys)

    async def fake_setex(key, ttl, value):
        log["setex"].append(key)
        return True

    async def fake_get(key):
        return None

    from robot.widgets import service as widget_service
    monkeypatch.setattr(widget_service.redis_client, "delete", fake_delete)
    monkeypatch.setattr(widget_service.redis_client, "setex", fake_setex)
    monkeypatch.setattr(widget_service.redis_client, "get", fake_get)
    return log


@pytest.mark.asyncio
async def test_cache_invalidate_on_like(
    monkeypatch, client: AsyncClient, alice_public_widget: dict, bob: dict,
):
    log = _install_cache_spies(monkeypatch)
    await client.post(
        f"/api/widgets/{alice_public_widget['widget_id']}/like",
        headers=auth_header(bob["access_token"]),
    )
    assert any("widgets:explore" in k for k in log["deleted"])
    assert any(
        f"widgets:meta:{alice_public_widget['widget_id']}" in k
        for k in log["deleted"]
    )


@pytest.mark.asyncio
async def test_cache_invalidate_on_favorite(
    monkeypatch, client: AsyncClient, alice_public_widget: dict, bob: dict,
):
    log = _install_cache_spies(monkeypatch)
    await client.post(
        f"/api/widgets/{alice_public_widget['widget_id']}/favorite",
        headers=auth_header(bob["access_token"]),
    )
    # Bob mine 缓存必须 invalidate
    assert any(
        f"widgets:mine:{bob['user']['id']}" in k for k in log["deleted"]
    )
    assert any("widgets:explore" in k for k in log["deleted"])


@pytest.mark.asyncio
async def test_cache_invalidate_on_remix(
    monkeypatch, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    log = _install_cache_spies(monkeypatch)
    await client.post(
        f"/api/widgets/{alice_widget_with_dir['widget_id']}/remix",
        headers=auth_header(bob["access_token"]),
    )
    # source 的 meta 必须 invalidate（remix_count 变了）
    assert any(
        f"widgets:meta:{alice_widget_with_dir['widget_id']}" in k
        for k in log["deleted"]
    )
    # Bob mine 缓存必须 invalidate（多了 remix 出来的副本）
    assert any(
        f"widgets:mine:{bob['user']['id']}" in k for k in log["deleted"]
    )


@pytest.mark.asyncio
async def test_cache_invalidate_on_delete(
    monkeypatch, client: AsyncClient, alice_widget_with_dir: dict,
):
    log = _install_cache_spies(monkeypatch)
    await client.delete(
        f"/api/widgets/{alice_widget_with_dir['widget_id']}",
        headers=auth_header(alice_widget_with_dir["access_token"]),
    )
    assert any(
        f"widgets:meta:{alice_widget_with_dir['widget_id']}" in k
        for k in log["deleted"]
    )
    assert any("widgets:explore" in k for k in log["deleted"])


# ─────────────────────────────────────────────────────────────────────
# Section O — Legacy GET /api/widgets 兼容
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_widgets_endpoint_returns_user_widgets_and_seeds(
    client: AsyncClient, alice_widget: dict, builtin_seed: str,
):
    """旧 iOS（升级前）调 /api/widgets 仍能正常工作 — 返 widgets + seeds 两个 key。"""
    resp = await client.get(
        "/api/widgets", headers=auth_header(alice_widget["access_token"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "widgets" in body
    assert "seeds" in body
    # 我自己创建的应该在 widgets 里
    assert alice_widget["widget_id"] in [
        w["widget_id"] for w in body["widgets"]
    ]
    # builtin 应该在 seeds 里
    assert builtin_seed in [s["widget_id"] for s in body["seeds"]]


# ─────────────────────────────────────────────────────────────────────
# Section I — 边界 / 并发 / 级联 / 链式 / 缓存命中
# ─────────────────────────────────────────────────────────────────────
#
# 这一节是上一轮 ad-hoc 写测试时漏的"看起来不重要直到出 bug"场景：
# - DELETE 级联：删除 widget / 删除 user 时，view 表 + likes / favorites
#   是否完全清干净？iOS Mine Tab 不能因为别人 DB 行没了就看到孤儿。
# - 链式 remix：A → B → C → D，每一层 lineage 是否正确，原作 remix_count
#   是否只 +1（不传播）。
# - 并发 toggle：同一用户同一 widget 在 0.5s 内连点 5 次，最终态稳定？
# - 缓存命中：第二次调 explore 不应再走 SQL JOIN。
# - 分页边界：250 行公开 widget，limit=200 是否截断且稳定排序。


@pytest.mark.asyncio
async def test_delete_widget_cascades_view_tables(
    db_session: AsyncSession, client: AsyncClient,
    alice_public_widget: dict, bob: dict,
):
    """删除 widget → user_widget_view + explore_widget_view + widget_likes
    + widget_favorites 全部该删的删干净。FK CASCADE 在 SQLite 默认不强制，
    DELETE 端点必须自己显式清，或依赖 FK CASCADE 已被 enable。

    我们假设端点的 `await db.delete(row)` + sqlalchemy ORM 关系会触发级联；
    若发现 SQLite 没强制 FK，本测试会暴露问题。
    """
    wid = alice_public_widget["widget_id"]
    bob_headers = auth_header(bob["access_token"])
    alice_headers = auth_header(alice_public_widget["access_token"])

    # 制造完整状态：bob 收藏 + 点赞，alice 是作者（user_widget_view created）
    await client.post(f"/api/widgets/{wid}/favorite", headers=bob_headers)
    await client.post(f"/api/widgets/{wid}/like", headers=bob_headers)

    # 验证 pre-condition
    from robot.db.models import WidgetFavorite
    pre_views = (
        await db_session.execute(
            select(UserWidgetView).where(UserWidgetView.widget_id == wid)
        )
    ).scalars().all()
    pre_likes = (
        await db_session.execute(
            select(WidgetLike).where(WidgetLike.widget_id == wid)
        )
    ).scalars().all()
    pre_favs = (
        await db_session.execute(
            select(WidgetFavorite).where(WidgetFavorite.widget_id == wid)
        )
    ).scalars().all()
    pre_explore = (
        await db_session.execute(
            select(ExploreWidgetView).where(ExploreWidgetView.widget_id == wid)
        )
    ).scalars().all()
    assert len(pre_views) == 1  # alice created (favorited 现在不在这表)
    assert len(pre_favs) == 1   # bob favorited
    assert len(pre_likes) == 1
    assert len(pre_explore) == 1

    # Act：alice 删除自己的 widget
    resp = await client.delete(f"/api/widgets/{wid}", headers=alice_headers)
    assert resp.status_code == 200, resp.text

    # 关键断言：所有相关索引行都没了
    post_widget = (
        await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == wid)
        )
    ).scalar_one_or_none()
    assert post_widget is None

    post_views = (
        await db_session.execute(
            select(UserWidgetView).where(UserWidgetView.widget_id == wid)
        )
    ).scalars().all()
    post_likes = (
        await db_session.execute(
            select(WidgetLike).where(WidgetLike.widget_id == wid)
        )
    ).scalars().all()
    post_favs = (
        await db_session.execute(
            select(WidgetFavorite).where(WidgetFavorite.widget_id == wid)
        )
    ).scalars().all()
    post_explore = (
        await db_session.execute(
            select(ExploreWidgetView).where(ExploreWidgetView.widget_id == wid)
        )
    ).scalars().all()
    assert post_views == []
    assert post_likes == []
    assert post_favs == []
    assert post_explore == []


@pytest.mark.asyncio
async def test_remix_chain_lineage_and_isolated_counts(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    """A → B → C 三层 remix。验证每一层的 original_widget_id 指向**直接**
    上游而非链顶；上游 remix_count 只 +1（不级联）。"""
    A = alice_widget_with_dir["widget_id"]
    bob_headers = auth_header(bob["access_token"])
    alice_headers = auth_header(alice_widget_with_dir["access_token"])

    # B = bob remix A
    r1 = await client.post(f"/api/widgets/{A}/remix", headers=bob_headers)
    assert r1.status_code == 200
    B = r1.json()["widget_id"]

    # B 必须 visibility=public_remixable 才能被再次 remix
    await client.patch(
        f"/api/widgets/{B}/visibility",
        json={"visibility": "public_remixable"},
        headers=bob_headers,
    )

    # C = alice remix B
    r2 = await client.post(f"/api/widgets/{B}/remix", headers=alice_headers)
    assert r2.status_code == 200, r2.text
    C = r2.json()["widget_id"]

    # 取最新行
    rows = {
        wid: (await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == wid)
        )).scalar_one()
        for wid in [A, B, C]
    }
    for r in rows.values():
        await db_session.refresh(r)

    # original_widget_id 链：C → B → A → None
    assert rows[A].original_widget_id is None
    assert rows[B].original_widget_id == A
    assert rows[C].original_widget_id == B

    # remix_count 不级联：每个上游只 +1（不会因为孙子被 remix 也 +1）
    assert rows[A].remix_count == 1, "A 只被 B remix 过，不应该因 C 而 +2"
    assert rows[B].remix_count == 1
    assert rows[C].remix_count == 0


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_data_skip_db(
    monkeypatch, db_session: AsyncSession, client: AsyncClient,
    alice_public_widget: dict, bob: dict,
):
    """缓存命中时不应再 JOIN ai_games × explore_widget_view。

    用 monkeypatch 给 redis.get 返回预制 JSON，断言端点直接返回这份数据
    而不是从 DB 查（DB 里我们偷偷把行删掉，看端点是否还能"返回"该 widget）。
    """
    from robot.widgets import service as widget_service

    fake_payload = [{
        "widget_id": "aw_cache_only",
        "owner_id": "u1",
        "title_i18n_raw": json.dumps({"en": "Cached", "zh-Hans": "缓存"}),
        "description_i18n_raw": json.dumps({"en": "", "zh-Hans": ""}),
        "supported_sizes_raw": json.dumps(["small"]),
        "base_template_id": "",
        "visibility": "public_remixable",
        "kind": "ai",
        "original_widget_id": None,
        "like_count": 99,
        "favorite_count": 0,
        "remix_count": 0,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }]

    async def fake_get(key):
        if "widgets:explore" in key:
            return json.dumps(fake_payload)
        return None

    async def fake_setex(key, ttl, val):
        return True

    monkeypatch.setattr(widget_service.redis_client, "get", fake_get)
    monkeypatch.setattr(widget_service.redis_client, "setex", fake_setex)

    resp = await client.get(
        "/api/widgets/explore", headers=auth_header(bob["access_token"])
    )
    assert resp.status_code == 200
    ids = [w["widget_id"] for w in resp.json()["widgets"]]
    # 缓存里有但 DB 里不存在的 ID 出现在响应里 → 证明端点读了缓存
    assert "aw_cache_only" in ids
    # 真正 DB 里的 alice_public_widget 反而**没出现**（说明 DB 没被 JOIN）
    assert alice_public_widget["widget_id"] not in ids


@pytest.mark.asyncio
async def test_explore_pagination_limit_200(
    db_session: AsyncSession, client: AsyncClient, bob: dict,
):
    """造 250 个 public 行，调 /explore 应正好返回 200，且按 added_at desc 稳定排序。"""
    now = datetime.utcnow()
    for i in range(250):
        ts = now.replace(microsecond=i)  # 让排序键各不相同
        widget_id = f"aw_pagi_{i:03d}"
        db_session.add(AIWidget(
            widget_id=widget_id,
            owner_id=None,  # 便于和 user 解耦
            title_i18n=json.dumps({"en": f"P{i}", "zh-Hans": f"分{i}"}),
            description_i18n=json.dumps({"en": "", "zh-Hans": ""}),
            supported_sizes=json.dumps(["small"]),
            base_template_id="",
            visibility="public_view_only",
            kind="builtin",  # builtin 没有 owner，简化
            original_widget_id=None,
            like_count=0, favorite_count=0, remix_count=0,
            created_at=ts, updated_at=ts,
        ))
        db_session.add(ExploreWidgetView(
            widget_id=widget_id, sort_score=0, added_at=ts
        ))
    await db_session.commit()

    resp = await client.get(
        "/api/widgets/explore", headers=auth_header(bob["access_token"])
    )
    assert resp.status_code == 200
    widgets = resp.json()["widgets"]
    assert len(widgets) == 200, f"limit=200 应该截断，但拿到 {len(widgets)}"

    # 排序断言：按 added_at desc → 最大 i 在前
    ids = [w["widget_id"] for w in widgets]
    nums = [int(wid.split("_")[-1]) for wid in ids if wid.startswith("aw_pagi_")]
    assert nums == sorted(nums, reverse=True), "排序应该是 added_at desc"


@pytest.mark.asyncio
async def test_user_widget_view_only_one_type_per_pair(
    db_session: AsyncSession, client: AsyncClient,
    alice_public_widget: dict,
):
    """同一 (user_id, widget_id) 复合 PK 保证只能存在一种 view_type。

    业务上：作者不能再 favorite 自己（端点已拒）；
    Schema 上：复合 PK 也防止误插两行。
    """
    from sqlalchemy.exc import IntegrityError

    alice_id = alice_public_widget["user"]["id"]
    wid = alice_public_widget["widget_id"]

    # Alice 已经有 view_type='created'（fixture 创建）
    existing = (
        await db_session.execute(
            select(UserWidgetView).where(
                UserWidgetView.user_id == alice_id,
                UserWidgetView.widget_id == wid,
            )
        )
    ).scalar_one()
    assert existing.view_type == "created"

    # 试图再插入 favorited 行 — 应该 IntegrityError（PK 冲突）
    db_session.add(UserWidgetView(
        user_id=alice_id, widget_id=wid,
        view_type="favorited", added_at=datetime.utcnow(),
    ))
    try:
        await db_session.commit()
        await db_session.rollback()
        # 走到这里说明没抛 — 失败
        assert False, "复合 PK 冲突应抛 IntegrityError"
    except IntegrityError:
        await db_session.rollback()


@pytest.mark.asyncio
async def test_visibility_change_invalidates_cached_explore_list(
    monkeypatch, client: AsyncClient, alice_widget: dict
):
    """改 visibility 必须 DEL widgets:explore，否则其他用户在 60s 缓存 TTL
    内看不到新公开的 widget（或仍能看到下架的）。"""
    from robot.widgets import service as widget_service

    deleted = []

    async def fake_delete(*keys):
        deleted.extend(keys)
        return len(keys)

    async def fake_setex(*a, **kw):
        return True

    async def fake_get(key):
        return None

    monkeypatch.setattr(widget_service.redis_client, "delete", fake_delete)
    monkeypatch.setattr(widget_service.redis_client, "setex", fake_setex)
    monkeypatch.setattr(widget_service.redis_client, "get", fake_get)

    # private → public_remixable
    await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(alice_widget["access_token"]),
    )
    assert any("widgets:explore" in k for k in deleted), \
        "visibility 切换必须失效全局 explore key"

    # public_remixable → private（再切回去）
    deleted.clear()
    await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/visibility",
        json={"visibility": "private"},
        headers=auth_header(alice_widget["access_token"]),
    )
    assert any("widgets:explore" in k for k in deleted), \
        "下架时也必须失效 explore key"


@pytest.mark.asyncio
async def test_like_then_favorite_then_remix_lifecycle(
    db_session: AsyncSession, client: AsyncClient,
    alice_widget_with_dir: dict, bob: dict,
):
    """端到端用户旅程：B 看到 A 的公开 widget → 点赞 → 收藏 → 二创副本 →
    副本 visibility 改 public → 自己 unlike 原作。每步检查 DB 一致性。"""
    src_id = alice_widget_with_dir["widget_id"]
    bob_headers = auth_header(bob["access_token"])
    bob_id = bob["user"]["id"]

    # 1. 点赞
    r1 = await client.post(f"/api/widgets/{src_id}/like", headers=bob_headers)
    assert r1.json()["like_count"] == 1

    # 2. 收藏
    r2 = await client.post(f"/api/widgets/{src_id}/favorite", headers=bob_headers)
    assert r2.json()["favorite_count"] == 1

    # 3. Mine Tab 应该有 src_id（view_type='favorited'）
    mine = await client.get("/api/widgets/mine", headers=bob_headers)
    mine_map = {w["widget_id"]: w for w in mine.json()["widgets"]}
    assert mine_map[src_id]["view_type"] == "favorited"

    # 4. Remix 出 new_id
    r3 = await client.post(f"/api/widgets/{src_id}/remix", headers=bob_headers)
    new_id = r3.json()["widget_id"]

    # Mine 应该新增 new_id（view_type='created'），src_id 仍然 favorited
    mine2 = await client.get("/api/widgets/mine", headers=bob_headers)
    mine2_map = {w["widget_id"]: w for w in mine2.json()["widgets"]}
    assert mine2_map[new_id]["view_type"] == "created"
    assert mine2_map[src_id]["view_type"] == "favorited"

    # 5. unlike 原作
    r5 = await client.post(f"/api/widgets/{src_id}/like", headers=bob_headers)
    assert r5.json()["like_count"] == 0
    assert r5.json()["liked_by_me"] is False

    # 6. unfavorite 原作（Mine 不再有 src_id，但 new_id 还在）
    r6 = await client.post(
        f"/api/widgets/{src_id}/favorite", headers=bob_headers
    )
    assert r6.json()["favorited_by_me"] is False
    mine3 = await client.get("/api/widgets/mine", headers=bob_headers)
    mine3_ids = [w["widget_id"] for w in mine3.json()["widgets"]]
    assert src_id not in mine3_ids
    assert new_id in mine3_ids


@pytest.mark.asyncio
async def test_explore_excludes_widgets_without_view_index(
    db_session: AsyncSession, client: AsyncClient, alice_widget: dict
):
    """防御性测试：手工把 visibility 直接改成 public_view_only 但**不**
    更新 explore_widget_view —— 模拟数据漂移。/explore 端点不应"猜"
    visibility 列，只认 explore_widget_view 索引。

    这条保证我们的"事件驱动维护两张表"约束被强制执行：宁愿丢一行也不
    脏读。
    """
    # 直接 ORM mutation 不走端点
    row = (
        await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == alice_widget["widget_id"])
        )
    ).scalar_one()
    row.visibility = "public_view_only"
    await db_session.commit()

    resp = await client.get("/api/widgets/explore")
    ids = [w["widget_id"] for w in resp.json()["widgets"]]
    assert alice_widget["widget_id"] not in ids, \
        "没有 explore_widget_view 索引行的 widget 不应出现在 explore"
