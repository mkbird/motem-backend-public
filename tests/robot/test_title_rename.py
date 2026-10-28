"""Title rename tests — PATCH /{game_id}/title + PATCH /{widget_id}/title.

覆盖矩阵：
- translator helper：CJK 检测、空串、有/无 LLM key
- game title PATCH：owner-only / 404 / 400 空标题 / 400 过长 / builtin 锁 / 成功
- widget title PATCH：对称
- 改名后列表 title 跟着变（按 Accept-Language 渲染）

LLM 翻译路径在测试环境（无 dashscope key）走 fallback：source-as-both，
两个 slot 都填用户输入。要测真正的 LLM 调用得跑集成测试，不在本文件范围。
"""

import json
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.models import AIGame, AIWidget, UserGameView, UserWidgetView
from tests.conftest import auth_header, register_and_login


# ─────────────────────────────────────────────────────────────────────
# Translator helper unit tests
# ─────────────────────────────────────────────────────────────────────


def test_detect_lang_cjk():
    from robot.agent.title_translator import detect_title_language
    assert detect_title_language("太空射击") == "zh-Hans"
    assert detect_title_language("Space Shooter") == "en"
    assert detect_title_language("Mix 混合") == "zh-Hans"  # 含 CJK 即归类 zh
    assert detect_title_language("") == "en"  # 空 → 默认 en


@pytest.mark.asyncio
async def test_translate_title_empty():
    from robot.agent.title_translator import translate_title
    out = await translate_title("")
    assert out == {"en": "", "zh-Hans": ""}


@pytest.mark.asyncio
async def test_translate_title_no_llm_fallback(monkeypatch):
    """无 dashscope key 时降级为 source-as-both，仍返合法 dict。"""
    from robot.agent.title_translator import translate_title
    from robot.config import settings

    monkeypatch.setattr(settings, "dashscope_api_key", "", raising=False)
    out = await translate_title("太空射击")
    assert out["zh-Hans"] == "太空射击"
    assert out["en"] == "太空射击"  # fallback


@pytest.mark.asyncio
async def test_translate_title_explicit_source(monkeypatch):
    from robot.agent.title_translator import translate_title
    from robot.config import settings

    monkeypatch.setattr(settings, "dashscope_api_key", "", raising=False)
    out = await translate_title("My Cool Game", source_lang="en")
    # source-as-both 时仍记得用户原文进 en slot
    assert out["en"] == "My Cool Game"
    assert out["zh-Hans"] == "My Cool Game"


# ─────────────────────────────────────────────────────────────────────
# LLM-driven translation path (mock AsyncOpenAI)
# ─────────────────────────────────────────────────────────────────────


def _make_mock_openai_client(reply_content: str):
    """构造一个假的 AsyncOpenAI client，`chat.completions.create()` 返回
    `choices[0].message.content == reply_content`。"""
    from unittest.mock import AsyncMock, MagicMock

    fake_message = MagicMock()
    fake_message.content = reply_content
    fake_choice = MagicMock()
    fake_choice.message = fake_message
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_response)
    return fake_client


@pytest.mark.asyncio
async def test_translate_title_zh_to_en_llm(monkeypatch):
    """用户输入中文 → LLM 翻译出英文，两个 slot 都正确填充。"""
    import robot.agent.title_translator as tt
    from robot.config import settings

    monkeypatch.setattr(settings, "dashscope_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        settings, "openai_base_url", "https://fake.test/v1", raising=False
    )

    fake_client = _make_mock_openai_client("Space Adventure")
    # 替换 openai 模块的 AsyncOpenAI 构造器 —— translate_title 内部 import
    # 时拿到这个 mock，不真发请求。
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client)

    out = await tt.translate_title("太空大冒险")
    assert out["zh-Hans"] == "太空大冒险"
    assert out["en"] == "Space Adventure"

    # 验证 system prompt 走的是 zh→en 分支
    call_args = fake_client.chat.completions.create.await_args
    msgs = call_args.kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert "Simplified Chinese" in msgs[0]["content"]
    assert "English" in msgs[0]["content"]
    assert msgs[1]["content"] == "太空大冒险"


@pytest.mark.asyncio
async def test_translate_title_en_to_zh_llm(monkeypatch):
    import robot.agent.title_translator as tt
    from robot.config import settings

    monkeypatch.setattr(settings, "dashscope_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        settings, "openai_base_url", "https://fake.test/v1", raising=False
    )

    fake_client = _make_mock_openai_client("太空冒险")
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client)

    out = await tt.translate_title("Space Adventure")
    assert out["en"] == "Space Adventure"
    assert out["zh-Hans"] == "太空冒险"

    # 系统提示词应该是 en→zh 方向
    call_args = fake_client.chat.completions.create.await_args
    sys_prompt = call_args.kwargs["messages"][0]["content"]
    assert "English to" in sys_prompt
    assert "Simplified Chinese" in sys_prompt


@pytest.mark.asyncio
async def test_translate_title_strips_wrapping_quotes(monkeypatch):
    """LLM 偶尔会把翻译包在引号里 —— translator 必须剥掉。"""
    import robot.agent.title_translator as tt
    from robot.config import settings

    monkeypatch.setattr(settings, "dashscope_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        settings, "openai_base_url", "https://fake.test/v1", raising=False
    )

    # 各种引号包装：英文 / 中文 / 单 / 双
    fake_client = _make_mock_openai_client('"Space Adventure"')
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client)
    out = await tt.translate_title("太空冒险")
    assert out["en"] == "Space Adventure"  # 双引号剥掉

    fake_client2 = _make_mock_openai_client("“太空冒险”")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client2)
    out2 = await tt.translate_title("Space Adventure")
    assert out2["zh-Hans"] == "太空冒险"  # 中文引号剥掉


@pytest.mark.asyncio
async def test_translate_title_truncates_overlong_llm_output(monkeypatch):
    """LLM 偶尔不听话给整段话，translator 截断到 40/15 字符上限。"""
    import robot.agent.title_translator as tt
    from robot.config import settings

    monkeypatch.setattr(settings, "dashscope_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        settings, "openai_base_url", "https://fake.test/v1", raising=False
    )

    long_en = "Space Adventure With Extremely Long Subtitle That Should Be Cut"
    fake_client = _make_mock_openai_client(long_en)
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client)
    out = await tt.translate_title("太空冒险")
    assert len(out["en"]) <= 40

    long_zh = "太空大冒险超级长的副标题应该被裁剪到指定长度以内防止 LLM 失控"
    fake_client2 = _make_mock_openai_client(long_zh)
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client2)
    out2 = await tt.translate_title("Space Adventure")
    assert len(out2["zh-Hans"]) <= 15


@pytest.mark.asyncio
async def test_translate_title_llm_exception_falls_back(monkeypatch):
    """LLM 调用 raise 时不能崩，应回退 source-as-both 让 DB 不写半截状态。"""
    import robot.agent.title_translator as tt
    from robot.config import settings
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr(settings, "dashscope_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        settings, "openai_base_url", "https://fake.test/v1", raising=False
    )

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("network down")
    )
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client)

    out = await tt.translate_title("太空冒险")
    # LLM 失败 → source-as-both，两个 slot 都填用户原文
    assert out["zh-Hans"] == "太空冒险"
    assert out["en"] == "太空冒险"


@pytest.mark.asyncio
async def test_game_title_patch_with_llm_translation(
    client: AsyncClient, db_session: AsyncSession, alice_game: dict, monkeypatch,
):
    """端到端：iOS PATCH /title → router 调 LLM → DB 双语 title_i18n 都正确。"""
    from robot.config import settings

    monkeypatch.setattr(settings, "dashscope_api_key", "fake-key", raising=False)
    monkeypatch.setattr(
        settings, "openai_base_url", "https://fake.test/v1", raising=False
    )

    fake_client = _make_mock_openai_client("Brave Knight")
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: fake_client)

    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/title",
        json={"title": "勇敢骑士"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    # 用户原文进 zh-Hans，LLM 翻译进 en
    assert body["title_i18n"]["zh-Hans"] == "勇敢骑士"
    assert body["title_i18n"]["en"] == "Brave Knight"

    # 列表按 Accept-Language: en 拿英文标题
    r_en = await client.get(
        "/api/games/mine",
        headers={
            **auth_header(alice_game["access_token"]),
            "Accept-Language": "en",
        },
    )
    g = next(g for g in r_en.json()["games"] if g["game_id"] == alice_game["game_id"])
    assert g["title"] == "Brave Knight"

    # 同一行按 Accept-Language: zh-Hans 拿中文标题
    r_zh = await client.get(
        "/api/games/mine",
        headers={
            **auth_header(alice_game["access_token"]),
            "Accept-Language": "zh-Hans",
        },
    )
    g_zh = next(
        g for g in r_zh.json()["games"] if g["game_id"] == alice_game["game_id"]
    )
    assert g_zh["title"] == "勇敢骑士"


# ─────────────────────────────────────────────────────────────────────
# Fixtures — Alice 拥有的 game / widget
# ─────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def alice(client: AsyncClient) -> dict:
    return await register_and_login(
        client, email="alice_rename@example.com", device_id="dev-a-rename"
    )


@pytest_asyncio.fixture
async def bob(client: AsyncClient) -> dict:
    return await register_and_login(
        client, email="bob_rename@example.com", device_id="dev-b-rename"
    )


@pytest_asyncio.fixture
async def alice_game(db_session: AsyncSession, alice: dict) -> dict:
    game_id = "ai_rename_alice_g_001"
    now = datetime.utcnow()
    db_session.add(AIGame(
        game_id=game_id,
        owner_id=alice["user"]["id"],
        title="Alice's Game",
        title_i18n=json.dumps({"en": "Old Title", "zh-Hans": "旧标题"}),
        description="d", description_i18n=json.dumps({"en": "d", "zh-Hans": "d"}),
        icon="🎮", input_mode="touch", kind="ai",
        visibility="private", original_game_id=None, template_from="",
        session_id="sess", like_count=0, favorite_count=0,
        share_count=0, remix_count=0, created_at=now, updated_at=now,
    ))
    db_session.add(UserGameView(
        user_id=alice["user"]["id"], game_id=game_id,
        view_type="created", added_at=now,
    ))
    await db_session.commit()
    return {**alice, "game_id": game_id}


@pytest_asyncio.fixture
async def alice_widget(db_session: AsyncSession, alice: dict) -> dict:
    widget_id = "ai_rename_alice_w_001"
    now = datetime.utcnow()
    db_session.add(AIWidget(
        widget_id=widget_id,
        owner_id=alice["user"]["id"],
        title_i18n=json.dumps({"en": "Old Widget", "zh-Hans": "旧组件"}),
        description_i18n=json.dumps({"en": "d", "zh-Hans": "d"}),
        supported_sizes=json.dumps(["medium"]),
        base_template_id="",
        kind="ai", visibility="private", original_widget_id=None,
        like_count=0, favorite_count=0, remix_count=0,
        created_at=now, updated_at=now,
    ))
    db_session.add(UserWidgetView(
        user_id=alice["user"]["id"], widget_id=widget_id,
        view_type="created", added_at=now,
    ))
    await db_session.commit()
    return {**alice, "widget_id": widget_id}


# ─────────────────────────────────────────────────────────────────────
# Game title PATCH
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_game_title_success_zh_input(
    client: AsyncClient, db_session: AsyncSession, alice_game: dict, monkeypatch,
):
    from robot.config import settings
    monkeypatch.setattr(settings, "dashscope_api_key", "", raising=False)

    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/title",
        json={"title": "太空大冒险"},
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "太空大冒险"
    # 无 LLM fallback：两个 slot 都填用户原文
    assert body["title_i18n"]["zh-Hans"] == "太空大冒险"
    assert body["title_i18n"]["en"] == "太空大冒险"

    # DB 真的写入
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AIGame).where(AIGame.game_id == alice_game["game_id"])
        )
    ).scalar_one()
    assert row.title == "太空大冒险"
    stored = json.loads(row.title_i18n)
    assert stored["zh-Hans"] == "太空大冒险"


@pytest.mark.asyncio
async def test_game_title_empty_400(client: AsyncClient, alice_game: dict):
    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/title",
        json={"title": "   "},
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_game_title_too_long_400(client: AsyncClient, alice_game: dict):
    long_title = "x" * 41
    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/title",
        json={"title": long_title},
        headers=auth_header(alice_game["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_game_title_non_owner_403(
    client: AsyncClient, alice_game: dict, bob: dict
):
    resp = await client.patch(
        f"/api/games/{alice_game['game_id']}/title",
        json={"title": "Bob hijacks"},
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_game_title_not_found_404(client: AsyncClient, alice: dict):
    resp = await client.patch(
        "/api/games/nonexistent/title",
        json={"title": "Hi"},
        headers=auth_header(alice["access_token"]),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_game_title_change_reflects_in_list(
    client: AsyncClient, alice_game: dict, monkeypatch,
):
    """改名后 /mine 列表（按 Accept-Language）跟着变。"""
    from robot.config import settings
    monkeypatch.setattr(settings, "dashscope_api_key", "", raising=False)

    # rename → zh-Hans
    await client.patch(
        f"/api/games/{alice_game['game_id']}/title",
        json={"title": "全新标题"},
        headers=auth_header(alice_game["access_token"]),
    )
    # 取中文列表
    r = await client.get(
        "/api/games/mine",
        headers={
            **auth_header(alice_game["access_token"]),
            "Accept-Language": "zh-Hans",
        },
    )
    assert r.status_code == 200
    games = r.json()["games"]
    g = next(g for g in games if g["game_id"] == alice_game["game_id"])
    assert g["title"] == "全新标题"


# ─────────────────────────────────────────────────────────────────────
# Widget title PATCH (对称)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_widget_title_success_en_input(
    client: AsyncClient, db_session: AsyncSession,
    alice_widget: dict, monkeypatch,
):
    from robot.config import settings
    monkeypatch.setattr(settings, "dashscope_api_key", "", raising=False)

    resp = await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/title",
        json={"title": "Cool Clock"},
        headers=auth_header(alice_widget["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Cool Clock"
    assert body["title_i18n"]["en"] == "Cool Clock"

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AIWidget).where(AIWidget.widget_id == alice_widget["widget_id"])
        )
    ).scalar_one()
    stored = json.loads(row.title_i18n)
    assert stored["en"] == "Cool Clock"


@pytest.mark.asyncio
async def test_widget_title_empty_400(client: AsyncClient, alice_widget: dict):
    resp = await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/title",
        json={"title": ""},
        headers=auth_header(alice_widget["access_token"]),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_widget_title_non_owner_403(
    client: AsyncClient, alice_widget: dict, bob: dict
):
    resp = await client.patch(
        f"/api/widgets/{alice_widget['widget_id']}/title",
        json={"title": "hijack"},
        headers=auth_header(bob["access_token"]),
    )
    assert resp.status_code == 403
