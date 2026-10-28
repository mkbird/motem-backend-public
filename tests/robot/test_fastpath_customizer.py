"""Customizer 单测 — mock LLM client，验证 recipe 构造 + 兜底。"""

from __future__ import annotations

import pytest

from robot.games.fastpath.customizer import Customizer, CustomizerError
from robot.games.fastpath.llm_client import LLMCallError, LLMResponse


_VALID_FRAGMENT = {
    "meta": {
        "title_i18n": {"en": "Star Catcher", "zh-Hans": "拾星者"},
        "description_i18n": {"en": "Catch falling stars", "zh-Hans": "接住下落的星星"},
        "icon": "⭐",
    },
    "theme": {"palette": "neon", "background_hint": "space"},
    "entities": {
        "player": {"sprite_hint": "astronaut"},
        "drop": {"sprite_hint": "star", "score_value": 1},
        "obstacle": {"sprite_hint": "rock", "lives_cost": 1},
    },
    "params": {
        "spawn_rate_per_sec": 2.0,
        "fall_speed_min": 240,
        "fall_speed_max": 460,
        "obstacle_probability": 0.28,
        "win_score": 25,
        "max_lives": 3,
    },
    "audio": {
        "bgm": "bgm_arcade",
        "sfx_pickup": "pickup",
        "sfx_hit": "hit",
        "sfx_gameover": "level_fail",
    },
}


class _MockLLM:
    def __init__(self, parsed: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._parsed = parsed
        self._exc = raise_exc
        self.last_system: str | None = None
        self.last_user: str | None = None

    async def chat_json(self, *, system: str, user: str, **kwargs) -> LLMResponse:
        self.last_system = system
        self.last_user = user
        if self._exc is not None:
            raise self._exc
        return LLMResponse(
            parsed=self._parsed or {},
            raw_text="{}",
            tokens_in=2400,
            tokens_out=420,
            cache_read_input_tokens=2200,
            model="mock-model",
            duration_ms=900,
        )


@pytest.mark.asyncio
async def test_customizer_happy_path():
    llm = _MockLLM(parsed=_VALID_FRAGMENT)
    cust = Customizer(llm)

    call = await cust.customize(
        user_text="星空主题接星星",
        archetype_id="catch_v2",
        confidence=0.91,
    )

    r = call.recipe
    assert r.archetype_id == "catch_v2"
    assert r.confidence == pytest.approx(0.91)
    assert r.meta.title_i18n["en"] == "Star Catcher"
    assert r.meta.title_i18n["zh-Hans"] == "拾星者"
    assert r.theme.palette == "neon"
    assert r.entities.player.sprite_hint == "astronaut"
    assert r.entities.drop.sprite_hint == "star"
    assert r.params.win_score == 25
    assert r.audio.bgm == "bgm_arcade"
    # 调用元数据
    assert call.tokens_in == 2400
    assert call.cache_read_input_tokens == 2200


@pytest.mark.asyncio
async def test_customizer_unknown_archetype_raises():
    llm = _MockLLM(parsed=_VALID_FRAGMENT)
    cust = Customizer(llm)
    with pytest.raises(CustomizerError, match="unknown archetype_id"):
        await cust.customize(user_text="x", archetype_id="no_such")


@pytest.mark.asyncio
async def test_customizer_llm_failure_raises():
    llm = _MockLLM(raise_exc=LLMCallError("rate limit"))
    cust = Customizer(llm)
    with pytest.raises(CustomizerError, match="LLM call failed"):
        await cust.customize(user_text="x", archetype_id="catch_v2")


@pytest.mark.asyncio
async def test_customizer_invalid_subfield_falls_back_per_field():
    """params 越界 → 该 sub-field 降级到默认；其他字段保留 LLM 输出。"""
    bad = {
        **_VALID_FRAGMENT,
        "params": {
            "spawn_rate_per_sec": 999.0,  # 越界
            "fall_speed_min": 50,
            "fall_speed_max": 30,  # < min，触发 cross-field 校验失败
            "obstacle_probability": 0.5,
            "win_score": 25,
            "max_lives": 3,
        },
    }
    llm = _MockLLM(parsed=bad)
    cust = Customizer(llm)

    call = await cust.customize(user_text="x", archetype_id="catch_v2")
    r = call.recipe

    # meta / theme / entities / audio 全部保留 LLM 输出
    assert r.meta.title_i18n["en"] == "Star Catcher"
    assert r.theme.palette == "neon"
    assert r.entities.drop.sprite_hint == "star"
    assert r.audio.bgm == "bgm_arcade"
    # params 降级到默认（Pydantic 默认值）
    assert r.params.spawn_rate_per_sec <= 8.0
    assert r.params.fall_speed_max >= r.params.fall_speed_min


@pytest.mark.asyncio
async def test_customizer_missing_required_field_uses_defaults():
    """LLM 漏掉某个顶级 key → 全 recipe 第一次构造失败 → piecewise 重建用默认值。"""
    partial = {"meta": _VALID_FRAGMENT["meta"]}  # 只给了 meta
    llm = _MockLLM(parsed=partial)
    cust = Customizer(llm)

    call = await cust.customize(user_text="x", archetype_id="catch_v2")
    r = call.recipe
    # meta 保留 LLM 输出
    assert r.meta.title_i18n["en"] == "Star Catcher"
    # 其他字段全部使用 Pydantic 默认值
    assert r.theme.palette == "warm"  # default
    assert r.entities.player.sprite_hint == "monkey"  # default


@pytest.mark.asyncio
async def test_customizer_prompt_includes_catalog_and_examples():
    llm = _MockLLM(parsed=_VALID_FRAGMENT)
    cust = Customizer(llm)
    await cust.customize(user_text="x", archetype_id="catch_v2")

    sys = llm.last_system or ""
    # catalog 各品类必须在 prompt 里
    assert "monkey" in sys
    assert "banana" in sys
    assert "neon" in sys or "warm" in sys
    # few-shot example 必须在 prompt 里
    assert "Example 1" in sys
    assert "archetype" in (llm.last_user or "").lower()


# ─── P2.2：v0.2 字段 inject + 解析 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_customizer_prompt_includes_v0_2_fields():
    """system prompt 必须 inject directors / hidden_tech / tag 字段说明 + cheatsheet。"""
    llm = _MockLLM(parsed=_VALID_FRAGMENT)
    cust = Customizer(llm)
    await cust.customize(user_text="x", archetype_id="catch_v2")

    sys = llm.last_system or ""
    # Schema 里出现新字段名
    assert "directors" in sys
    assert "hidden_tech_enabled" in sys
    # ItemTag enum 6 个值都出现
    for tag in ("neutral", "fire", "water", "grass", "metal", "explosive"):
        assert tag in sys, f"tag {tag} missing in prompt"
    # Director enum 10 个值都出现
    for d in ("tense", "relaxing", "chaotic", "puzzle",
              "gravity_flip", "slowmo", "magnet", "dark", "wind", "boss_round"):
        assert d in sys, f"director {d} missing in prompt"
    # Cheatsheet 段落标识
    assert "director + tag selection rules" in sys


@pytest.mark.asyncio
async def test_customizer_parses_directors_and_hidden_tech():
    """LLM 输出 directors / hidden_tech_enabled 顶层字段 → recipe 正确读取。"""
    fragment = {
        **_VALID_FRAGMENT,
        "directors": ["tense", "chaotic"],
        "hidden_tech_enabled": True,
    }
    llm = _MockLLM(parsed=fragment)
    cust = Customizer(llm)
    call = await cust.customize(user_text="紧张刺激", archetype_id="catch_v2")

    assert [d.value for d in call.recipe.directors] == ["tense", "chaotic"]
    assert call.recipe.hidden_tech_enabled is True


@pytest.mark.asyncio
async def test_customizer_parses_entity_tags():
    """LLM 输出 entities.*.tag → recipe 正确读取，影响 Chemistry 行为。"""
    fragment = {
        **_VALID_FRAGMENT,
        "entities": {
            "player": {"sprite_hint": "astronaut", "tag": "neutral"},
            "drop": {"sprite_hint": "fireball", "score_value": 1, "tag": "fire"},
            "obstacle": {"sprite_hint": "ice", "lives_cost": 1, "tag": "water"},
        },
    }
    llm = _MockLLM(parsed=fragment)
    cust = Customizer(llm)
    call = await cust.customize(user_text="火 vs 冰", archetype_id="catch_v2")

    assert call.recipe.entities.drop.tag.value == "fire"
    assert call.recipe.entities.obstacle.tag.value == "water"


@pytest.mark.asyncio
async def test_customizer_omits_v0_2_fields_falls_back():
    """LLM 没输出 v0.2 字段 → 用默认值（v0.1 兼容路径）。"""
    llm = _MockLLM(parsed=_VALID_FRAGMENT)  # 不含 directors / hidden_tech
    cust = Customizer(llm)
    call = await cust.customize(user_text="无 modifier", archetype_id="catch_v2")

    assert call.recipe.directors == []
    assert call.recipe.hidden_tech_enabled is False
    assert call.recipe.entities.drop.tag.value == "neutral"


@pytest.mark.asyncio
async def test_customizer_rejects_invalid_directors_keeps_recipe():
    """LLM 输出非法 director value → piecewise fallback 用默认 []，其他字段保留。"""
    fragment = {
        **_VALID_FRAGMENT,
        "directors": ["not_a_real_director", "tense"],  # 整个列表非法
        "hidden_tech_enabled": True,
    }
    llm = _MockLLM(parsed=fragment)
    cust = Customizer(llm)
    call = await cust.customize(user_text="x", archetype_id="catch_v2")

    # directors 整体被拒，走默认；hidden_tech_enabled 仍然生效（独立字段）
    assert call.recipe.directors == []
    assert call.recipe.hidden_tech_enabled is True
    # 其他字段照常解析
    assert call.recipe.meta.title_i18n["en"] == "Star Catcher"
