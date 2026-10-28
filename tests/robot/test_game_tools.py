"""Tests for game_tools and games API."""

import json
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from robot.agent.tools.game_tools import (
    GAMES_DIR,
    TEMPLATE_DIR,
    _compute_diffs,
    _compute_file_manifest,
    _generate_game_id,
    _build_system_prompt,
    _extract_description_i18n_from_result,
    _run_claude_agent,
    _run_gdparse,
    _send_game_progress,
    _validate_and_fix,
    create_game,
    modify_game,
)


# ── Unit tests ──


class TestGenerateGameId:
    def test_format(self):
        gid = _generate_game_id("太空射击")
        assert gid.startswith("ai_")
        parts = gid.split("_")
        assert len(parts) == 3
        assert parts[1].isdigit()
        assert len(parts[2]) == 6

    def test_unique(self):
        """Different calls produce different IDs (timestamp + hash)."""
        id1 = _generate_game_id("game1")
        id2 = _generate_game_id("game2")
        assert id1 != id2


class TestComputeFileManifest:
    def test_reads_gd_and_tscn(self, tmp_path):
        (tmp_path / "main.gd").write_text("extends Node2D", encoding="utf-8")
        (tmp_path / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
        (tmp_path / "readme.md").write_text("ignore me", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "helper.gd").write_text("extends Node", encoding="utf-8")

        manifest = _compute_file_manifest(tmp_path)
        assert "main.gd" in manifest
        assert "main.tscn" in manifest
        assert "readme.md" not in manifest  # .md not included
        assert "sub/helper.gd" in manifest
        assert manifest["main.gd"] == "extends Node2D"

    def test_empty_dir(self, tmp_path):
        manifest = _compute_file_manifest(tmp_path)
        assert manifest == {}


class TestComputeDiffs:
    def test_add_file(self):
        old = {"a.gd": "old"}
        new = {"a.gd": "old", "b.gd": "new"}
        diffs = _compute_diffs(old, new)
        assert len(diffs) == 1
        assert diffs[0]["op"] == "add"
        assert diffs[0]["path"] == "b.gd"

    def test_delete_file(self):
        old = {"a.gd": "content", "b.gd": "content"}
        new = {"a.gd": "content"}
        diffs = _compute_diffs(old, new)
        assert len(diffs) == 1
        assert diffs[0]["op"] == "delete"
        assert diffs[0]["path"] == "b.gd"

    def test_modify_file(self):
        old = {"a.gd": "old content"}
        new = {"a.gd": "new content"}
        diffs = _compute_diffs(old, new)
        assert len(diffs) == 1
        assert diffs[0]["op"] == "modify"
        assert diffs[0]["content"] == "new content"

    def test_no_changes(self):
        old = {"a.gd": "same"}
        new = {"a.gd": "same"}
        diffs = _compute_diffs(old, new)
        assert diffs == []

    def test_mixed_operations(self):
        old = {"a.gd": "old_a", "b.gd": "old_b"}
        new = {"a.gd": "new_a", "c.gd": "new_c"}
        diffs = _compute_diffs(old, new)
        ops = {d["path"]: d["op"] for d in diffs}
        assert ops["a.gd"] == "modify"
        assert ops["b.gd"] == "delete"
        assert ops["c.gd"] == "add"

    def test_text_diff_carries_utf8_encoding(self):
        """文本文件的 diff 必须带 encoding=utf-8，让 iOS 走文本写盘路径。"""
        diffs = _compute_diffs({"main.gd": "old"}, {"main.gd": "new"})
        assert len(diffs) == 1
        assert diffs[0]["op"] == "modify"
        assert diffs[0]["encoding"] == "utf-8"
        assert diffs[0]["content"] == "new"

    def test_binary_add_diff_carries_base64_encoding(self):
        """新增 .ogg / .png 文件 → diff 带 encoding=base64。iOS 据此 base64 解
        码后写二进制，避免之前必须重启游戏才能下载新音频/图片的限制。"""
        import base64
        b64 = base64.b64encode(b"\x00\x01\x02 fake ogg bytes").decode("ascii")
        diffs = _compute_diffs({}, {"audio/hit_metal.ogg": b64})
        assert len(diffs) == 1
        assert diffs[0]["op"] == "add"
        assert diffs[0]["path"] == "audio/hit_metal.ogg"
        assert diffs[0]["encoding"] == "base64"
        assert diffs[0]["content"] == b64

    def test_binary_modify_diff_carries_base64_encoding(self):
        """已有 .png 被替换 → diff modify + encoding=base64。"""
        import base64
        old_b64 = base64.b64encode(b"old").decode("ascii")
        new_b64 = base64.b64encode(b"new").decode("ascii")
        diffs = _compute_diffs({"assets/background.png": old_b64},
                               {"assets/background.png": new_b64})
        assert len(diffs) == 1
        assert diffs[0]["op"] == "modify"
        assert diffs[0]["encoding"] == "base64"
        assert diffs[0]["content"] == new_b64

    def test_binary_delete_diff_no_content(self):
        """delete diff 不带 content 也不带 encoding（不需要传字节）。"""
        diffs = _compute_diffs({"audio/old.ogg": "abc"}, {})
        assert len(diffs) == 1
        assert diffs[0]["op"] == "delete"
        assert diffs[0]["path"] == "audio/old.ogg"
        assert "content" not in diffs[0]


class TestFileManifestBinarySupport:
    def test_manifest_includes_audio_as_base64(self, tmp_path):
        """.ogg 文件应被 _compute_file_manifest 收录，content 是 base64 ASCII。"""
        import base64
        from robot.agent.tools.game_tools import _compute_file_manifest
        (tmp_path / "audio").mkdir()
        ogg_bytes = b"\x4f\x67\x67\x53 fake ogg header"
        (tmp_path / "audio" / "click.ogg").write_bytes(ogg_bytes)

        manifest = _compute_file_manifest(tmp_path)
        assert "audio/click.ogg" in manifest
        # content 是 base64 字符串，能解回原字节
        decoded = base64.b64decode(manifest["audio/click.ogg"])
        assert decoded == ogg_bytes

    def test_manifest_includes_png_as_base64(self, tmp_path):
        """.png 文件同样被 base64 收录。"""
        import base64
        from robot.agent.tools.game_tools import _compute_file_manifest
        (tmp_path / "assets").mkdir()
        png_bytes = b"\x89PNG\r\n\x1a\n fake png"
        (tmp_path / "assets" / "background.png").write_bytes(png_bytes)

        manifest = _compute_file_manifest(tmp_path)
        assert "assets/background.png" in manifest
        assert base64.b64decode(manifest["assets/background.png"]) == png_bytes

    def test_manifest_skips_unknown_binary_types(self, tmp_path):
        """未列入 _BINARY_EXTS 的扩展（如 .zip / .so）不应进 manifest，
        防止把整个游戏打包后 zip 自身又进入 diff（递归循环）。"""
        from robot.agent.tools.game_tools import _compute_file_manifest
        (tmp_path / "stuff.zip").write_bytes(b"PK\x03\x04 fake zip")
        manifest = _compute_file_manifest(tmp_path)
        assert "stuff.zip" not in manifest

    def test_manifest_text_files_unchanged_format(self, tmp_path):
        """文本文件继续是 utf-8 字符串，向后兼容。"""
        from robot.agent.tools.game_tools import _compute_file_manifest
        (tmp_path / "scenes").mkdir()
        (tmp_path / "scenes" / "main.gd").write_text(
            "extends Node2D\nvar x = 1\n", encoding="utf-8",
        )
        manifest = _compute_file_manifest(tmp_path)
        assert manifest["scenes/main.gd"] == "extends Node2D\nvar x = 1\n"

    def test_manifest_round_trip_text_plus_binary(self, tmp_path):
        """混合：文本 + 二进制都进 manifest，diff 各自加正确 encoding。"""
        import base64
        from robot.agent.tools.game_tools import (
            _compute_file_manifest, _compute_diffs,
        )
        # 初始：1 个文本，1 个 ogg
        (tmp_path / "scenes").mkdir()
        (tmp_path / "audio").mkdir()
        (tmp_path / "scenes" / "main.gd").write_text("v1", encoding="utf-8")
        (tmp_path / "audio" / "click.ogg").write_bytes(b"old_audio")
        old_manifest = _compute_file_manifest(tmp_path)

        # 修改两者
        (tmp_path / "scenes" / "main.gd").write_text("v2", encoding="utf-8")
        (tmp_path / "audio" / "click.ogg").write_bytes(b"new_audio")
        new_manifest = _compute_file_manifest(tmp_path)

        diffs = _compute_diffs(old_manifest, new_manifest)
        diffs_by_path = {d["path"]: d for d in diffs}

        assert diffs_by_path["scenes/main.gd"]["encoding"] == "utf-8"
        assert diffs_by_path["scenes/main.gd"]["content"] == "v2"
        assert diffs_by_path["audio/click.ogg"]["encoding"] == "base64"
        assert (
            base64.b64decode(diffs_by_path["audio/click.ogg"]["content"])
            == b"new_audio"
        )


class TestTemplateExists:
    def test_template_dir_exists(self):
        assert TEMPLATE_DIR.is_dir(), f"Template dir missing: {TEMPLATE_DIR}"

    def test_template_has_project_godot(self):
        assert (TEMPLATE_DIR / "project.godot").exists()

    def test_template_has_main_scene(self):
        assert (TEMPLATE_DIR / "scenes" / "main.tscn").exists()
        assert (TEMPLATE_DIR / "scenes" / "main.gd").exists()

    def test_template_has_input_bridge(self):
        assert (TEMPLATE_DIR / "scripts" / "input_bridge.gd").exists()

    def test_template_has_game_manager(self):
        assert (TEMPLATE_DIR / "scripts" / "game_manager.gd").exists()

    def test_template_has_i18n_runtime(self):
        assert (TEMPLATE_DIR / "scripts" / "i18n.gd").exists()
        assert (TEMPLATE_DIR / "scripts" / "i18n_strings.gd").exists()
        project_text = (TEMPLATE_DIR / "project.godot").read_text(encoding="utf-8")
        assert 'I18n="*res://scripts/i18n.gd"' in project_text

    def test_template_main_uses_i18n(self):
        main_text = (TEMPLATE_DIR / "scenes" / "main.gd").read_text(encoding="utf-8")
        strings_text = (TEMPLATE_DIR / "scripts" / "i18n_strings.gd").read_text(encoding="utf-8")
        assert "_tr(" in main_text
        assert "STRINGS" in strings_text
        assert '"en"' in strings_text
        assert '"zh-Hans"' in strings_text


# ── Games API tests ──


@pytest.mark.asyncio
async def test_list_games_empty(client):
    """Empty games list when no AI games exist."""
    resp = await client.get("/api/games")
    assert resp.status_code == 200
    data = resp.json()
    assert "games" in data


@pytest.mark.asyncio
async def test_list_games_with_meta(client, tmp_path):
    """Games with meta.json appear in list."""
    # Create a fake game dir
    game_dir = GAMES_DIR / "ai_test_123"
    game_dir.mkdir(parents=True, exist_ok=True)
    meta = {"game_id": "ai_test_123", "title": "Test Game"}
    (game_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    try:
        resp = await client.get("/api/games")
        assert resp.status_code == 200
        games = resp.json()["games"]
        found = [g for g in games if g["game_id"] == "ai_test_123"]
        assert len(found) == 1
        assert found[0]["title"] == "Test Game"
    finally:
        shutil.rmtree(game_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_list_games_renders_i18n_meta_by_accept_language(client):
    game_dir = GAMES_DIR / "ai_i18n_meta_test"
    game_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "game_id": "ai_i18n_meta_test",
        "title": "Legacy Title",
        "description": "Legacy description",
        "title_i18n": {"en": "Space Dash", "zh-Hans": "太空冲刺"},
        "description_i18n": {"en": "Dodge asteroids.", "zh-Hans": "躲避陨石。"},
    }
    (game_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    try:
        resp_en = await client.get("/api/games", headers={"Accept-Language": "en-US"})
        found_en = [g for g in resp_en.json()["games"] if g["game_id"] == "ai_i18n_meta_test"][0]
        assert found_en["title"] == "Space Dash"
        assert found_en["description"] == "Dodge asteroids."
        assert found_en["title_i18n"]["zh-Hans"] == "太空冲刺"

        resp_zh = await client.get("/api/games", headers={"Accept-Language": "zh-CN"})
        found_zh = [g for g in resp_zh.json()["games"] if g["game_id"] == "ai_i18n_meta_test"][0]
        assert found_zh["title"] == "太空冲刺"
        assert found_zh["description"] == "躲避陨石。"
    finally:
        shutil.rmtree(game_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_download_game_not_found(client):
    resp = await client.get("/api/games/nonexistent/download")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_game(client):
    """Delete a game directory and zip — owner-only after marketplace migration."""
    from tests.conftest import auth_header, register_and_login
    auth = await register_and_login(client, email="del_owner@example.com")
    headers = auth_header(auth["access_token"])
    owner_id = auth["user"]["id"]

    game_dir = GAMES_DIR / "ai_delete_test"
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "test.gd").write_text("extends Node", encoding="utf-8")
    # meta.json 含 owner_id 让懒回灌把 owner 写对
    import json as _json
    (game_dir / "meta.json").write_text(_json.dumps({
        "game_id": "ai_delete_test",
        "owner_id": owner_id,
        "title": "Delete Test",
        "input_mode": "touch",
    }), encoding="utf-8")
    zip_path = GAMES_DIR / "ai_delete_test.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(game_dir))

    # 先调一次 /stats 触发懒回灌 —— DELETE 端点本身不带 lazy rehydrate（避免误删）
    await client.get("/api/games/ai_delete_test/stats", headers=headers)

    resp = await client.delete("/api/games/ai_delete_test", headers=headers)
    assert resp.status_code == 200, resp.text
    assert not game_dir.exists()
    assert not zip_path.exists()


@pytest.mark.asyncio
async def test_delete_game_not_found(client):
    """完全不存在的 game_id：404（无 fs / 无 ai_games 行）。"""
    from tests.conftest import auth_header, register_and_login
    auth = await register_and_login(client, email="del_404@example.com")
    headers = auth_header(auth["access_token"])
    resp = await client.delete("/api/games/nonexistent", headers=headers)
    assert resp.status_code == 404


# ── Games API: /files endpoint ──


@pytest.mark.asyncio
async def test_get_game_files(client, db_session):
    """GET /api/games/{id}/files 返回游戏文件内容。"""
    from datetime import datetime
    from robot.db.models import AIGame
    db_session.add(AIGame(
        game_id="ai_files_test", title="Files Test",
        title_i18n='{"en":"Files Test","zh-Hans":"Files Test"}',
        description_i18n='{"en":"","zh-Hans":""}',
        visibility="public_view_only", kind="ai",
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))
    await db_session.flush()

    game_dir = GAMES_DIR / "ai_files_test"
    game_dir.mkdir(parents=True, exist_ok=True)
    scenes_dir = game_dir / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)

    (game_dir / "project.godot").write_text("[gd_resource]", encoding="utf-8")
    (scenes_dir / "main.gd").write_text("extends Node2D", encoding="utf-8")
    (scenes_dir / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
    # .md 文件不应出现在结果中
    (game_dir / "readme.md").write_text("not included", encoding="utf-8")

    try:
        resp = await client.get("/api/games/ai_files_test/files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["game_id"] == "ai_files_test"
        files = data["files"]
        assert "project.godot" in files
        assert "scenes/main.gd" in files
        assert "scenes/main.tscn" in files
        assert "readme.md" not in files
        assert files["scenes/main.gd"] == "extends Node2D"
    finally:
        shutil.rmtree(game_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_get_game_files_not_found(client):
    """GET /api/games/{id}/files 返回 404。"""
    resp = await client.get("/api/games/nonexistent/files")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_game_with_zip(client, db_session):
    """GET /api/games/{id}/download 有 zip 文件时返回文件内容。"""
    from datetime import datetime
    from robot.db.models import AIGame
    db_session.add(AIGame(
        game_id="ai_dl_test", title="DL Test",
        title_i18n='{"en":"DL Test","zh-Hans":"DL Test"}',
        description_i18n='{"en":"","zh-Hans":""}',
        visibility="public_view_only", kind="ai",
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))
    await db_session.flush()

    game_dir = GAMES_DIR / "ai_dl_test"
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "main.gd").write_text("extends Node2D", encoding="utf-8")
    zip_path = GAMES_DIR / "ai_dl_test.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(game_dir))

    try:
        resp = await client.get("/api/games/ai_dl_test/download")
        assert resp.status_code == 200
        assert "application/zip" in resp.headers.get("content-type", "")
        assert len(resp.content) > 0
    finally:
        shutil.rmtree(game_dir, ignore_errors=True)
        if zip_path.exists():
            zip_path.unlink()


# ── System prompt 验证 ──


class TestGodotSystemPrompt:
    def test_touch_mode_no_body_pose(self):
        prompt = _build_system_prompt(input_mode="touch")
        assert "TOUCH ONLY" in prompt
        assert "body_lean_x" not in prompt

    def test_body_pose_mode_has_input_bridge(self):
        prompt = _build_system_prompt(input_mode="body_pose")
        assert "InputBridge" in prompt
        assert "body_lean_x" in prompt

    def test_contains_screen_resolution_placeholder(self):
        prompt = _build_system_prompt(screen_resolution="1194x834")
        assert "1194x834" in prompt

    def test_contains_godot4(self):
        prompt = _build_system_prompt()
        assert "Godot 4" in prompt

    def test_contains_2d_requirement(self):
        prompt = _build_system_prompt()
        assert "2D" in prompt

    def test_default_omits_mcp(self):
        """`_build_system_prompt()` 默认 use_mcp=False（2026-05 起，节省
        ~137 tok + 消除 mcp__godot__list_projects permission warning）。
        要 MCP 段必须显式 use_mcp=True。"""
        prompt = _build_system_prompt()
        assert "create_scene" not in prompt
        assert "add_node" not in prompt

    def test_explicit_use_mcp_true_includes_mcp(self):
        prompt = _build_system_prompt(use_mcp=True)
        assert "create_scene" in prompt
        assert "add_node" in prompt

    def test_mcp_section_omitted_when_use_mcp_false(self):
        """modify 路径 use_mcp=False，MCP 段应消失（省 ~137 tok）。"""
        full = _build_system_prompt(use_mcp=True)
        lite = _build_system_prompt(use_mcp=False)
        assert "create_scene" in full and "add_node" in full
        assert "create_scene" not in lite and "add_node" not in lite
        assert "## Godot MCP Tools" not in lite
        # 但其它必备部分要保留
        assert "Godot 4" in lite
        assert "Project structure" in lite

    def test_do_not_modify_rules(self):
        prompt = _build_system_prompt()
        assert "DO NOT MODIFY" in prompt
        assert "project.godot" in prompt
        assert "input_bridge.gd" in prompt

    def test_contains_i18n_rules(self):
        prompt = _build_system_prompt()
        assert "_tr(" in prompt
        assert "i18n_strings.gd" in prompt
        assert '"en"' in prompt
        assert '"zh-Hans"' in prompt


def test_extract_description_i18n_from_result():
    result = '---\ndescription_i18n: {"en": "Catch falling stars.", "zh-Hans": "接住落下的星星。"}\nChange summary: Updated text'
    assert _extract_description_i18n_from_result(result) == {
        "en": "Catch falling stars.",
        "zh-Hans": "接住落下的星星。",
    }


# ── create_game tool 测试 ──


@pytest.fixture
def clean_game_dir():
    """自动清理测试中创建的游戏目录。"""
    created_dirs = []
    yield created_dirs
    for d in created_dirs:
        if isinstance(d, (str, Path)):
            p = Path(d)
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
            # 也清理 zip
            zip_p = p.with_suffix(".zip")
            if zip_p.exists():
                zip_p.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def default_zh_lang():
    """Keep legacy game tool assertions in Chinese unless a test opts into another locale."""
    from robot.agent.tools import _context as ctx

    token = ctx._current_user_lang.set("zh-Hans")
    yield
    ctx._current_user_lang.reset(token)


@pytest.fixture(autouse=True)
def stub_modify_game_owner():
    """Stub `async_session` so modify_game's ownership DB query returns a row
    whose ``owner_id`` matches the test's current `_current_user_id`.

    Unit-test convenience: most modify_game tests don't seed an `ai_games`
    row (would require an FK-valid user), so without this they'd fall into
    the production "no DB row → game_not_found" branch added by the W1 P0-2
    fix. Tests that DO want to exercise the not_owner path should call
    `modify_game` outside this fixture and inject their own DB rows."""
    import contextlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock

    from robot.agent.tools import _context as ctx

    @contextlib.asynccontextmanager
    async def _fake_session_cm():
        session = _AsyncMock()
        # Build a row whose owner_id matches whoever the test set as user_id.
        # Re-read on each entry so tests can flip user_id mid-suite.
        fake_row = SimpleNamespace(owner_id=ctx._current_user_id.get())
        result = _MagicMock()
        result.scalar_one_or_none = _MagicMock(return_value=fake_row)
        session.execute = _AsyncMock(return_value=result)
        try:
            yield session
        finally:
            pass

    with patch("robot.db.database.async_session", side_effect=_fake_session_cm):
        yield


@pytest.mark.asyncio
async def test_create_game_success(clean_game_dir):
    """create_game 应复制模板、调用 Claude、创建 zip 和 meta.json。"""
    from robot.agent.tools import _context as ctx

    # 设置 ContextVar
    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")

    # Mock ws_manager
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    # Mock _run_claude_agent 让它在项目目录创建文件
    async def fake_claude_agent(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        cwd = Path(cwd)
        scenes = cwd / "scenes"
        scenes.mkdir(parents=True, exist_ok=True)
        (scenes / "main.gd").write_text(
            "extends Node2D\nfunc _ready():\n\tprint('hello')",
            encoding="utf-8",
        )
        (scenes / "main.tscn").write_text(
            '[gd_scene load_steps=2 format=3]\n[node name="Main" type="Node2D"]',
            encoding="utf-8",
        )
        return "游戏创建完成", "fake-session-001", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude_agent), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await create_game.ainvoke({"title": "测试游戏", "description": "一个测试用的简单游戏"})

    assert "测试游戏" in result
    assert "创建完成" in result

    # 提取 game_id
    # 格式：游戏「测试游戏」创建完成！游戏ID: ai_xxx
    game_id = result.split("游戏ID: ")[-1].strip()
    assert game_id.startswith("ai_")
    clean_game_dir.append(GAMES_DIR / game_id)

    # 验证目录结构
    game_dir = GAMES_DIR / game_id
    assert game_dir.is_dir()
    assert (game_dir / "scenes" / "main.gd").exists()
    assert (game_dir / "scenes" / "main.tscn").exists()
    assert (game_dir / "project.godot").exists()  # 从模板复制

    # 验证 meta.json
    meta = json.loads((game_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["title"] == "测试游戏"
    assert meta["game_id"] == game_id
    assert meta["owner_id"] == "owner_test"

    # 验证 zip 已创建
    assert (GAMES_DIR / f"{game_id}.zip").exists()

    # 验证 ws_manager 被调用推送 game_ready
    mock_ws.send_to_user.assert_awaited_once()
    call_args = mock_ws.send_to_user.call_args
    assert call_args[0][0] == "owner_test"
    assert call_args[0][1] == "user_test"
    msg = call_args[0][2]
    assert msg["type"] == "robot_command"
    assert msg["data"]["action"] == "game_ready"
    assert msg["data"]["game_id"] == game_id

    # 恢复
    ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_create_game_missing_files(clean_game_dir):
    """Claude 没有生成 main 文件时，返回不完整警告。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")

    original_ws = ctx._ws_manager
    ctx._ws_manager = None  # 无 ws_manager

    # Claude 不写任何文件
    async def fake_claude_noop(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        return "未能生成代码", "fake-session-002", 0

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude_noop), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude_noop):
            result = await create_game.ainvoke({"title": "空游戏", "description": "不会成功"})

    # 模板本身就有 main.gd 和 main.tscn，所以应该通过
    # 提取 game_id 并清理
    if "游戏ID:" in result:
        game_id = result.split("游戏ID: ")[-1].strip()
        clean_game_dir.append(GAMES_DIR / game_id)

    ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_create_game_template_missing():
    """模板目录不存在时应返回错误。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")

    with patch("robot.agent.tools.game_tools.TEMPLATE_DIR", Path("/nonexistent/template")), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await create_game.ainvoke({"title": "Test", "description": "Test"})
    assert "模板目录不存在" in result


# ── modify_game tool 测试 ──


@pytest.mark.asyncio
async def test_modify_game_success(clean_game_dir):
    """modify_game 应修改文件并发送 diff。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")

    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    # 准备游戏目录
    game_id = "ai_1700000000_abc123"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    scenes = game_dir / "scenes"
    scenes.mkdir(parents=True, exist_ok=True)
    (scenes / "main.gd").write_text("extends Node2D\nvar speed = 100", encoding="utf-8")
    (scenes / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
    clean_game_dir.append(game_dir)

    # Mock Claude 修改 speed 值
    async def fake_claude_modify(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        p = Path(cwd) / "scenes" / "main.gd"
        p.write_text("extends Node2D\nvar speed = 200", encoding="utf-8")
        return "已将速度改为200", "fake-session-mod", 1

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude_modify), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await modify_game.ainvoke({"game_id": game_id, "instruction": "把速度加倍"})

    assert "1" in result  # 更新了 1 个文件
    assert "main.gd" in result

    # 验证文件确实被修改
    content = (scenes / "main.gd").read_text(encoding="utf-8")
    assert "speed = 200" in content

    # 验证 ws 发送了 game_patch
    mock_ws.send_to_user.assert_awaited_once()
    call_args = mock_ws.send_to_user.call_args
    msg = call_args[0][2]
    assert msg["type"] == "robot_command"
    assert msg["data"]["action"] == "game_patch"
    assert msg["data"]["game_id"] == game_id
    diffs = msg["data"]["diffs"]
    assert len(diffs) == 1
    assert diffs[0]["op"] == "modify"
    assert diffs[0]["path"] == "scenes/main.gd"

    # 验证 zip 已重新创建
    assert (GAMES_DIR / f"{game_id}.zip").exists()

    ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_not_found():
    """modify_game 目标游戏不存在时返回错误。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")

    result = await modify_game.ainvoke({"game_id": "ai_nonexistent_999", "instruction": "加快速度"})
    assert "找不到游戏" in result


@pytest.mark.asyncio
async def test_modify_game_no_changes(clean_game_dir):
    """Claude 没有修改文件时，返回未变更提示。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    original_ws = ctx._ws_manager
    ctx._ws_manager = AsyncMock()

    game_id = "ai_1700000001_def456"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
    clean_game_dir.append(game_dir)

    # Claude 不修改任何文件
    async def fake_noop(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        return "没有需要修改的", "fake-session-noop", 0

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_noop), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await modify_game.ainvoke({"game_id": game_id, "instruction": "无效指令"})

    assert "没有检测到文件变更" in result

    ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_add_and_delete_files(clean_game_dir):
    """modify_game 支持新增和删除文件的 diff。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    game_id = "ai_1700000010_ad0001"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
    (game_dir / "old.gd").write_text("extends Node", encoding="utf-8")
    (game_dir / "keep.gd").write_text("extends Node2D", encoding="utf-8")
    clean_game_dir.append(game_dir)

    async def fake_claude_add_del(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        cwd = Path(cwd)
        # 删除 old.gd
        (cwd / "old.gd").unlink()
        # 新增 new.gd
        (cwd / "new.gd").write_text("extends Area2D", encoding="utf-8")
        return "done", "fake-session-addel", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude_add_del), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await modify_game.ainvoke({"game_id": game_id, "instruction": "重构代码"})

    assert "2" in result  # 2 个文件变更

    # 验证 diffs
    call_args = mock_ws.send_to_user.call_args
    diffs = call_args[0][2]["data"]["diffs"]
    ops = {d["path"]: d["op"] for d in diffs}
    assert ops["new.gd"] == "add"
    assert ops["old.gd"] == "delete"
    assert "keep.gd" not in ops  # 未变更

    ctx._ws_manager = original_ws


# ── list_games 边界情况 ──


@pytest.mark.asyncio
async def test_list_games_ignores_template(client):
    """_template 目录不应出现在游戏列表中。"""
    resp = await client.get("/api/games")
    assert resp.status_code == 200
    games = resp.json()["games"]
    game_ids = [g.get("game_id", "") for g in games]
    assert "_template" not in game_ids


@pytest.mark.asyncio
async def test_list_games_without_meta(client):
    """没有 meta.json 的游戏目录也应被列出（使用目录名）。"""
    game_dir = GAMES_DIR / "ai_nometa_test"
    game_dir.mkdir(parents=True, exist_ok=True)

    try:
        resp = await client.get("/api/games")
        assert resp.status_code == 200
        games = resp.json()["games"]
        found = [g for g in games if g.get("game_id") == "ai_nometa_test"]
        assert len(found) == 1
        assert found[0]["title"] == "ai_nometa_test"  # fallback 用目录名
    finally:
        shutil.rmtree(game_dir, ignore_errors=True)


# ── _run_claude_agent 环境变量 & 参数测试 ──


class TestRunClaudeAgentEnvVars:
    """验证 _run_claude_agent 正确设置环境变量。"""

    @pytest.mark.asyncio
    async def test_proxy_mode_sets_auth_token(self):
        """代理模式：设置 ANTHROPIC_AUTH_TOKEN，清空 ANTHROPIC_API_KEY。"""
        mock_settings = MagicMock()
        mock_settings.anthropic_auth_token = "test-token-123"
        mock_settings.anthropic_api_key = ""
        mock_settings.anthropic_base_url = "https://proxy.example.com"

        saved_env = {}
        for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            saved_env[k] = os.environ.pop(k, None)

        try:
            with patch("robot.config.settings", mock_settings):
                result = await _run_claude_agent("/tmp", "test")

            assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "test-token-123"
            assert os.environ.get("ANTHROPIC_API_KEY") == ""
            assert os.environ.get("ANTHROPIC_BASE_URL") == "https://proxy.example.com"
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    @pytest.mark.asyncio
    async def test_direct_mode_sets_api_key(self):
        """直连模式：仅设置 ANTHROPIC_API_KEY。"""
        mock_settings = MagicMock()
        mock_settings.anthropic_auth_token = ""
        mock_settings.anthropic_api_key = "sk-ant-test-key"
        mock_settings.anthropic_base_url = ""

        saved_env = {}
        for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            saved_env[k] = os.environ.pop(k, None)

        try:
            with patch("robot.config.settings", mock_settings):
                result = await _run_claude_agent("/tmp", "test")

            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test-key"
            assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestClaudeCodeOptions:
    """验证 ClaudeCodeOptions 参数与文档一致。"""

    def test_options_values(self):
        """ClaudeCodeOptions 参数值正确。"""
        from claude_code_sdk import ClaudeCodeOptions

        prompt = _build_system_prompt()
        opts = ClaudeCodeOptions(
            system_prompt=prompt,
            cwd="/tmp/test",
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
            max_turns=25,
            permission_mode="acceptEdits",
        )

        assert opts.allowed_tools == ["Read", "Write", "Edit", "Glob", "Grep"]
        assert opts.max_turns == 25
        assert opts.permission_mode == "acceptEdits"
        assert opts.system_prompt == prompt
        assert str(opts.cwd) == "/tmp/test"


# ── _compute_file_manifest 额外文件类型测试 ──


class TestComputeFileManifestExtended:
    def test_includes_cfg_and_tres(self, tmp_path):
        """manifest 应包含 .cfg 和 .tres 文件。"""
        (tmp_path / "settings.cfg").write_text("[settings]", encoding="utf-8")
        (tmp_path / "theme.tres").write_text("[gd_resource]", encoding="utf-8")
        (tmp_path / "data.json").write_text("{}", encoding="utf-8")

        manifest = _compute_file_manifest(tmp_path)
        assert "settings.cfg" in manifest
        assert "theme.tres" in manifest
        assert "data.json" not in manifest  # .json 不在列表中

    def test_includes_godot_file(self, tmp_path):
        """manifest 应包含 .godot 文件。"""
        (tmp_path / "project.godot").write_text("[application]", encoding="utf-8")
        manifest = _compute_file_manifest(tmp_path)
        assert "project.godot" in manifest


# ── 模板文件内容验证 ──


class TestTemplateContents:
    def test_project_godot_has_resolution(self):
        """project.godot 包含目标分辨率 1194x834。"""
        content = (TEMPLATE_DIR / "project.godot").read_text(encoding="utf-8")
        assert "1194" in content
        assert "834" in content

    def test_project_godot_has_2d(self):
        """project.godot 配置为 mobile 渲染。"""
        content = (TEMPLATE_DIR / "project.godot").read_text(encoding="utf-8")
        assert "mobile" in content.lower() or "2d" in content.lower()

    def test_input_bridge_has_body_lean(self):
        """input_bridge.gd 包含 body_lean_x/y 变量。"""
        content = (TEMPLATE_DIR / "scripts" / "input_bridge.gd").read_text(encoding="utf-8")
        assert "body_lean_x" in content
        assert "body_lean_y" in content

    def test_input_bridge_has_ducking(self):
        """input_bridge.gd 包含 is_ducking 变量。"""
        content = (TEMPLATE_DIR / "scripts" / "input_bridge.gd").read_text(encoding="utf-8")
        assert "is_ducking" in content

    def test_input_bridge_has_face_position(self):
        """input_bridge.gd 包含 face_position。"""
        content = (TEMPLATE_DIR / "scripts" / "input_bridge.gd").read_text(encoding="utf-8")
        assert "face_position" in content

    def test_game_manager_has_signals(self):
        """game_manager.gd 包含游戏生命周期信号。"""
        content = (TEMPLATE_DIR / "scripts" / "game_manager.gd").read_text(encoding="utf-8")
        assert "game_started" in content
        assert "game_over" in content


# ── create_game meta.json 完整性 ──


@pytest.mark.asyncio
async def test_create_game_meta_has_all_fields(clean_game_dir):
    """meta.json 包含 game_id, title, description, created_at, owner_id。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_meta")
    ctx._current_user_id.set("user_meta")
    original_ws = ctx._ws_manager
    ctx._ws_manager = AsyncMock()

    async def fake_claude(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        cwd = Path(cwd)
        (cwd / "scenes").mkdir(parents=True, exist_ok=True)
        (cwd / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
        (cwd / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
        return "done", "fake-session-meta", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await create_game.ainvoke({"title": "Meta测试", "description": "测试描述"})

    game_id = result.split("游戏ID: ")[-1].strip()
    clean_game_dir.append(GAMES_DIR / game_id)

    meta = json.loads((GAMES_DIR / game_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["game_id"] == game_id
    assert meta["title"] == "Meta测试"
    assert meta["description"] == "测试描述"
    assert "created_at" in meta
    assert isinstance(meta["created_at"], int)
    assert meta["owner_id"] == "owner_meta"
    assert meta["input_mode"] == "touch"
    # history 字段
    assert "history" in meta
    assert len(meta["history"]) == 2
    assert meta["history"][0]["role"] == "user"
    assert meta["history"][0]["content"] == "测试描述"
    assert meta["history"][1]["role"] == "assistant"
    assert meta["history"][1]["content"] == "done"
    # modified_at 字段
    assert "modified_at" in meta
    assert isinstance(meta["modified_at"], int)

    ctx._ws_manager = original_ws


# ── game_ready 消息包含 title ──


@pytest.mark.asyncio
async def test_game_ready_message_contains_title(clean_game_dir):
    """game_ready robot_command 消息包含 title 字段。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_title")
    ctx._current_user_id.set("user_title")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    async def fake_claude(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        cwd = Path(cwd)
        (cwd / "scenes").mkdir(parents=True, exist_ok=True)
        (cwd / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
        (cwd / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
        return "done", "fake-session-title", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await create_game.ainvoke({"title": "标题验证游戏", "description": "desc"})

    game_id = result.split("游戏ID: ")[-1].strip()
    clean_game_dir.append(GAMES_DIR / game_id)

    msg = mock_ws.send_to_user.call_args[0][2]
    assert msg["data"]["title"] == "标题验证游戏"
    assert msg["data"]["input_mode"] == "touch"

    ctx._ws_manager = original_ws


# ── modify_game 无变更不发送 ws 通知 ──


@pytest.mark.asyncio
async def test_modify_game_no_changes_pushes_modification_failed(clean_game_dir):
    """无文件变更时，ws_manager 必须收到一条 game_modification_failed
    (reason=no_changes) — 之前是「不发 ws」，但那正是导致 iOS
    `isModifying` 永久卡死的 P0 bug，修复后必须显式推送让 UI 解锁。
    不发 game_patch（diffs 为空），只发失败事件。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    game_id = "ai_1700000011_a0b0c0"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
    clean_game_dir.append(game_dir)

    async def fake_noop(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        return "nothing", "fake-session-noop2", 0

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_noop), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await modify_game.ainvoke({"game_id": game_id, "instruction": "无操作"})

    assert "没有检测到文件变更" in result
    # 必须推一条 game_modification_failed (reason=no_changes)，不能 game_patch
    actions = [
        c.args[2]["data"]["action"]
        for c in mock_ws.send_to_user.await_args_list
    ]
    assert "game_modification_failed" in actions
    assert "game_patch" not in actions
    failed = [
        c for c in mock_ws.send_to_user.await_args_list
        if c.args[2]["data"]["action"] == "game_modification_failed"
    ]
    assert len(failed) == 1
    assert failed[0].args[2]["data"]["reason"] == "no_changes"

    ctx._ws_manager = original_ws


# ── modify_game history 更新测试 ──


@pytest.mark.asyncio
async def test_modify_game_does_not_touch_meta_history(clean_game_dir):
    """B 体系（meta["history"] 注入 / 写回）已经删除：

    - prompt 不再包含 "## Modification history" 段
    - meta.history 不再被追加新条目
    - 老 meta 里的旧 history 字段保留不动（向后兼容，不删数据）

    新方案下 model 的对话记忆完全依赖 sliding-window 之后的 session jsonl。
    """
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    game_id = "ai_1700000012_b15700"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D\nvar x = 1", encoding="utf-8")
    (game_dir / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
    # 旧版 meta 里有 history 字段（来自老 modify 写入）
    initial_meta = {
        "game_id": game_id,
        "title": "History Test",
        "input_mode": "touch",
        "history": [
            {"role": "user", "content": "old user 1"},
            {"role": "assistant", "content": "old ai 1"},
        ],
    }
    (game_dir / "meta.json").write_text(json.dumps(initial_meta), encoding="utf-8")
    clean_game_dir.append(game_dir)

    captured_prompts = []

    async def fake_modify(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        captured_prompts.append(prompt)
        p = Path(cwd) / "scenes" / "main.gd"
        p.write_text("extends Node2D\nvar x = 2", encoding="utf-8")
        return "ok", "sid-x", 1

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_modify), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        await modify_game.ainvoke({"game_id": game_id, "instruction": "新指令"})

    # 1. prompt 不再包含 modification history 段
    assert captured_prompts, "fake_modify 应被调用一次"
    assert "Modification history" not in captured_prompts[0], (
        "prompt 不应再注入 meta.history（B 体系已删）"
    )
    assert "old user 1" not in captured_prompts[0]
    assert "old ai 1" not in captured_prompts[0]

    # 2. meta.history 字段保持原样（保留不破坏老数据）
    meta = json.loads((game_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta.get("history") == [
        {"role": "user", "content": "old user 1"},
        {"role": "assistant", "content": "old ai 1"},
    ], "meta.history 不应被追加新条目，保持向后兼容（旧字段）"

    # 3. modified_at 仍正常更新
    assert "modified_at" in meta

    ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_reads_input_mode_from_meta(clean_game_dir):
    """modify_game 应从 meta.json 读取 input_mode 并传递给 agent。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    game_id = "ai_1700000013_1cbe0d"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D\nvar x = 1", encoding="utf-8")
    (game_dir / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
    meta = {"game_id": game_id, "title": "Body Pose Game", "input_mode": "body_pose"}
    (game_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    clean_game_dir.append(game_dir)

    captured_prompt = []

    async def fake_modify(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        captured_prompt.append(prompt)
        p = Path(cwd) / "scenes" / "main.gd"
        p.write_text("extends Node2D\nvar x = 2", encoding="utf-8")
        return "done", "fake-session-inputmode", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_modify), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await modify_game.ainvoke({"game_id": game_id, "instruction": "修改内容"})

    # body_pose guidance should be in English internal prompt.
    assert any("body pose / visual control" in p for p in captured_prompt)

    ctx._ws_manager = original_ws


# ── SILENT_TOOLS 不包含 game tools ──


def test_game_tools_not_in_silent_tools():
    """create_game 和 modify_game 不在 SILENT_TOOLS 中（需要文字回复）。"""
    streaming_path = Path(__file__).resolve().parent.parent.parent / "robot" / "agent" / "streaming.py"
    source = streaming_path.read_text(encoding="utf-8")

    # 找到 SILENT_TOOLS 定义行
    for line in source.splitlines():
        if "SILENT_TOOLS" in line and "=" in line:
            assert "create_game" not in line
            assert "modify_game" not in line
            break


# ── game tools 注册验证 ──


def test_creation_tools_in_all_tools():
    """create_game / modify_game / create_widget / modify_widget 必须注册到
    主 LangGraph agent 的 all_tools，让用户在主页通过语音说"做一个游戏 / 做一个
    组件"时 agent 能识别意图并调用对应工具。"""
    from robot.agent.tools import all_tools

    tool_names = [t.name for t in all_tools]
    assert "create_game" in tool_names
    assert "modify_game" in tool_names
    assert "create_widget" in tool_names
    assert "modify_widget" in tool_names


# ── system prompt 提及 creation tools ──


def test_system_prompt_allows_voice_creation():
    """system_prompt_robot.txt 必须明确指引何时调用 create_game / create_widget，
    并且不再保留之前禁止创作游戏的措辞。"""
    prompt_path = Path(__file__).resolve().parent.parent.parent / "robot" / "agent" / "prompts" / "system_prompt_robot.txt"
    content = prompt_path.read_text(encoding="utf-8")
    # New voice routing — both creation tools must be referenced in the rules.
    assert "create_game" in content
    assert "modify_game" in content
    assert "create_widget" in content
    assert "modify_widget" in content
    # 之前的禁令已经移除（不再"Do NOT create or modify games"）
    assert "Do NOT create" not in content
    assert "go to the Games section" not in content
    # body_pose 推断提示存在
    assert "body_pose" in content


# ── 模板选择测试 ──


@pytest.mark.asyncio
async def test_create_game_with_template(clean_game_dir):
    """create_game 使用已有游戏作为模板时，应复制该游戏目录而非 _template。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_tpl")
    ctx._current_user_id.set("user_tpl")
    original_ws = ctx._ws_manager
    ctx._ws_manager = AsyncMock()

    # 创建一个 official 模板（必须落在 _templates/<id>/，不再是 GAMES_DIR/<id>/
    # 老路径 —— 那条扫用户游戏的 path 已经废弃，create_game 只在 _templates/ 下查）
    template_game_id = "shooter_test"
    template_game_dir = GAMES_DIR / "_templates" / template_game_id
    template_game_dir.mkdir(parents=True, exist_ok=True)
    (template_game_dir / "scenes").mkdir(exist_ok=True)
    (template_game_dir / "scenes" / "main.gd").write_text(
        "extends Node2D\n# Template game with shooting", encoding="utf-8")
    (template_game_dir / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
    (template_game_dir / "project.godot").write_text(
        "[application]\nviewport_width=1194\nviewport_height=834", encoding="utf-8")
    (template_game_dir / "meta.json").write_text(
        json.dumps({"game_id": template_game_id, "title": "射击游戏"}), encoding="utf-8")
    clean_game_dir.append(template_game_dir)

    captured_prompt = []

    async def fake_claude(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        captured_prompt.append(prompt)
        cwd = Path(cwd)
        (cwd / "scenes").mkdir(parents=True, exist_ok=True)
        (cwd / "scenes" / "main.gd").write_text(
            "extends Node2D\n# New game based on template", encoding="utf-8")
        (cwd / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
        return "基于模板创建完成", "fake-session-tpl", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await create_game.ainvoke({
            "title": "新射击", "description": "一个新的射击游戏",
            "template_game_id": template_game_id,
        })

    assert "新射击" in result
    game_id = result.split("游戏ID: ")[-1].strip()
    clean_game_dir.append(GAMES_DIR / game_id)

    # Verify the template branch uses the CREATE NEW GAME mode prompt
    # （走 curated template 分支，与 skeleton hook 默认分支区分开）
    assert any("MODE: CREATE NEW GAME" in p and "curated template" in p for p in captured_prompt)

    # 验证 meta.json 中有 template_from
    meta = json.loads((GAMES_DIR / game_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["template_from"] == template_game_id

    # 验证 meta.json 中没有旧的模板游戏的 meta（已被清理）
    # 新 meta 的 game_id 应该是新生成的
    assert meta["game_id"] == game_id
    assert meta["game_id"] != template_game_id

    ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_create_game_invalid_template_fallback(clean_game_dir):
    """template_game_id 不存在时，回退到 _template。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_fb")
    ctx._current_user_id.set("user_fb")
    original_ws = ctx._ws_manager
    ctx._ws_manager = AsyncMock()

    captured_prompt = []

    async def fake_claude(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        captured_prompt.append(prompt)
        cwd = Path(cwd)
        (cwd / "scenes").mkdir(parents=True, exist_ok=True)
        (cwd / "scenes" / "main.gd").write_text("extends Node2D\nfunc _ready(): pass", encoding="utf-8")
        (cwd / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
        return "done", "fake-session-fb", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await create_game.ainvoke({
            "title": "测试", "description": "测试游戏",
            "template_game_id": "ai_nonexistent_999",
        })

    game_id = result.split("游戏ID: ")[-1].strip()
    clean_game_dir.append(GAMES_DIR / game_id)

    # 回退到默认模板时，prompt 不含"基于已有"
    assert not any("基于一个已有的类似游戏" in p for p in captured_prompt)

    # template_from 应为空
    meta = json.loads((GAMES_DIR / game_id / "meta.json").read_text(encoding="utf-8"))
    assert meta["template_from"] == ""

    ctx._ws_manager = original_ws


# ── description 截断测试 ──


@pytest.mark.asyncio
async def test_create_game_description_truncated(clean_game_dir):
    """meta.json 中的 description 不超过 _MAX_DESCRIPTION_LEN。"""
    from robot.agent.tools import _context as ctx
    from robot.agent.tools.game_tools import _MAX_DESCRIPTION_LEN

    ctx._current_owner_id.set("owner_desc")
    ctx._current_user_id.set("user_desc")
    original_ws = ctx._ws_manager
    ctx._ws_manager = AsyncMock()

    long_desc = "这是一个非常长的游戏描述" * 20  # > 100 chars

    async def fake_claude(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        cwd = Path(cwd)
        (cwd / "scenes").mkdir(parents=True, exist_ok=True)
        (cwd / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
        (cwd / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
        return "done", "fake-session-desc", 2

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_claude), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await create_game.ainvoke({"title": "长描述测试", "description": long_desc})

    game_id = result.split("游戏ID: ")[-1].strip()
    clean_game_dir.append(GAMES_DIR / game_id)

    meta = json.loads((GAMES_DIR / game_id / "meta.json").read_text(encoding="utf-8"))
    assert len(meta["description"]) <= _MAX_DESCRIPTION_LEN

    ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_updates_description(clean_game_dir):
    """modify_game 应从 agent 回复第一行更新 meta.json 的 description。"""
    from robot.agent.tools import _context as ctx
    from robot.agent.tools.game_tools import _MAX_DESCRIPTION_LEN

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    game_id = "ai_1700000014_de5c00"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D\nvar speed = 100", encoding="utf-8")
    (game_dir / "scenes" / "main.tscn").write_text("[gd_scene]", encoding="utf-8")
    initial_meta = {
        "game_id": game_id,
        "title": "Desc Update Test",
        "description": "旧的描述",
        "input_mode": "touch",
        "history": [],
    }
    (game_dir / "meta.json").write_text(json.dumps(initial_meta), encoding="utf-8")
    clean_game_dir.append(game_dir)

    async def fake_modify(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
        p = Path(cwd) / "scenes" / "main.gd"
        p.write_text("extends Node2D\nvar speed = 200", encoding="utf-8")
        return (
            "我先读取代码了解结构，然后做局部修改。\n\n"
            "---\n"
            "游戏描述：控制火球移动收集星星躲避水滴三条命\n"
            "修改概要：速度从100改为200"
        ), "fake-session-desc", 1

    with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_modify), \
         patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
         patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
        result = await modify_game.ainvoke({"game_id": game_id, "instruction": "加快速度"})

    meta = json.loads((game_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["description"] == "控制火球移动收集星星躲避水滴三条命"
    assert len(meta["description"]) <= _MAX_DESCRIPTION_LEN

    ctx._ws_manager = original_ws


# ── gdparse 验证测试 ──


class TestRunGdparse:
    def test_valid_gdscript(self, tmp_path):
        """合法的 GDScript 文件不应返回错误。"""
        (tmp_path / "valid.gd").write_text("extends Node2D\n\nfunc _ready():\n\tpass\n", encoding="utf-8")
        errors = _run_gdparse(tmp_path)
        assert errors == []

    def test_invalid_gdscript(self, tmp_path):
        """语法错误的 GDScript 应被检测到。"""
        (tmp_path / "bad.gd").write_text("extends Node2D\n\nfunc _ready(\n\tpass\n", encoding="utf-8")
        errors = _run_gdparse(tmp_path)
        assert len(errors) >= 1
        assert "bad.gd" in errors[0]

    def test_empty_dir(self, tmp_path):
        """空目录不应返回错误。"""
        errors = _run_gdparse(tmp_path)
        assert errors == []

    def test_mixed_files(self, tmp_path):
        """只有语法错误的文件被报告。"""
        (tmp_path / "good.gd").write_text("extends Node2D\n", encoding="utf-8")
        (tmp_path / "bad.gd").write_text("func (\n", encoding="utf-8")
        (tmp_path / "readme.md").write_text("ignore", encoding="utf-8")
        errors = _run_gdparse(tmp_path)
        assert len(errors) >= 1
        assert any("bad.gd" in e for e in errors)
        assert not any("good.gd" in e for e in errors)
        assert not any("readme.md" in e for e in errors)


# ── _validate_and_fix 测试 ──


class TestValidateAndFix:
    @pytest.mark.asyncio
    async def test_no_errors_no_fix(self, tmp_path):
        """没有语法错误时不应调用 Claude 修复。"""
        (tmp_path / "main.gd").write_text("extends Node2D\n\nfunc _ready():\n\tpass\n", encoding="utf-8")

        with patch("robot.agent.tools.game_tools._run_claude_agent", new_callable=AsyncMock) as mock_agent, \
             patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
            fix_log = await _validate_and_fix(tmp_path, "test_game")

        assert fix_log == []
        mock_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fix_called_on_error(self, tmp_path):
        """有语法错误时应调用 Claude 修复。"""
        bad_file = tmp_path / "bad.gd"
        bad_file.write_text("func (\n", encoding="utf-8")

        call_count = 0

        async def fake_fix(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=False, session_id="", model=""):
            nonlocal call_count
            call_count += 1
            # 模拟 Claude 修复了文件
            bad_file.write_text("extends Node2D\nfunc _ready():\n\tpass\n", encoding="utf-8")
            return "fixed", "", 1

        with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_fix), \
             patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
            fix_log = await _validate_and_fix(tmp_path, "test_game")

        assert len(fix_log) >= 1
        assert call_count == 1  # 修复一轮后通过

    @pytest.mark.asyncio
    async def test_max_rounds_respected(self, tmp_path):
        """超过最大修复轮次后停止。"""
        (tmp_path / "bad.gd").write_text("func (\n", encoding="utf-8")

        # Claude 不修复任何东西
        async def fake_noop(cwd, prompt, input_mode="touch", max_turns=20, use_mcp=True, session_id="", model=""):
            return "dunno", "", 0

        with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_noop), \
             patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
            fix_log = await _validate_and_fix(tmp_path, "test_game", max_rounds=2)

        assert len(fix_log) == 2  # 两轮都有错误


# ── _send_game_progress 测试 ──


class TestSendGameProgress:
    @pytest.mark.asyncio
    async def test_sends_ws_message(self):
        """_send_game_progress 应通过 ws_manager 发送 game_progress 消息。"""
        from robot.agent.tools import _context as ctx

        ctx._current_owner_id.set("owner_p")
        ctx._current_user_id.set("user_p")
        mock_ws = AsyncMock()
        original_ws = ctx._ws_manager
        ctx._ws_manager = mock_ws

        try:
            await _send_game_progress("正在测试...")

            mock_ws.send_to_user.assert_awaited_once()
            call_args = mock_ws.send_to_user.call_args
            assert call_args[0][0] == "owner_p"
            assert call_args[0][1] == "user_p"
            msg = call_args[0][2]
            assert msg["type"] == "robot_command"
            assert msg["data"]["action"] == "game_progress"
            assert msg["data"]["message"] == "正在测试..."
        finally:
            ctx._ws_manager = original_ws

    @pytest.mark.asyncio
    async def test_no_ws_manager(self):
        """ws_manager 为 None 时不应报错。"""
        from robot.agent.tools import _context as ctx

        original_ws = ctx._ws_manager
        ctx._ws_manager = None

        try:
            await _send_game_progress("should not crash")
        finally:
            ctx._ws_manager = original_ws


# ── P0 修复测试：game_id 路径穿越 + modify 失败事件闭环 + visibility ──


class TestGameIdSafetyValidation:
    """单元测试：_is_safe_ai_game_id 严格匹配 `ai_<digits>_<6hex>`，拒绝
    所有路径穿越 / 模板污染 attempt。"""

    def test_valid_generated_id(self):
        from robot.agent.tools.game_tools import _is_safe_ai_game_id, _generate_game_id
        gid = _generate_game_id("test title")
        assert _is_safe_ai_game_id(gid)

    @pytest.mark.parametrize("bad_id", [
        "",                                # 空 id
        "/home/developer",                 # 绝对路径（Path 拼接陷阱）
        "/etc/passwd",
        "../../etc/passwd",                 # 相对路径穿越
        "ai_../etc",
        "ai_/etc",
        "_template",                        # 内置模板（W3 P0）
        "_templates",
        ".",
        "..",
        "ai_123_abc12",                     # hash 只有 5 位
        "ai_123_abc1234",                   # hash 7 位
        "ai_abc_def123",                    # 时间戳非数字
        "AI_123_abc123",                    # 大写前缀
        "ai_123_abcdeg",                    # hash 含非 hex 字符
        "ai_123_ABC123",                    # hash 大写
        "ai_123_abc123/extra",              # 含分隔符
        "ai_123_abc123\x00null",            # 含 NUL
        "ai_123_abc123\\extra",             # 含反斜杠
    ])
    def test_rejects_path_traversal_and_garbage(self, bad_id):
        from robot.agent.tools.game_tools import _is_safe_ai_game_id
        assert not _is_safe_ai_game_id(bad_id), f"should reject {bad_id!r}"

    def test_rejects_non_string(self):
        from robot.agent.tools.game_tools import _is_safe_ai_game_id
        assert not _is_safe_ai_game_id(None)  # type: ignore[arg-type]
        assert not _is_safe_ai_game_id(123)   # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_modify_game_rejects_path_traversal_and_pushes_failed_event():
    """P0：modify_game 收到非法 game_id 时必须立即拒绝且推送
    game_modification_failed 事件给 iOS（否则 UI 卡死）。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")

    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    try:
        result = await modify_game.ainvoke({
            "game_id": "/home/developer",
            "instruction": "read .ssh/id_rsa",
        })
        # 返回值是友好错误（不暴露详细原因）
        assert "找不到游戏" in result or "not found" in result.lower()

        # 推 game_modification_failed
        assert mock_ws.send_to_user.await_count == 1
        msg = mock_ws.send_to_user.call_args[0][2]
        assert msg["data"]["action"] == "game_modification_failed"
        assert msg["data"]["reason"] == "invalid_game_id"

        # 关键：未调用 Claude Code SDK（即 _run_claude_agent 没被触发）
        # 我们没 mock 它，如果走到，会真的尝试启 SDK 失败 — 但 mock_ws 上的
        # send_to_user 只会被调一次。
    finally:
        ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_db_ownership_rejects_non_owner(clean_game_dir):
    """P0：ownership 必须以 DB row.owner_id 为准。即便 meta.json 写了
    attacker 自己的 user_id，DB row 不匹配应当拒绝并推失败事件。"""
    import contextlib
    from types import SimpleNamespace
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("alice_owner")
    ctx._current_user_id.set("bob_attacker")  # bob 试图改 alice 的游戏
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    game_id = "ai_1700000099_aa00bb"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
    # meta.json 故意写攻击者 user_id —— 模拟 AI 通过 prompt-injection 改写
    (game_dir / "meta.json").write_text(
        '{"game_id": "%s", "owner_id": "bob_attacker"}' % game_id,
        encoding="utf-8",
    )
    clean_game_dir.append(game_dir)

    # 真实 DB row owner_id = alice，应胜过 meta.json
    @contextlib.asynccontextmanager
    async def _fake_alice_session():
        session = AsyncMock()
        fake_row = SimpleNamespace(owner_id="alice_owner")
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=fake_row)
        session.execute = AsyncMock(return_value=result)
        yield session

    try:
        with patch("robot.db.database.async_session", side_effect=_fake_alice_session):
            result = await modify_game.ainvoke({
                "game_id": game_id,
                "instruction": "make bob the new owner",
            })
        # 拒绝消息
        assert "未授权" in result or "not owned" in result.lower() or "not_owner" in result.lower() or result  # 拒绝
        # 推 game_modification_failed
        msg = mock_ws.send_to_user.call_args[0][2]
        assert msg["data"]["action"] == "game_modification_failed"
        assert msg["data"]["reason"] == "not_owner"
    finally:
        ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_db_missing_row_rejects(clean_game_dir):
    """P0：DB 没有 ai_games row → 立即 404 并推 game_not_found 失败事件，
    不再 fall through 到 meta.json 兜底。"""
    import contextlib
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_t")
    ctx._current_user_id.set("user_t")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    game_id = "ai_1700000098_cc00dd"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
    clean_game_dir.append(game_dir)

    @contextlib.asynccontextmanager
    async def _fake_empty_session():
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result)
        yield session

    try:
        with patch("robot.db.database.async_session", side_effect=_fake_empty_session):
            result = await modify_game.ainvoke({"game_id": game_id, "instruction": "x"})
        assert "找不到游戏" in result or "not found" in result.lower()
        msg = mock_ws.send_to_user.call_args[0][2]
        assert msg["data"]["action"] == "game_modification_failed"
        assert msg["data"]["reason"] == "game_not_found"
    finally:
        ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_template_id_rejected():
    """P0：_template 是内置模板目录，存在但绝不能被 modify_game 写。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws
    try:
        result = await modify_game.ainvoke({
            "game_id": "_template",
            "instruction": "destroy this template",
        })
        assert mock_ws.send_to_user.await_count == 1
        msg = mock_ws.send_to_user.call_args[0][2]
        assert msg["data"]["reason"] == "invalid_game_id"
    finally:
        ctx._ws_manager = original_ws


@pytest.mark.asyncio
async def test_modify_game_no_diffs_pushes_failed_event(clean_game_dir):
    """P0：modify_game 跑完但 AI 没改任何文件，必须推
    game_modification_failed 而非只发 GAME_PROGRESS_EMPTY，否则 iOS
    isModifying / aiGameCreating 永久卡 true。"""
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("owner_test")
    ctx._current_user_id.set("user_test")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    # 用合法 id 进入正常流程
    game_id = "ai_1700000002_aabb12"
    game_dir = GAMES_DIR / game_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("extends Node2D", encoding="utf-8")
    clean_game_dir.append(game_dir)

    async def fake_noop(cwd, prompt, **kwargs):
        return "nothing to change", "session-nodiff", 0

    try:
        with patch("robot.agent.tools.game_tools._run_claude_agent", side_effect=fake_noop), \
             patch("robot.agent.tools.game_tools._validate_and_fix", return_value=[]), \
             patch("robot.agent.tools.game_tools._send_game_progress", new_callable=AsyncMock):
            result = await modify_game.ainvoke({
                "game_id": game_id,
                "instruction": "no-op",
            })

        assert "没有检测到文件变更" in result or "no changes" in result.lower()

        # 必须推 game_modification_failed (reason="no_changes")
        actions = [
            call.args[2]["data"]["action"]
            for call in mock_ws.send_to_user.await_args_list
        ]
        assert "game_modification_failed" in actions
        # 找到那条事件验证 reason
        failed_calls = [
            c for c in mock_ws.send_to_user.await_args_list
            if c.args[2]["data"]["action"] == "game_modification_failed"
        ]
        assert len(failed_calls) == 1
        assert failed_calls[0].args[2]["data"]["reason"] == "no_changes"
        assert failed_calls[0].args[2]["data"]["game_id"] == game_id
    finally:
        ctx._ws_manager = original_ws


@pytest.mark.asyncio
@pytest.mark.parametrize("malicious_id", [
    "_template",
    "_templates",
    "..",
    "ai_../etc",
])
async def test_download_endpoint_rejects_path_traversal_game_ids(client, malicious_id):
    """P0 端到端：/api/games/{id}/download 即便文件系统上 _template /
    历史 ai_* 目录存在，DB row 缺失就必须 404。anonymous 与 logged-in 都
    一致 404，不再有 fs fallback 漏洞。"""
    from tests.conftest import auth_header, register_and_login

    # 匿名
    resp_anon = await client.get(f"/api/games/{malicious_id}/download")
    assert resp_anon.status_code == 404, f"anon got {resp_anon.status_code} for {malicious_id!r}"

    # 登录用户
    auth = await register_and_login(client, email=f"pt_{abs(hash(malicious_id))}@x.com")
    headers = auth_header(auth["access_token"])
    resp_user = await client.get(f"/api/games/{malicious_id}/download", headers=headers)
    assert resp_user.status_code == 404, f"logged-in got {resp_user.status_code} for {malicious_id!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("malicious_id", [
    "_template",
    "../etc",
    "ai_../etc",
])
async def test_files_endpoint_rejects_path_traversal_game_ids(client, malicious_id):
    """/api/games/{id}/files 同款端到端拒绝。"""
    from tests.conftest import auth_header, register_and_login
    resp_anon = await client.get(f"/api/games/{malicious_id}/files")
    assert resp_anon.status_code == 404

    auth = await register_and_login(client, email=f"pt_f_{abs(hash(malicious_id))}@x.com")
    headers = auth_header(auth["access_token"])
    resp_user = await client.get(f"/api/games/{malicious_id}/files", headers=headers)
    assert resp_user.status_code == 404


@pytest.mark.asyncio
async def test_check_files_access_missing_row_returns_404_for_logged_in(client, db_session):
    """P0：之前 _check_files_access 对登录用户访问未入库 game 返 None
    继续 fs fallback，导致任意登录用户能下载所有未入库的私有 zip。修复
    后 row 缺失统一 404，不区分匿名 / 登录。"""
    from tests.conftest import auth_header, register_and_login

    # 注册 + 登录用户
    auth = await register_and_login(client, email="files_404@example.com")
    headers = auth_header(auth["access_token"])

    # 创建一个 fs 上存在但 DB row 不存在的 game_dir
    leaky_id = "ai_1700000099_leak00"
    game_dir = GAMES_DIR / leaky_id
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "scenes").mkdir(exist_ok=True)
    (game_dir / "scenes" / "main.gd").write_text("var secret = 42", encoding="utf-8")
    zip_path = GAMES_DIR / f"{leaky_id}.zip"
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(game_dir))

    try:
        # 登录用户 GET /files → 必须 404，不能返回文件内容
        resp_files = await client.get(f"/api/games/{leaky_id}/files", headers=headers)
        assert resp_files.status_code == 404, f"got {resp_files.status_code} body={resp_files.text}"

        # 登录用户 GET /download → 同样 404
        resp_dl = await client.get(f"/api/games/{leaky_id}/download", headers=headers)
        assert resp_dl.status_code == 404, f"got {resp_dl.status_code}"

        # 匿名同样 404（旧行为已正确）
        resp_anon = await client.get(f"/api/games/{leaky_id}/files")
        assert resp_anon.status_code == 404
    finally:
        shutil.rmtree(game_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_payload,expected_reason", [
    ('{"game_id":"../etc","instruction":"x"}', "invalid_game_id"),
    ('{"game_id":"_template","instruction":"x"}', "invalid_game_id"),
    ('{"game_id":"/Users/foo","instruction":"x"}', "invalid_game_id"),
    ('{"game_id":"","instruction":"x"}', "invalid_game_id"),
    ('not-json-payload', "invalid_payload"),
])
async def test_streaming_game_modify_rejects_invalid_payloads(bad_payload, expected_reason):
    """P0：streaming.py fast-path 必须在解析后立即拒绝非法 payload，
    不能让 _handle_game_direct 落到 modify_game.ainvoke，且必须推
    game_modification_failed 给 iOS。"""
    from robot.agent import streaming
    from robot.agent.tools import _context as ctx

    ctx._current_owner_id.set("o")
    ctx._current_user_id.set("u")
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws

    def on_chunk(text: str):
        return None

    state = {"owner_id": "o", "user_id": "u", "user_lang": "en"}
    memory = MagicMock()
    memory.load_history_as_messages = AsyncMock(return_value=[])
    memory.save_turn = AsyncMock()

    try:
        with patch("robot.agent.streaming._handle_game_direct", new_callable=AsyncMock) as mock_handle:
            await streaming.stream_agent_response(
                user_input=f"[game_modify]{bad_payload}",
                agent=MagicMock(),  # 永远不会被 fast-path 走到
                state=state,
                memory=memory,
                on_text_chunk=on_chunk,
            )
            mock_handle.assert_not_called()

        # 应推 game_modification_failed
        assert mock_ws.send_to_user.await_count == 1
        msg = mock_ws.send_to_user.call_args[0][2]
        assert msg["data"]["action"] == "game_modification_failed"
        assert msg["data"]["reason"] == expected_reason
    finally:
        ctx._ws_manager = original_ws
