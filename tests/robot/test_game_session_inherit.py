"""Session inheritance tests — clone_game / _run_claude_agent 会话继承.

覆盖：
1. `_session_paths` helpers 的 slug 编码 / 路径推导是否正确；
2. `clone_game` 在源带 per-game session 时把 jsonl 改名搬运 + meta.session_id 继承；
3. `clone_game` 在源带 sid 但没 per-game project dir（legacy）时降级到 None；
4. `clone_game` 在源 session_id 为 None 时彻底跳过继承逻辑；
5. `_run_claude_agent` 的旧游戏 lazy 迁移块：global jsonl 存在 + per-game 缺 → 拷贝；per-game 已存在 → 不覆盖。

不调真实 LLM，纯 fs / DB 验证。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import AIGame
from tests.conftest import auth_header, register_and_login


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


# ─────────────────────────────────────────────────────────────────────
# Section A — _session_paths helpers (pure unit, 无 DB / IO)
# ─────────────────────────────────────────────────────────────────────


def test_cwd_to_project_slug_basic():
    from robot.games._session_paths import cwd_to_project_slug

    assert (
        cwd_to_project_slug("/home/developer/games/ai_123_abc")
        == "-Users-x-games-ai-123-abc"
    )


def test_cwd_to_project_slug_preserves_alphanumeric():
    from robot.games._session_paths import cwd_to_project_slug

    # 大小写、数字保留
    assert cwd_to_project_slug("/AbC/123/Def") == "-AbC-123-Def"


def test_cwd_to_project_slug_consecutive_separators():
    from robot.games._session_paths import cwd_to_project_slug

    # `/.` 之类连续非字母数字字符应各自变成 '-'，不合并
    assert cwd_to_project_slug("/a/.b/c") == "-a--b-c"


def test_per_game_session_jsonl_layout(tmp_path: Path):
    from robot.games._session_paths import (
        cwd_to_project_slug,
        per_game_session_jsonl,
        per_game_config_dir,
    )

    game_dir = tmp_path / "games" / "ai_999_xyz"
    sid = "abc-123"
    p = per_game_session_jsonl(game_dir, sid)
    # 关键不变式：.claude 在 game_dir **外**（sibling），不能在内（避免 zip 泄漏）
    expected = (
        game_dir.parent / "ai_999_xyz.claude" / "projects"
        / cwd_to_project_slug(game_dir) / f"{sid}.jsonl"
    )
    assert p == expected
    assert per_game_config_dir(game_dir) == game_dir.parent / "ai_999_xyz.claude"
    # 双保险：jsonl 路径绝对不能包含 game_dir 自身作为前缀
    assert not str(p).startswith(str(game_dir) + "/"), (
        ".claude 必须是 game_dir 的 sibling，不能在 game_dir 里"
    )


def test_global_session_jsonl_uses_user_home(tmp_path: Path):
    from robot.games._session_paths import global_session_jsonl

    game_dir = "/home/developer/games/ai_42_q"
    p = global_session_jsonl(game_dir, "sid-7")
    assert ".claude/projects/" in str(p)
    assert p.name == "sid-7.jsonl"
    # path 必须是 user home 下的，不是相对路径
    assert str(p).startswith(str(Path.home()))


# ─────────────────────────────────────────────────────────────────────
# Section B — clone_game 会话继承（用 marketplace fixture 复用模式）
# ─────────────────────────────────────────────────────────────────────


def _seed_per_game_session(game_dir: Path, session_id: str, payload: str = '{"k":"v"}\n') -> Path:
    """在 game_dir **sibling** `.claude/` 造一个 per-game session jsonl，
    模拟原作者跑过 Claude Code。"""
    from robot.games._session_paths import per_game_session_jsonl

    jsonl = per_game_session_jsonl(game_dir, session_id)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(payload, encoding="utf-8")
    return jsonl


@pytest_asyncio.fixture
async def alice_game_with_session(
    db_session: AsyncSession, client: AsyncClient
) -> dict:
    """Alice 拥有 + public_remixable 的游戏，盘上带 per-game session jsonl + DB 有 sid。"""
    from robot.games import service as game_service

    auth = await register_and_login(
        client, email="alice_sess@example.com", device_id="dev-a-sess"
    )
    game_id = "ai_test_alice_sess_001"
    sid = "alice-session-uuid-1"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=auth["user"]["id"],
        title="Alice's Session Game",
        title_i18n=json.dumps({"en": "Alice's Session Game", "zh-Hans": "爱丽丝会话游戏"}),
        description="by alice",
        description_i18n=json.dumps({"en": "by alice", "zh-Hans": "爱丽丝制作"}),
        icon="🎮",
        input_mode="body_pose",
        kind="ai",
        visibility="public_remixable",
        original_game_id=None,
        template_from="",
        session_id=sid,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    from robot.db.models import ExploreGameView, UserGameView
    db_session.add(UserGameView(
        user_id=auth["user"]["id"], game_id=game_id,
        view_type="created", added_at=now,
    ))
    db_session.add(ExploreGameView(game_id=game_id, added_at=now))
    await db_session.commit()

    # 造盘上目录 + meta.json + per-game session jsonl
    src = game_service.GAMES_DIR / game_id
    src.mkdir(parents=True, exist_ok=True)
    (src / "project.godot").write_text(
        '[application]\nconfig/name="Alice"\n', encoding="utf-8"
    )
    (src / "scenes").mkdir(exist_ok=True)
    (src / "scenes" / "main.gd").write_text(
        "extends Node2D\nfunc _ready(): pass\n", encoding="utf-8"
    )
    (src / "meta.json").write_text(
        json.dumps({
            "game_id": game_id,
            "title": "Alice's Session Game",
            "input_mode": "body_pose",
            "session_id": sid,
        }),
        encoding="utf-8",
    )
    seeded_jsonl = _seed_per_game_session(src, sid, payload='{"turn":1}\n')

    yield {**auth, "game_id": game_id, "session_id": sid, "src_dir": src,
           "seeded_jsonl": seeded_jsonl}

    # 清理 — 删 src + 所有 remix 出去的副本（含 zip + sibling .claude）
    from robot.games._session_paths import wipe_game_artifacts
    wipe_game_artifacts(src)
    rows = (
        await db_session.execute(
            select(AIGame).where(AIGame.original_game_id == game_id)
        )
    ).scalars().all()
    for r in rows:
        wipe_game_artifacts(game_service.GAMES_DIR / r.game_id)


@pytest_asyncio.fixture
async def bob_for_remix(client: AsyncClient) -> dict:
    return await register_and_login(
        client, email="bob_sess@example.com", device_id="dev-b-sess"
    )


@pytest.mark.asyncio
async def test_clone_inherits_session_when_source_has_jsonl(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_session: dict, bob_for_remix: dict,
):
    """源带 sid + sibling .claude project dir → 副本继承 sid + 内部 project dir 改名到新 cwd slug。"""
    from robot.games import service as game_service
    from robot.games._session_paths import (
        cwd_to_project_slug,
        per_game_config_dir,
        per_game_session_jsonl,
    )

    src_id = alice_game_with_session["game_id"]
    src_dir = alice_game_with_session["src_dir"]
    sid = alice_game_with_session["session_id"]

    resp = await client.post(
        f"/api/games/{src_id}/remix",
        headers=auth_header(bob_for_remix["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["game_id"]
    new_dir = game_service.GAMES_DIR / new_id

    # 1. 副本 meta.session_id 继承
    new_meta = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
    assert new_meta["session_id"] == sid

    # 2. DB 行 session_id 也继承（不再是 None）
    new_row = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == new_id))
    ).scalar_one()
    assert new_row.session_id == sid

    # 3. sibling .claude 内 project dir 已经从 src slug 改名到 dst slug
    new_claude = per_game_config_dir(new_dir)
    assert new_claude.is_dir()
    assert per_game_session_jsonl(new_dir, sid).is_file()
    assert not (new_claude / "projects" / cwd_to_project_slug(src_dir)).exists()

    # 4. 源目录 jsonl 不被污染（copy + rename 只动副本侧）
    assert alice_game_with_session["seeded_jsonl"].is_file()


@pytest.mark.asyncio
async def test_clone_falls_back_when_source_has_sid_but_no_project_dir(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_session: dict, bob_for_remix: dict,
):
    """源带 sid 但 sibling .claude 不存在（legacy 全局存储）→ 副本 sid=None，无 crash。"""
    from robot.games import service as game_service
    from robot.games._session_paths import per_game_config_dir

    # 把源 sibling .claude 删掉，模拟 legacy（session 在全局 ~/.claude/projects/）
    shutil.rmtree(
        per_game_config_dir(alice_game_with_session["src_dir"]),
        ignore_errors=True,
    )

    src_id = alice_game_with_session["game_id"]
    resp = await client.post(
        f"/api/games/{src_id}/remix",
        headers=auth_header(bob_for_remix["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["game_id"]
    new_dir = game_service.GAMES_DIR / new_id

    new_meta = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
    assert new_meta["session_id"] is None

    new_row = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == new_id))
    ).scalar_one()
    assert new_row.session_id is None


@pytest_asyncio.fixture
async def alice_game_no_session(
    db_session: AsyncSession, client: AsyncClient,
) -> dict:
    """Alice 拥有 + public_remixable + session_id=None 的游戏（首次 remix 路径）。"""
    from robot.games import service as game_service

    auth = await register_and_login(
        client, email="alice_nosess@example.com", device_id="dev-a-nosess"
    )
    game_id = "ai_test_alice_nosess_001"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=auth["user"]["id"],
        title="Alice No Session",
        title_i18n=json.dumps({"en": "Alice No Session", "zh-Hans": "爱丽丝无会话"}),
        description="x",
        description_i18n=json.dumps({"en": "x", "zh-Hans": "x"}),
        icon="🎮",
        input_mode="touch",
        kind="ai",
        visibility="public_remixable",
        original_game_id=None,
        template_from="",
        session_id=None,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    from robot.db.models import ExploreGameView, UserGameView
    db_session.add(UserGameView(
        user_id=auth["user"]["id"], game_id=game_id,
        view_type="created", added_at=now,
    ))
    db_session.add(ExploreGameView(game_id=game_id, added_at=now))
    await db_session.commit()

    src = game_service.GAMES_DIR / game_id
    src.mkdir(parents=True, exist_ok=True)
    (src / "project.godot").write_text("[application]\n", encoding="utf-8")
    (src / "scenes").mkdir(exist_ok=True)
    (src / "scenes" / "main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (src / "meta.json").write_text(
        json.dumps({"game_id": game_id, "input_mode": "touch"}),
        encoding="utf-8",
    )

    yield {**auth, "game_id": game_id, "src_dir": src}

    from robot.games._session_paths import wipe_game_artifacts
    wipe_game_artifacts(src)
    rows = (
        await db_session.execute(
            select(AIGame).where(AIGame.original_game_id == game_id)
        )
    ).scalars().all()
    for r in rows:
        wipe_game_artifacts(game_service.GAMES_DIR / r.game_id)


@pytest.mark.asyncio
async def test_clone_skips_inheritance_when_source_session_is_none(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_no_session: dict, bob_for_remix: dict,
):
    """源 session_id=None → 跳过整段继承逻辑，副本 sid=None。"""
    from robot.games import service as game_service

    src_id = alice_game_no_session["game_id"]
    resp = await client.post(
        f"/api/games/{src_id}/remix",
        headers=auth_header(bob_for_remix["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["game_id"]
    new_dir = game_service.GAMES_DIR / new_id

    new_meta = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
    assert new_meta["session_id"] is None

    # 副本不应该有任何 .claude 目录或 sibling .claude（源没有，clone 也不应造）
    from robot.games._session_paths import per_game_config_dir
    assert not (new_dir / ".claude").exists()
    assert not per_game_config_dir(new_dir).exists()

    new_row = (
        await db_session.execute(select(AIGame).where(AIGame.game_id == new_id))
    ).scalar_one()
    assert new_row.session_id is None


# ─────────────────────────────────────────────────────────────────────
# Section B-priv — 隐私不变式：.claude 不能进 game_dir / 不能进 zip
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_does_not_leak_into_game_dir_or_zip(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_session: dict, bob_for_remix: dict,
):
    """关键安全：game_dir 会被打包成 zip 发到 iOS。session jsonl（含原作者
    prompts / tool calls / thinking blocks）**绝对不能**进 game_dir，否则会随
    zip 泄漏给所有下载者。

    校验：
    1. 新副本的 game_dir 内**没有** `.claude/` 子目录
    2. 重新打 zip 后，zip 内容里没有任何 .claude 路径
    """
    import zipfile
    from robot.games import service as game_service
    from robot.games._session_paths import per_game_config_dir

    src_id = alice_game_with_session["game_id"]
    resp = await client.post(
        f"/api/games/{src_id}/remix",
        headers=auth_header(bob_for_remix["access_token"]),
    )
    new_id = resp.json()["game_id"]
    new_dir = game_service.GAMES_DIR / new_id

    # 1. game_dir 内绝对没 .claude/
    assert not (new_dir / ".claude").exists(), (
        ".claude **不能**在 game_dir 内（会随 zip 泄漏对话历史）"
    )

    # 2. session 必须落在 sibling 目录里
    assert per_game_config_dir(new_dir).is_dir()

    # 3. 重新打一份 zip（remix 路径已经打过；这里再打一遍模拟 modify 重打）
    zip_path = game_service.GAMES_DIR / f"{new_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(new_dir))

    # 4. zip 内不能含任何 .claude 路径
    with zipfile.ZipFile(zip_path) as zf:
        leaks = [n for n in zf.namelist() if ".claude" in n.lower()]
    assert not leaks, f"zip 泄漏了 session 路径: {leaks}"


# ─────────────────────────────────────────────────────────────────────
# Section C — _run_claude_agent legacy lazy migration
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# Section C-helper — inherit_session_from helper（共享给 clone_game + 模板继承）
# ─────────────────────────────────────────────────────────────────────


def test_inherit_session_with_expected_sid_succeeds(tmp_path: Path):
    """clone_game 用法：传源 sid，helper 校验该 jsonl 存在 + copy + rename。"""
    from robot.games._session_paths import (
        cwd_to_project_slug,
        inherit_session_from,
        per_game_session_jsonl,
    )

    src = tmp_path / "ai_src_001"
    src.mkdir()
    dst = tmp_path / "ai_dst_001"
    dst.mkdir()
    sid = "src-sid-9"

    # 在源 sibling 下种 jsonl（slug 用源 cwd 编码）
    src_jsonl = per_game_session_jsonl(src, sid)
    src_jsonl.parent.mkdir(parents=True, exist_ok=True)
    src_jsonl.write_text("turn data", encoding="utf-8")

    out = inherit_session_from(src, dst, expected_sid=sid)
    assert out == sid

    dst_jsonl = per_game_session_jsonl(dst, sid)
    assert dst_jsonl.is_file()
    assert dst_jsonl.read_text(encoding="utf-8") == "turn data"
    # 源 slug 子目录在 dst 内不应残留
    assert not (
        dst.parent / f"{dst.name}.claude" / "projects" / cwd_to_project_slug(src)
    ).exists()


def test_inherit_session_auto_discover_works_for_cross_machine_template(
    tmp_path: Path,
):
    """模板带 session 用法：dev 在 /home/dev/... 路径下构建模板，bundled jsonl
    的内部 slug 是 dev 的路径编码；prod 上 src_dir 是另一个绝对路径，slug 对不上。
    helper 应该 auto-discover（iterdir 找唯一一个 subdir）成功复制 + 改名。"""
    from robot.games._session_paths import (
        inherit_session_from,
        per_game_config_dir,
        per_game_session_jsonl,
    )

    src = tmp_path / "_templates" / "shooter"
    src.mkdir(parents=True)
    dst = tmp_path / "ai_user_001"
    dst.mkdir()

    # 手动造一个「dev 机器原始 slug」的 bundled session（不等于 src 的实际 slug）
    src_claude = per_game_config_dir(src)
    bogus_dev_slug = "-home-dev-some-other-path-shooter"
    bundled_proj = src_claude / "projects" / bogus_dev_slug
    bundled_proj.mkdir(parents=True)
    bundled_sid = "tmpl-sid-42"
    (bundled_proj / f"{bundled_sid}.jsonl").write_text(
        "design conversation", encoding="utf-8",
    )

    # 不传 expected_sid，让 helper 走 auto-discover 路径
    out = inherit_session_from(src, dst)
    assert out == bundled_sid, "helper 应能从任意 dev slug subdir 中提取 sid"

    # 复制后内部子目录已改名到 dst 实际 slug
    dst_jsonl = per_game_session_jsonl(dst, bundled_sid)
    assert dst_jsonl.is_file()
    assert dst_jsonl.read_text(encoding="utf-8") == "design conversation"


def test_inherit_session_returns_none_when_no_sibling(tmp_path: Path):
    """源没 sibling .claude → helper 返 None，dst 不应被创建任何 .claude。"""
    from robot.games._session_paths import (
        inherit_session_from,
        per_game_config_dir,
    )

    src = tmp_path / "src_legacy"
    src.mkdir()
    dst = tmp_path / "dst_target"
    dst.mkdir()

    assert inherit_session_from(src, dst) is None
    assert not per_game_config_dir(dst).exists()


def test_inherit_session_returns_none_when_expected_sid_missing(tmp_path: Path):
    """源有 sibling 但 expected_sid 对应的 jsonl 不存在 → 返 None，dst 干净。"""
    from robot.games._session_paths import (
        inherit_session_from,
        per_game_config_dir,
        per_game_session_jsonl,
    )

    src = tmp_path / "src_001"
    src.mkdir()
    dst = tmp_path / "dst_001"
    dst.mkdir()

    # 种一个 wrong-sid 的 jsonl
    wrong = per_game_session_jsonl(src, "wrong-sid")
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text("x", encoding="utf-8")

    assert inherit_session_from(src, dst, expected_sid="other-sid") is None
    assert not per_game_config_dir(dst).exists()


def test_inherit_session_ambiguous_when_multiple_subdirs(tmp_path: Path):
    """源有多个 project 子目录又没传 expected_sid → 不知该选哪个，安全返 None。"""
    from robot.games._session_paths import (
        inherit_session_from,
        per_game_config_dir,
    )

    src = tmp_path / "src_ambig"
    src.mkdir()
    dst = tmp_path / "dst_ambig"
    dst.mkdir()

    src_proj_root = per_game_config_dir(src) / "projects"
    (src_proj_root / "slug-a").mkdir(parents=True)
    (src_proj_root / "slug-a" / "a.jsonl").write_text("a")
    (src_proj_root / "slug-b").mkdir(parents=True)
    (src_proj_root / "slug-b" / "b.jsonl").write_text("b")

    assert inherit_session_from(src, dst) is None


def test_legacy_session_migrates_into_per_game(tmp_path: Path, monkeypatch):
    """模拟 legacy 游戏：global jsonl 存在但 per-game 路径空 → 应被搬到 per-game。

    不调 query()，只验文件搬运逻辑：直接复用 helper + 实现思路。
    """
    from robot.games._session_paths import (
        per_game_session_jsonl,
        global_session_jsonl,
    )

    # 隔离 ~/.claude/projects/ 到 tmp_path 避免污染真实 home
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    game_dir = tmp_path / "games" / "ai_legacy_001"
    game_dir.mkdir(parents=True)
    sid = "legacy-sid-99"

    gl = global_session_jsonl(game_dir, sid)
    gl.parent.mkdir(parents=True, exist_ok=True)
    gl.write_text("legacy turn\n", encoding="utf-8")

    pg = per_game_session_jsonl(game_dir, sid)
    assert not pg.exists()

    # 运行迁移逻辑（与 _run_claude_agent 内部块同款）
    if not pg.is_file() and gl.is_file():
        pg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gl, pg)

    assert pg.is_file()
    assert pg.read_text(encoding="utf-8") == "legacy turn\n"
    # legacy 文件保留（迁移是 copy 不是 move，幂等）
    assert gl.is_file()


def test_legacy_migration_idempotent_when_per_game_already_exists(
    tmp_path: Path, monkeypatch
):
    """per-game 已经有 jsonl → 迁移块不动它（不覆盖）。"""
    from robot.games._session_paths import (
        per_game_session_jsonl,
        global_session_jsonl,
    )

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    game_dir = tmp_path / "games" / "ai_already_001"
    game_dir.mkdir(parents=True)
    sid = "sid-7"

    pg = per_game_session_jsonl(game_dir, sid)
    pg.parent.mkdir(parents=True, exist_ok=True)
    pg.write_text("per-game first\n", encoding="utf-8")

    gl = global_session_jsonl(game_dir, sid)
    gl.parent.mkdir(parents=True, exist_ok=True)
    gl.write_text("global stale\n", encoding="utf-8")

    if not pg.is_file() and gl.is_file():
        shutil.copy2(gl, pg)  # should not run

    assert pg.read_text(encoding="utf-8") == "per-game first\n"


# ─────────────────────────────────────────────────────────────────────
# Section D — _run_claude_agent 内部接线（mock SDK，不调真 LLM）
# ─────────────────────────────────────────────────────────────────────


class _FakeResultIter:
    """Yield 一条 ResultMessage 让 _run_claude_agent 顺利结束（不走 zero-output retry）。"""
    def __init__(self, sid: str = "fake-out-sid"):
        self._sid = sid
        self._yielded = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        from claude_code_sdk import ResultMessage
        return ResultMessage(
            subtype="success",
            duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1,
            session_id=self._sid,
        )


def _install_query_capture(monkeypatch, out_sid: str = "fake-out-sid"):
    """Mock claude_code_sdk.query；捕获**首次**调用的 options（避免 retry-without-resume
    路径覆盖捕获结果）。同时在 SDK 调用瞬间快照 cwd 下 main.gd 的内容，便于
    cleanup 之后仍能断言模板复制结果。返回 captured dict。"""
    captured: dict = {}

    def fake_query(*, prompt, options):
        if "options" not in captured:
            captured["prompt"] = prompt
            captured["options"] = options
            try:
                cwd_path = Path(str(options.cwd))
                main_gd = cwd_path / "scenes" / "main.gd"
                captured["main_gd_at_sdk_call"] = (
                    main_gd.read_text(encoding="utf-8") if main_gd.is_file() else None
                )
            except Exception:
                captured["main_gd_at_sdk_call"] = None
        return _FakeResultIter(out_sid)

    import claude_code_sdk
    monkeypatch.setattr(claude_code_sdk, "query", fake_query)
    return captured


@pytest.mark.asyncio
async def test_run_agent_injects_per_game_claude_config_dir(
    tmp_path: Path, monkeypatch
):
    """_run_claude_agent 应该把 CLAUDE_CONFIG_DIR=<cwd>/.claude 塞进 options.env。"""
    captured = _install_query_capture(monkeypatch)
    game_dir = tmp_path / "games" / "ai_xx_001"
    game_dir.mkdir(parents=True)

    from robot.agent.tools.game_tools import _run_claude_agent

    result, sid, n_files = await _run_claude_agent(
        cwd=str(game_dir), prompt="hello", input_mode="touch",
        max_turns=1, use_mcp=False, session_id="",
    )

    from robot.games._session_paths import per_game_config_dir
    opts = captured["options"]
    # 关键不变式：CLAUDE_CONFIG_DIR 指向 sibling，不是 game_dir 内（防 zip 泄漏）
    assert opts.env.get("CLAUDE_CONFIG_DIR") == str(per_game_config_dir(game_dir))
    assert not opts.env["CLAUDE_CONFIG_DIR"].startswith(str(game_dir) + "/"), (
        "CLAUDE_CONFIG_DIR 不能落在 game_dir 内，否则 session 会随 zip 下发"
    )
    # cwd 透传一致
    assert str(opts.cwd) == str(game_dir)


@pytest.mark.asyncio
async def test_run_agent_does_not_pollute_parent_environ(
    tmp_path: Path, monkeypatch
):
    """`options.env` 通过 SDK subprocess 透传，不应该写到当前进程的 os.environ。"""
    import os
    _install_query_capture(monkeypatch)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    game_dir = tmp_path / "games" / "ai_yy_002"
    game_dir.mkdir(parents=True)

    from robot.agent.tools.game_tools import _run_claude_agent
    await _run_claude_agent(
        cwd=str(game_dir), prompt="x", use_mcp=False,
        max_turns=1, session_id="",
    )

    assert "CLAUDE_CONFIG_DIR" not in os.environ, (
        "per-game CLAUDE_CONFIG_DIR 不应该写到父进程 env，必须只走 SDK options.env"
    )


@pytest.mark.asyncio
async def test_run_agent_legacy_migration_fires_inside_function(
    tmp_path: Path, monkeypatch
):
    """带 session_id 调进 _run_claude_agent 时：global jsonl 在 + per-game 缺
    → 函数内部应该把 jsonl 搬到 per-game 路径再调 SDK（resume 才能命中）。"""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    captured = _install_query_capture(monkeypatch)

    game_dir = tmp_path / "games" / "ai_legacy_inproc_001"
    game_dir.mkdir(parents=True)
    sid = "legacy-inproc-sid"

    from robot.games._session_paths import (
        per_game_session_jsonl,
        global_session_jsonl,
    )
    gl = global_session_jsonl(game_dir, sid)
    gl.parent.mkdir(parents=True, exist_ok=True)
    gl.write_text('{"role":"user","text":"hi"}\n', encoding="utf-8")

    from robot.agent.tools.game_tools import _run_claude_agent
    await _run_claude_agent(
        cwd=str(game_dir), prompt="x", use_mcp=False,
        max_turns=1, session_id=sid,
    )

    # 1. per-game 路径（sibling .claude）已经被搬过去了
    pg = per_game_session_jsonl(game_dir, sid)
    assert pg.is_file()
    assert pg.read_text(encoding="utf-8") == '{"role":"user","text":"hi"}\n'
    # 2. SDK 拿到的 options.resume = sid，env 指向 sibling 而非 game_dir 内
    from robot.games._session_paths import per_game_config_dir
    opts = captured["options"]
    assert opts.resume == sid
    assert opts.env.get("CLAUDE_CONFIG_DIR") == str(per_game_config_dir(game_dir))


# ─────────────────────────────────────────────────────────────────────
# Section D-template — create_game 模板路径继承会话
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_game_resumes_template_session(
    tmp_path: Path, monkeypatch,
):
    """模板带 sibling .claude/ 时，create_game 应把 bundled sid 透传给 SDK。

    fixture: 假造一个「shooter」模板目录 + bundled session jsonl。
    断言：create_game 调 SDK 时 options.resume == bundled sid。

    GAMES_DIR 由 autouse 的 `_isolate_games_dir` 已经 patch 到 tmp_path/games；
    我们这里再 patch TEMPLATE_DIR 即可。
    """
    captured = _install_query_capture(monkeypatch, out_sid="post-resume-sid")

    fake_games = tmp_path / "games"
    monkeypatch.setattr(
        "robot.agent.tools.game_tools.TEMPLATE_DIR", fake_games / "_template",
        raising=True,
    )

    # 1. 构建空 _template（兜底）
    blank = fake_games / "_template"
    (blank / "scenes").mkdir(parents=True)
    (blank / "scenes" / "main.gd").write_text(
        "extends Node2D\n# AI will modify this file\n", encoding="utf-8",
    )
    (blank / "scenes" / "main.tscn").write_text("[gd_scene format=3]", encoding="utf-8")
    (blank / "project.godot").write_text("[application]\n", encoding="utf-8")

    # 2. 构建 shooter 官方模板目录（注意：在 _templates/ 子目录下） + bundled session
    shooter_id = "shooter"
    templates_root = fake_games / "_templates"
    shooter = templates_root / shooter_id
    (shooter / "scenes").mkdir(parents=True)
    (shooter / "scenes" / "main.gd").write_text(
        "extends Node2D\nfunc _ready(): pass\n", encoding="utf-8",
    )
    (shooter / "scenes" / "main.tscn").write_text("[gd_scene format=3]", encoding="utf-8")
    (shooter / "project.godot").write_text("[application]\n", encoding="utf-8")
    # bundled session（dev 路径 slug，prod 上对不上 → 走 auto-discover）
    bundled_sid = "tpl-bundled-sid"
    bundled_proj = (
        templates_root / f"{shooter_id}.claude" / "projects"
        / "-some-dev-machine-slug"
    )
    bundled_proj.mkdir(parents=True)
    (bundled_proj / f"{bundled_sid}.jsonl").write_text(
        '{"role":"system","content":"design rationale"}\n', encoding="utf-8",
    )

    from robot.agent.tools.game_tools import create_game
    # mock SDK 返空会让 create_game 走 generation_failed 清掉新 game_dir，
    # 这里我们只关心 SDK 拿到的 options.resume 是不是 bundled sid，cleanup
    # 行为不在本测试 scope，所以直接调用、最后检查 captured 即可。
    await create_game.ainvoke({
        "title": "Cosmic Shoot",
        "description": "A shooter where stars are enemies",
        "input_mode": "touch",
        "icon": "🚀",
        "template_game_id": shooter_id,
    })

    opts = captured["options"]
    assert opts.resume == bundled_sid, (
        f"create_game 应把模板 bundled sid={bundled_sid} 透传给 SDK 做 resume，"
        f"实际 opts.resume={opts.resume!r}"
    )
    # 同时校验 cwd 是新 game_dir（不是模板源），CLAUDE_CONFIG_DIR 指向新 sibling
    new_cwd = Path(str(opts.cwd))
    assert new_cwd != shooter, "cwd 必须是新 game_dir，不能是模板源目录"
    assert opts.env.get("CLAUDE_CONFIG_DIR") == str(new_cwd.parent / f"{new_cwd.name}.claude")


@pytest.mark.asyncio
async def test_create_game_falls_back_to_blank_when_template_id_missing(
    tmp_path: Path, monkeypatch,
):
    """LLM / iOS 传了一个 _templates/ 里**不存在**的 id（hallucination 或 stale
    registry）→ create_game 应静默降级到空脚手架 _template，不抛错、不去
    GAMES_DIR 根目录里乱找用户游戏。"""
    captured = _install_query_capture(monkeypatch, out_sid="fb-sid")
    fake_games = tmp_path / "games"
    monkeypatch.setattr(
        "robot.agent.tools.game_tools.TEMPLATE_DIR", fake_games / "_template",
        raising=True,
    )

    # 1. 空脚手架 _template
    blank = fake_games / "_template"
    (blank / "scenes").mkdir(parents=True)
    (blank / "scenes" / "main.gd").write_text(
        "extends Node2D\n# AI will modify this file\n", encoding="utf-8",
    )
    (blank / "scenes" / "main.tscn").write_text("[gd_scene format=3]", encoding="utf-8")
    (blank / "project.godot").write_text("[application]\n", encoding="utf-8")

    # 2. **故意**在 GAMES_DIR 根下造一个 ai_some_user 目录（模拟用户游戏池）
    #    create_game 必须 NOT 选它做模板（不再扫用户游戏目录）
    pretend_user_game = fake_games / "ai_other_user_001"
    (pretend_user_game / "scenes").mkdir(parents=True)
    (pretend_user_game / "scenes" / "main.gd").write_text(
        "extends Node2D\n# bob's source code\n", encoding="utf-8",
    )

    from robot.agent.tools.game_tools import create_game
    await create_game.ainvoke({
        "title": "Some Game",
        "description": "anything",
        "input_mode": "touch",
        "icon": "🎮",
        # 这个 id 既不在 _templates/ 也不在 GAMES_DIR 根 ——
        # 在 GAMES_DIR 根那条名字（"ai_other_user_001"）即使存在也不能被选中
        "template_game_id": "ai_other_user_001",
    })

    # SDK 调用瞬间快照的 main.gd 必须是 blank _template 的内容（用 captured
    # 而不是 cwd 后读，因为 mock 返空后 cleanup 会把新 game_dir 删掉）
    main_gd_at_call = captured.get("main_gd_at_sdk_call") or ""
    assert "AI will modify" in main_gd_at_call, (
        "降级失败：create_game 错误地把 GAMES_DIR/<id> 用户游戏当成模板了。"
        "新方案下只能从 _templates/<id>/ 找模板。"
    )
    assert "bob's source code" not in main_gd_at_call, (
        "致命：用户游戏源代码泄漏到新副本。template 路径不能再扫 GAMES_DIR 根目录。"
    )

    # 没继承 session（_template 没 sibling），fresh start
    opts = captured["options"]
    assert opts.resume is None


@pytest.mark.asyncio
async def test_create_game_no_template_session_starts_fresh(
    tmp_path: Path, monkeypatch,
):
    """模板没带 session（_template 默认空脚手架）→ SDK 拿到 resume=None，
    走 fresh start。GAMES_DIR 由 autouse fixture 处理，这里只补 TEMPLATE_DIR。"""
    captured = _install_query_capture(monkeypatch, out_sid="fresh-out-sid")

    fake_games = tmp_path / "games"  # already created by _isolate_games_dir autouse
    monkeypatch.setattr(
        "robot.agent.tools.game_tools.TEMPLATE_DIR", fake_games / "_template",
        raising=True,
    )

    blank = fake_games / "_template"
    (blank / "scenes").mkdir(parents=True)
    (blank / "scenes" / "main.gd").write_text(
        "extends Node2D\n# AI will modify this file\n", encoding="utf-8",
    )
    (blank / "scenes" / "main.tscn").write_text("[gd_scene format=3]", encoding="utf-8")
    (blank / "project.godot").write_text("[application]\n", encoding="utf-8")

    from robot.agent.tools.game_tools import create_game
    await create_game.ainvoke({
        "title": "Plain Game",
        "description": "Just a game",
        "input_mode": "touch",
        "icon": "🎮",
        "template_game_id": "",  # 走默认 _template 路径
    })

    opts = captured["options"]
    # 默认模板没 sibling .claude/，应该不 resume
    assert opts.resume is None, (
        f"_template 默认空脚手架不带 session，opts.resume 必须是 None，"
        f"实际={opts.resume}"
    )


# ─────────────────────────────────────────────────────────────────────
# Section E — copytree / chain remix / re-remix 幂等
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clone_explicitly_copies_sibling_claude_dir(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_session: dict, bob_for_remix: dict,
):
    """sibling `.claude/` 不在 src_dir 里，所以 shutil.copytree(src_dir, dst_dir)
    不会自动带过来；必须靠 clone_game 内部显式 copytree sibling。这里校验
    它确实拷过去了 + 字节一致 + 源不被污染。"""
    from robot.games import service as game_service
    from robot.games._session_paths import per_game_session_jsonl

    src_id = alice_game_with_session["game_id"]
    sid = alice_game_with_session["session_id"]

    seeded = alice_game_with_session["seeded_jsonl"]
    src_jsonl_before = seeded.read_bytes()

    resp = await client.post(
        f"/api/games/{src_id}/remix",
        headers=auth_header(bob_for_remix["access_token"]),
    )
    new_id = resp.json()["game_id"]
    new_dir = game_service.GAMES_DIR / new_id

    # 副本的 sibling jsonl 文件存在 + 与源原始内容字节一致（copy + rename 不改 content）
    new_jsonl = per_game_session_jsonl(new_dir, sid)
    assert new_jsonl.is_file()
    assert new_jsonl.read_bytes() == src_jsonl_before

    # 源 jsonl 没被污染
    assert seeded.read_bytes() == src_jsonl_before


@pytest.mark.asyncio
async def test_chain_remix_inherits_session_through_b_to_c(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_session: dict, bob_for_remix: dict,
):
    """A → B remix → B 把 visibility 设 public_remixable → C remix B
    → C 应继承 sid（因为 B 已经继承了 A 的 sid + jsonl 在 B/.claude/.../<dst-slug>/）。"""
    from robot.games import service as game_service
    from robot.games._session_paths import cwd_to_project_slug

    src_id = alice_game_with_session["game_id"]
    sid = alice_game_with_session["session_id"]

    from robot.games._session_paths import per_game_session_jsonl

    # B = bob remix Alice
    r_b = await client.post(
        f"/api/games/{src_id}/remix",
        headers=auth_header(bob_for_remix["access_token"]),
    )
    b_id = r_b.json()["game_id"]
    b_meta = json.loads(
        (game_service.GAMES_DIR / b_id / "meta.json").read_text(encoding="utf-8")
    )
    assert b_meta["session_id"] == sid

    # B 公开 remixable
    await client.patch(
        f"/api/games/{b_id}/visibility",
        json={"visibility": "public_remixable"},
        headers=auth_header(bob_for_remix["access_token"]),
    )

    # C = 新用户 remix B
    carol = await register_and_login(
        client, email="carol_chain@example.com", device_id="dev-c-chain"
    )
    r_c = await client.post(
        f"/api/games/{b_id}/remix",
        headers=auth_header(carol["access_token"]),
    )
    assert r_c.status_code == 200, r_c.text
    c_id = r_c.json()["game_id"]
    c_dir = game_service.GAMES_DIR / c_id

    # C 也继承 sid
    c_meta = json.loads((c_dir / "meta.json").read_text(encoding="utf-8"))
    assert c_meta["session_id"] == sid

    # C sibling jsonl 在新 cwd slug 下
    assert per_game_session_jsonl(c_dir, sid).is_file()


@pytest.mark.asyncio
async def test_delete_game_endpoint_removes_sibling_claude_dir(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_session: dict,
):
    """DELETE /api/games/{id} 必须把 sibling .claude 一起清掉，否则用户的
    Claude Code 会话 jsonl 会无限期 leak 在服务器磁盘上。"""
    from robot.games import service as game_service
    from robot.games._session_paths import per_game_config_dir

    src_id = alice_game_with_session["game_id"]
    src_dir = alice_game_with_session["src_dir"]
    src_claude = per_game_config_dir(src_dir)
    assert src_claude.is_dir(), "前置：fixture 应该已经造好 sibling .claude"

    resp = await client.delete(
        f"/api/games/{src_id}",
        headers=auth_header(alice_game_with_session["access_token"]),
    )
    assert resp.status_code == 200, resp.text

    assert not src_dir.exists(), "game_dir 应被删掉"
    assert not (game_service.GAMES_DIR / f"{src_id}.zip").exists(), "zip 应被删掉"
    assert not src_claude.exists(), (
        "sibling .claude 必须一起清掉，不能留 stale 会话 jsonl 在磁盘"
    )


def test_wipe_game_artifacts_idempotent_when_partial(tmp_path: Path):
    """wipe_game_artifacts 在仅部分 artifact 存在时也能正确清理（不抛异常）。
    防御 cancel 路径在 zip 还没生成时就触发的场景。"""
    from robot.games._session_paths import (
        wipe_game_artifacts,
        per_game_config_dir,
    )

    game_dir = tmp_path / "games" / "ai_partial_001"
    game_dir.mkdir(parents=True)
    (game_dir / "junk.txt").write_text("x")
    # 没有 zip，没有 sibling .claude

    wipe_game_artifacts(game_dir)
    assert not game_dir.exists()
    assert not per_game_config_dir(game_dir).exists()
    assert not (game_dir.parent / "ai_partial_001.zip").exists()

    # 完全空（什么都没有）→ 也不应抛
    ghost = tmp_path / "games" / "ai_ghost_002"
    wipe_game_artifacts(ghost)


@pytest.mark.asyncio
async def test_re_remix_idempotent_does_not_break_inheritance(
    db_session: AsyncSession, client: AsyncClient,
    alice_game_with_session: dict, bob_for_remix: dict,
):
    """同一用户对同一源 remix 两次 → 第二次走 service.py:559-576 幂等分支，
    返回已存在副本；副本的 sid + jsonl 不应该被破坏。"""
    from robot.games import service as game_service
    from robot.games._session_paths import cwd_to_project_slug

    src_id = alice_game_with_session["game_id"]
    sid = alice_game_with_session["session_id"]
    h = auth_header(bob_for_remix["access_token"])

    r1 = await client.post(f"/api/games/{src_id}/remix", headers=h)
    new_id = r1.json()["game_id"]

    # 二次 remix —— 应原样返回已有副本
    r2 = await client.post(f"/api/games/{src_id}/remix", headers=h)
    assert r2.json()["game_id"] == new_id

    # 副本 sid + sibling jsonl 仍然完好
    from robot.games._session_paths import per_game_session_jsonl
    new_dir = game_service.GAMES_DIR / new_id
    new_meta = json.loads((new_dir / "meta.json").read_text(encoding="utf-8"))
    assert new_meta["session_id"] == sid
    assert per_game_session_jsonl(new_dir, sid).is_file()


# ─────────────────────────────────────────────────────────────────────
# Section F — sliding-window compaction + append-only archive
# ─────────────────────────────────────────────────────────────────────


def _make_synthetic_jsonl(target_path: Path, n_prompts: int, turns_per_prompt: int = 3, msg_padding: int = 500):
    """造一个仿真 SDK jsonl：n_prompts 个 prompt cycle，每个 cycle 有 user+若干
    assistant turn。每条带 parentUuid 链 + promptId 标记，结构与真实 jsonl 形态
    一致，可被 compact_session 解析。"""
    import uuid as _uuid
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    last_uuid = None
    pad = "x" * msg_padding  # 让每条变大方便测 byte budget
    for p in range(n_prompts):
        pid = str(_uuid.uuid4())
        # 每个 prompt 第一行 = user 消息（带 promptId）
        u_uid = str(_uuid.uuid4())
        user_line = {
            "type": "user",
            "parentUuid": last_uuid,
            "promptId": pid,
            "uuid": u_uid,
            "message": {"role": "user", "content": f"prompt {p} {pad}"},
        }
        lines.append(json.dumps(user_line, ensure_ascii=False) + "\n")
        last_uuid = u_uid
        # 后面跟若干 assistant turn（无 promptId，链 parentUuid）
        for t in range(turns_per_prompt):
            a_uid = str(_uuid.uuid4())
            assistant_line = {
                "type": "assistant",
                "parentUuid": last_uuid,
                "uuid": a_uid,
                "message": {"role": "assistant", "content": f"reply {p}.{t} {pad}"},
            }
            lines.append(json.dumps(assistant_line, ensure_ascii=False) + "\n")
            last_uuid = a_uid
    target_path.write_text("".join(lines), encoding="utf-8")


def test_compact_session_basic_trim(tmp_path: Path):
    """compact_session 的核心路径：超 cap → 按 prompt 边界切头 → archive 增长 +
    active 缩小 + 第一行 parentUuid 改 null。"""
    from robot.games._session_paths import (
        compact_session,
        per_game_session_jsonl,
        session_archive_jsonl,
    )

    game_dir = tmp_path / "ai_compact_basic"
    game_dir.mkdir()
    sid = "sid-compact-1"
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=5, turns_per_prompt=3, msg_padding=500)

    before_size = active.stat().st_size
    assert before_size > 10_000  # 5 prompts × ~2KB+ each = ~10KB+

    ok = compact_session(game_dir, sid, target_bytes=4_000, min_prompts_kept=2)
    assert ok is True

    after_size = active.stat().st_size
    archive_size = session_archive_jsonl(game_dir, sid).stat().st_size
    assert archive_size > 0
    # 字节守恒（差异在 parentUuid rewrite 后第一行重序列化的 0~10 bytes 范围）
    assert abs((after_size + archive_size) - before_size) < 50

    # active 第一行 parentUuid = null
    first_line = active.read_text(encoding="utf-8").splitlines(keepends=True)[0]
    first_msg = json.loads(first_line)
    assert first_msg.get("parentUuid") is None


def test_compact_session_keeps_min_prompts_when_target_too_small(tmp_path: Path):
    """target_bytes 小到放不下 min_prompts_kept 个 prompt → 回退到保留 min 个，
    哪怕超 target。不让 active 被裁到只剩 1 个 prompt。"""
    from robot.games._session_paths import compact_session, per_game_session_jsonl

    game_dir = tmp_path / "ai_compact_min"
    game_dir.mkdir()
    sid = "sid-compact-2"
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=4, turns_per_prompt=3, msg_padding=500)

    # target 给到极小（1 字节），强制走 min_prompts_kept fallback
    ok = compact_session(game_dir, sid, target_bytes=1, min_prompts_kept=2)
    assert ok is True

    # 活动 jsonl 应至少含 2 个 promptId
    pids = set()
    for line in active.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        pid = d.get("promptId")
        if pid:
            pids.add(pid)
    assert len(pids) >= 2


def test_compact_session_returns_false_if_too_few_prompts(tmp_path: Path):
    """jsonl 里 prompt 数 < min_prompts_kept + 1 → 没"切头"余地 → 返 False，不动文件。"""
    from robot.games._session_paths import compact_session, per_game_session_jsonl, session_archive_jsonl

    game_dir = tmp_path / "ai_compact_few"
    game_dir.mkdir()
    sid = "sid-compact-3"
    active = per_game_session_jsonl(game_dir, sid)
    # 只 2 个 prompt，要求 keep ≥ 2 → 没法切
    _make_synthetic_jsonl(active, n_prompts=2, turns_per_prompt=2, msg_padding=200)
    before = active.read_bytes()

    ok = compact_session(game_dir, sid, target_bytes=10, min_prompts_kept=2)
    assert ok is False

    # 文件未被改动 + archive 没被创建
    assert active.read_bytes() == before
    assert not session_archive_jsonl(game_dir, sid).exists()


def test_compact_session_archive_appends_across_calls(tmp_path: Path):
    """多次 compact_session 调用，archive 是 append-only（单文件累积），不被覆盖。"""
    from robot.games._session_paths import compact_session, per_game_session_jsonl, session_archive_jsonl

    game_dir = tmp_path / "ai_compact_append"
    game_dir.mkdir()
    sid = "sid-compact-4"
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=6, turns_per_prompt=2, msg_padding=300)

    archive = session_archive_jsonl(game_dir, sid)

    # 第一次 compact
    ok1 = compact_session(game_dir, sid, target_bytes=2_000, min_prompts_kept=2)
    assert ok1 is True
    arch_size_1 = archive.stat().st_size

    # 给 active 长一点（仿真又 modify 了几次）
    extra = []
    for line in active.read_text(encoding="utf-8").splitlines(keepends=True):
        extra.append(line)
    # 复制末尾几行追加，模拟新生成的 turns
    for _ in range(3):
        extra.append(extra[-1])
    active.write_text("".join(extra), encoding="utf-8")

    # 第二次 compact
    ok2 = compact_session(game_dir, sid, target_bytes=2_000, min_prompts_kept=2)
    # 第二次可能因为 prompt 数不够（之前已切走大部分）返 False，无所谓 ——
    # 关键是如果成功，archive 应只增不减
    arch_size_2 = archive.stat().st_size
    assert arch_size_2 >= arch_size_1


def test_compact_session_idempotent_on_already_small_jsonl(tmp_path: Path):
    """active 已经在 cap 内 → 调 compact 也不该破坏（_run_claude_agent 不会调，
    但单元测试守住边界）。这个 case 实际由调用方 size check 守门，helper 仍
    应在 prompt 数太少时安全返 False。"""
    from robot.games._session_paths import compact_session, per_game_session_jsonl

    game_dir = tmp_path / "ai_compact_small"
    game_dir.mkdir()
    sid = "sid-compact-5"
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=2, turns_per_prompt=1, msg_padding=50)
    before_bytes = active.read_bytes()

    # 太小 prompt 数 → 不切
    ok = compact_session(game_dir, sid, target_bytes=80_000, min_prompts_kept=2)
    assert ok is False
    assert active.read_bytes() == before_bytes


def test_flush_active_to_archive_appends(tmp_path: Path):
    """flush_active_to_archive 把整个 active append 到 archive，active 不动。"""
    from robot.games._session_paths import (
        flush_active_to_archive,
        per_game_session_jsonl,
        session_archive_jsonl,
    )

    game_dir = tmp_path / "ai_flush"
    game_dir.mkdir()
    sid = "sid-flush"
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=2, turns_per_prompt=1, msg_padding=100)
    before = active.read_bytes()

    flush_active_to_archive(game_dir, sid)

    # active 内容不变
    assert active.read_bytes() == before
    # archive 包含 active 的全部内容
    archive = session_archive_jsonl(game_dir, sid)
    assert archive.is_file()
    assert archive.read_bytes() == before


def test_flush_active_to_archive_no_op_when_active_missing(tmp_path: Path):
    """active 不存在 → flush 静默 no-op，不抛错、不创建 archive。"""
    from robot.games._session_paths import flush_active_to_archive, session_archive_jsonl

    game_dir = tmp_path / "ai_flush_empty"
    game_dir.mkdir()

    flush_active_to_archive(game_dir, "nonexistent-sid")
    assert not session_archive_jsonl(game_dir, "nonexistent-sid").exists()


@pytest.mark.asyncio
async def test_run_agent_triggers_compact_above_cap(tmp_path: Path, monkeypatch):
    """_run_claude_agent 在 jsonl > 50KB（SESSION_TRIM_TRIGGER_BYTES）时应自动
    调 compact_session，从而让 SDK 收到的 options.resume 仍然有效（active 已被
    缩小但 sid 不变）。"""
    captured = _install_query_capture(monkeypatch)
    game_dir = tmp_path / "games" / "ai_compact_trigger"
    game_dir.mkdir(parents=True)
    sid = "sid-compact-trig"
    from robot.games._session_paths import per_game_session_jsonl, session_archive_jsonl
    active = per_game_session_jsonl(game_dir, sid)
    # 造一个超 50KB 的 jsonl（5 prompts × 4 turns × 2.5K padding ≈ 60KB+）
    _make_synthetic_jsonl(active, n_prompts=5, turns_per_prompt=4, msg_padding=2500)
    assert active.stat().st_size > 50_000

    from robot.agent.tools.game_tools import _run_claude_agent
    await _run_claude_agent(
        cwd=str(game_dir), prompt="x", use_mcp=False,
        max_turns=1, session_id=sid,
    )

    # 1. compact 已发生：active 缩小、archive 出现
    assert active.stat().st_size < 50_000, "active should be compacted below trigger"
    assert session_archive_jsonl(game_dir, sid).is_file(), "archive should exist"

    # 2. SDK 拿到的 options.resume 仍是同一个 sid（保持不变）
    opts = captured["options"]
    assert opts.resume == sid


@pytest.mark.asyncio
async def test_run_agent_cliff_fallback_when_compact_fails(tmp_path: Path, monkeypatch):
    """compact 失败（jsonl 无可切边界）→ flush_active_to_archive + 删孤儿
    active jsonl + clear sid → SDK options.resume = None（fresh start）。"""
    captured = _install_query_capture(monkeypatch)
    game_dir = tmp_path / "games" / "ai_compact_cliff"
    game_dir.mkdir(parents=True)
    sid = "sid-cliff"
    from robot.games._session_paths import per_game_session_jsonl, session_archive_jsonl
    active = per_game_session_jsonl(game_dir, sid)
    # 造一个超 50KB 的 jsonl，但只有 1 个 prompt → compact 必失败
    _make_synthetic_jsonl(active, n_prompts=1, turns_per_prompt=10, msg_padding=6000)
    assert active.stat().st_size > 50_000

    before_active = active.read_bytes()

    from robot.agent.tools.game_tools import _run_claude_agent
    await _run_claude_agent(
        cwd=str(game_dir), prompt="x", use_mcp=False,
        max_turns=1, session_id=sid,
    )

    # 1. cliff fallback：SDK options.resume = None
    opts = captured["options"]
    assert opts.resume is None, "compact 失败应触发 cliff，SDK 不应再 resume 旧 sid"

    # 2. flush 已发生：archive 含原 active 全部内容
    archive = session_archive_jsonl(game_dir, sid)
    assert archive.is_file()
    assert archive.read_bytes() == before_active

    # 3. 旧 active jsonl **已被删除**，避免磁盘上累积孤儿
    assert not active.exists(), (
        "cliff fallback 后旧 active jsonl 应被删掉（archive 里有完整副本）"
    )


@pytest.mark.asyncio
async def test_run_agent_skips_compact_below_cap(tmp_path: Path, monkeypatch):
    """jsonl < 50KB → 不触发 compact、archive 不出现、SDK 正常 resume。"""
    captured = _install_query_capture(monkeypatch)
    game_dir = tmp_path / "games" / "ai_compact_under_cap"
    game_dir.mkdir(parents=True)
    sid = "sid-undercap"
    from robot.games._session_paths import per_game_session_jsonl, session_archive_jsonl
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=3, turns_per_prompt=2, msg_padding=1000)
    pre_size = active.stat().st_size
    assert pre_size < 50_000

    from robot.agent.tools.game_tools import _run_claude_agent
    await _run_claude_agent(
        cwd=str(game_dir), prompt="x", use_mcp=False,
        max_turns=1, session_id=sid,
    )

    # 1. active 大小不变（compact 没跑）
    assert active.stat().st_size == pre_size
    # 2. archive 不存在
    assert not session_archive_jsonl(game_dir, sid).exists()
    # 3. SDK 仍 resume 该 sid
    opts = captured["options"]
    assert opts.resume == sid


def _make_realistic_sdk_jsonl(target_path: Path, n_prompts: int = 3, msg_padding: int = 300):
    """造一个 **真实形态** 的 SDK jsonl，含 6 种 type：
    queue-operation / user / attachment / ai-title / last-prompt / assistant.
    用于测 compact 在多类型行混合场景下不会破坏结构。

    每个 prompt cycle 的真实形状（按观察的真实 jsonl 推断）：
      queue-op enqueue → user (with promptId) → attachment? → ai-title →
      last-prompt → assistant... → queue-op dequeue
    """
    import uuid as _uuid
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    last_uuid = None
    pad = "x" * msg_padding
    sess_id = "test-session-id-" + _uuid.uuid4().hex[:8]

    for p in range(n_prompts):
        # queue-op enqueue（无 parentUuid，无 promptId）
        lines.append(json.dumps({
            "type": "queue-operation", "operation": "enqueue",
            "sessionId": sess_id, "timestamp": 1700000000 + p * 10,
        }, ensure_ascii=False) + "\n")

        # user message（带 promptId 标记，新 prompt 起点）
        pid = str(_uuid.uuid4())
        u_uid = str(_uuid.uuid4())
        lines.append(json.dumps({
            "type": "user", "parentUuid": last_uuid,
            "promptId": pid, "uuid": u_uid, "isSidechain": False,
            "message": {"role": "user", "content": f"prompt {p} {pad}"},
        }, ensure_ascii=False) + "\n")
        last_uuid = u_uid

        # attachment（每两轮加一个，模拟 user 上传了图片）
        if p % 2 == 0:
            att_uid = str(_uuid.uuid4())
            lines.append(json.dumps({
                "type": "attachment", "parentUuid": last_uuid,
                "uuid": att_uid, "isSidechain": False,
                "attachment": {"name": "img.png", "size": 1024},
            }, ensure_ascii=False) + "\n")

        # ai-title + last-prompt（无 parentUuid，session-scoped 元数据）
        lines.append(json.dumps({
            "type": "ai-title", "aiTitle": f"Game {p}", "sessionId": sess_id,
        }, ensure_ascii=False) + "\n")
        lines.append(json.dumps({
            "type": "last-prompt", "lastPrompt": f"prompt {p}", "sessionId": sess_id,
        }, ensure_ascii=False) + "\n")

        # 2 个 assistant turn（每个 turn 模拟一次 LLM 调用）
        for t in range(2):
            a_uid = str(_uuid.uuid4())
            lines.append(json.dumps({
                "type": "assistant", "parentUuid": last_uuid,
                "uuid": a_uid, "isSidechain": False,
                "message": {"role": "assistant", "content": f"reply {p}.{t} {pad}"},
            }, ensure_ascii=False) + "\n")
            last_uuid = a_uid

        # queue-op dequeue
        lines.append(json.dumps({
            "type": "queue-operation", "operation": "dequeue",
            "sessionId": sess_id, "timestamp": 1700000000 + p * 10 + 5,
        }, ensure_ascii=False) + "\n")

    target_path.write_text("".join(lines), encoding="utf-8")


def test_compact_session_handles_realistic_sdk_jsonl(tmp_path: Path):
    """真实 SDK jsonl 含 queue-operation / ai-title / last-prompt / attachment
    等多种 type 行（无 promptId）。compact 应正确按 user 消息边界切分，不
    crash 不丢链路完整性。"""
    from robot.games._session_paths import (
        compact_session,
        per_game_session_jsonl,
        session_archive_jsonl,
    )

    game_dir = tmp_path / "ai_realistic"
    game_dir.mkdir()
    sid = "sid-realistic"
    active = per_game_session_jsonl(game_dir, sid)
    _make_realistic_sdk_jsonl(active, n_prompts=5, msg_padding=400)

    pre_lines = active.read_text(encoding="utf-8").splitlines()
    pre_user_uuids = []
    for line in pre_lines:
        d = json.loads(line)
        if d.get("type") == "user":
            pre_user_uuids.append(d.get("uuid"))

    ok = compact_session(game_dir, sid, target_bytes=3_000, min_prompts_kept=2)
    assert ok is True, "compact should succeed on realistic-shape jsonl"

    # 1. active 第一行必须是 user 类型 + parentUuid=None
    post_lines = active.read_text(encoding="utf-8").splitlines()
    first = json.loads(post_lines[0])
    assert first["type"] == "user"
    assert first.get("parentUuid") is None
    assert first.get("promptId"), "promptId 应保留"

    # 2. 所有 type 都仍能解析（compact 不损坏 JSON）
    for line in post_lines:
        d = json.loads(line)  # 不 raise 即 OK
        assert "type" in d

    # 3. archive 也是合法 jsonl
    archive = session_archive_jsonl(game_dir, sid)
    for line in archive.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_compact_session_preserves_chain_integrity_after_first_message(tmp_path: Path):
    """parentUuid 链不变式：compact 只 rewrite 第一条 kept 消息的 parentUuid，
    后续消息的 parentUuid 应保持原值（指向同样在 kept 范围内的 uuid）。这
    保证整段 kept 区间在自身范围内 chain 完整。"""
    from robot.games._session_paths import compact_session, per_game_session_jsonl

    game_dir = tmp_path / "ai_chain"
    game_dir.mkdir()
    sid = "sid-chain"
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=4, turns_per_prompt=3, msg_padding=300)

    # 记下原始 parentUuid 链
    pre_lines = active.read_text(encoding="utf-8").splitlines()
    pre_chain = [(json.loads(l).get("uuid"), json.loads(l).get("parentUuid")) for l in pre_lines]

    ok = compact_session(game_dir, sid, target_bytes=3_000, min_prompts_kept=2)
    assert ok is True

    post_lines = active.read_text(encoding="utf-8").splitlines()
    post_chain = [(json.loads(l).get("uuid"), json.loads(l).get("parentUuid")) for l in post_lines]

    # 第一条：parentUuid 改 null（rewrite 标记）
    assert post_chain[0][1] is None

    # 第一条之后的每一条：uuid 和 parentUuid 都应该和原来某行一致
    pre_uuid_to_parent = dict(pre_chain)
    for uid, puid in post_chain[1:]:
        # 这条消息原本 parentUuid 就是 puid（compact 没改它）
        assert pre_uuid_to_parent.get(uid) == puid, (
            f"chain corruption: uuid={uid} now has parent={puid} but originally {pre_uuid_to_parent.get(uid)}"
        )

    # 此外：每一条 (除第一条外) 的 parentUuid 应能在 post 链中找到（chain 完整）
    post_uuids = {uid for uid, _ in post_chain if uid}
    for uid, puid in post_chain[1:]:
        if puid is None:
            continue
        assert puid in post_uuids, f"orphan parentUuid {puid} in compacted active"


def test_compact_full_history_reconstructable_from_archive_plus_active(tmp_path: Path):
    """关键不变式：完整历史 = archive + active（重建后字节计数等于原 jsonl 字节数 ±
    parentUuid rewrite 引入的极小 diff < 50 bytes）。多次 compact 后仍成立。"""
    from robot.games._session_paths import (
        compact_session,
        per_game_session_jsonl,
        session_archive_jsonl,
    )

    game_dir = tmp_path / "ai_reconstruct"
    game_dir.mkdir()
    sid = "sid-reconstruct"
    active = per_game_session_jsonl(game_dir, sid)
    _make_synthetic_jsonl(active, n_prompts=8, turns_per_prompt=3, msg_padding=400)

    original_size = active.stat().st_size
    original_lines_count = len(active.read_text(encoding="utf-8").splitlines())

    # 第一次 compact
    ok1 = compact_session(game_dir, sid, target_bytes=3_000, min_prompts_kept=2)
    assert ok1 is True
    archive = session_archive_jsonl(game_dir, sid)
    sum_after_1 = active.stat().st_size + archive.stat().st_size
    assert abs(sum_after_1 - original_size) < 50, "完整历史字节守恒（首轮）"

    lines_after_1 = (
        len(archive.read_text(encoding="utf-8").splitlines())
        + len(active.read_text(encoding="utf-8").splitlines())
    )
    assert lines_after_1 == original_lines_count, "首轮 compact 后行数守恒"

    # 模拟新 modify 又 append 了一些 turns
    _append_more = """{"type":"user","parentUuid":null,"promptId":"new-pid-1","uuid":"new-u-1","message":{}}\n{"type":"assistant","parentUuid":"new-u-1","uuid":"new-a-1","message":{}}\n"""
    with active.open("a", encoding="utf-8") as f:
        f.write(_append_more)

    # 第二次 compact（再次会切走前面 prompt）
    ok2 = compact_session(game_dir, sid, target_bytes=1_000, min_prompts_kept=2)
    if ok2:
        sum_after_2 = active.stat().st_size + archive.stat().st_size
        assert abs(sum_after_2 - (sum_after_1 + len(_append_more))) < 100, (
            "二轮 compact 后字节守恒（archive append-only，不丢历史）"
        )


# ─────────────────────────────────────────────────────────────────────
# Section G — truncate_stale_tool_results
# ─────────────────────────────────────────────────────────────────────


def _make_jsonl_with_reads(target_path: Path, n_prompts: int = 5,
                            reads_per_prompt: int = 2, read_content_size: int = 5000):
    """合成 jsonl：每个 prompt 含若干 Read tool_use + tool_result，模拟
    "Read 全文 main.gd 多次"的真实场景。"""
    import uuid as _uuid
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    last_uuid = None
    fake_main_gd = "x" * read_content_size
    for p in range(n_prompts):
        pid = str(_uuid.uuid4())
        u_uid = str(_uuid.uuid4())
        lines.append(json.dumps({
            "type": "user", "parentUuid": last_uuid,
            "promptId": pid, "uuid": u_uid,
            "message": {"role": "user", "content": f"prompt {p}"},
        }, ensure_ascii=False) + "\n")
        last_uuid = u_uid
        for r in range(reads_per_prompt):
            tu_id = f"tool_{p}_{r}"
            a_uid = str(_uuid.uuid4())
            lines.append(json.dumps({
                "type": "assistant", "parentUuid": last_uuid, "uuid": a_uid,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tu_id, "name": "Read",
                     "input": {"file_path": "scenes/main.gd"}},
                ]},
            }, ensure_ascii=False) + "\n")
            last_uuid = a_uid
            r_uid = str(_uuid.uuid4())
            lines.append(json.dumps({
                "type": "user", "parentUuid": last_uuid, "uuid": r_uid,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu_id,
                     "content": fake_main_gd},
                ]},
            }, ensure_ascii=False) + "\n")
            last_uuid = r_uid
    target_path.write_text("".join(lines), encoding="utf-8")


def test_truncate_stale_tool_results_basic(tmp_path: Path):
    """5 prompt × 2 Read = 10 Read result。保留最近 3 prompt → 截前 2 prompt 的 4 个 Read。"""
    from robot.games._session_paths import (
        truncate_stale_tool_results, per_game_session_jsonl,
    )
    game_dir = tmp_path / "ai_truncate_basic"
    game_dir.mkdir()
    sid = "sid-truncate-1"
    active = per_game_session_jsonl(game_dir, sid)
    _make_jsonl_with_reads(active, n_prompts=5, reads_per_prompt=2, read_content_size=5000)

    before_size = active.stat().st_size
    n = truncate_stale_tool_results(game_dir, sid, keep_recent_prompts=3)
    after_size = active.stat().st_size

    assert n == 4
    assert after_size < before_size
    assert before_size - after_size > 15_000


def test_truncate_preserves_recent_prompts(tmp_path: Path):
    """最后 3 prompt 的 tool_result 完整保留 5000 chars 不变。"""
    from robot.games._session_paths import (
        truncate_stale_tool_results, per_game_session_jsonl,
    )
    game_dir = tmp_path / "ai_truncate_preserve"
    game_dir.mkdir()
    sid = "sid-truncate-2"
    active = per_game_session_jsonl(game_dir, sid)
    _make_jsonl_with_reads(active, n_prompts=5, reads_per_prompt=2, read_content_size=5000)

    truncate_stale_tool_results(game_dir, sid, keep_recent_prompts=3)

    seen_pids = []
    pid_to_lines = {}
    last_pid = None
    for line in active.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        pid = d.get("promptId")
        if pid and pid not in seen_pids:
            seen_pids.append(pid)
            last_pid = pid
        if last_pid:
            pid_to_lines.setdefault(last_pid, []).append(d)

    for pid in seen_pids[-3:]:
        for d in pid_to_lines[pid]:
            if d.get("type") != "user": continue
            for c in d.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    assert len(str(c.get("content", ""))) == 5000

    for pid in seen_pids[:-3]:
        for d in pid_to_lines[pid]:
            if d.get("type") != "user": continue
            for c in d.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    assert "truncated" in str(c.get("content", ""))


def test_truncate_returns_zero_when_few_prompts(tmp_path: Path):
    """≤ keep_recent_prompts 个 prompt → 不动。"""
    from robot.games._session_paths import (
        truncate_stale_tool_results, per_game_session_jsonl,
    )
    game_dir = tmp_path / "ai_truncate_few"
    game_dir.mkdir()
    sid = "sid-truncate-3"
    active = per_game_session_jsonl(game_dir, sid)
    _make_jsonl_with_reads(active, n_prompts=2, reads_per_prompt=2, read_content_size=3000)

    before = active.read_bytes()
    n = truncate_stale_tool_results(game_dir, sid, keep_recent_prompts=3)
    assert n == 0
    assert active.read_bytes() == before


def test_truncate_does_not_touch_edit_results(tmp_path: Path):
    """只截 Read 的 tool_result；Edit/Grep 的 result 即使在老 prompt 也不动。"""
    import uuid as _uuid
    from robot.games._session_paths import (
        truncate_stale_tool_results, per_game_session_jsonl,
    )
    game_dir = tmp_path / "ai_truncate_edit_safe"
    game_dir.mkdir()
    sid = "sid-truncate-4"
    active = per_game_session_jsonl(game_dir, sid)
    active.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    last_uuid = None
    long_edit_result = "edit ok " * 500
    for p in range(5):
        pid = str(_uuid.uuid4())
        u_uid = str(_uuid.uuid4())
        lines.append(json.dumps({
            "type": "user", "parentUuid": last_uuid, "promptId": pid, "uuid": u_uid,
            "message": {"role": "user", "content": f"p{p}"},
        }) + "\n")
        last_uuid = u_uid
        for tname, tu_id in [("Read", f"r_{p}"), ("Edit", f"e_{p}")]:
            a_uid = str(_uuid.uuid4())
            lines.append(json.dumps({
                "type": "assistant", "parentUuid": last_uuid, "uuid": a_uid,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tu_id, "name": tname, "input": {}},
                ]},
            }) + "\n")
            last_uuid = a_uid
            r_uid = str(_uuid.uuid4())
            lines.append(json.dumps({
                "type": "user", "parentUuid": last_uuid, "uuid": r_uid,
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu_id,
                     "content": long_edit_result if tname == "Edit" else "x" * 4000},
                ]},
            }) + "\n")
            last_uuid = r_uid
    active.write_text("".join(lines), encoding="utf-8")

    n = truncate_stale_tool_results(game_dir, sid, keep_recent_prompts=3)
    assert n == 2

    for line in active.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("type") != "user": continue
        for c in d.get("message", {}).get("content", []) or []:
            if not isinstance(c, dict): continue
            if c.get("type") != "tool_result": continue
            tid = c.get("tool_use_id", "")
            if tid.startswith("e_"):
                assert "edit ok" in str(c.get("content", ""))


def test_truncate_preserves_uuid_chain(tmp_path: Path):
    """uuid / parentUuid 链不动。"""
    from robot.games._session_paths import (
        truncate_stale_tool_results, per_game_session_jsonl,
    )
    game_dir = tmp_path / "ai_truncate_chain"
    game_dir.mkdir()
    sid = "sid-truncate-5"
    active = per_game_session_jsonl(game_dir, sid)
    _make_jsonl_with_reads(active, n_prompts=5, reads_per_prompt=2, read_content_size=3000)

    pre_chain = []
    for line in active.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        pre_chain.append((d.get("uuid"), d.get("parentUuid"), d.get("type")))

    truncate_stale_tool_results(game_dir, sid, keep_recent_prompts=3)

    post_chain = []
    for line in active.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        post_chain.append((d.get("uuid"), d.get("parentUuid"), d.get("type")))

    assert pre_chain == post_chain


# Note: 集成 test_run_agent_calls_truncate_before_compact 已删除。
# truncate_stale_tool_results 的 wiring 在 _run_claude_agent 已撤回（实测在
# compact + min_prompts_kept=1 配置下 active 稳定 1 prompt，truncate 永远没
# 工作目标）。helper 函数本身保留，未来 compact 阈值放宽时可重新启用。
# 上面 5 个 unit test 已充分覆盖 helper 行为。
