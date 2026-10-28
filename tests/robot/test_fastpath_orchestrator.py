"""Orchestrator 集成测试 — Router + Customizer + Compiler 端到端串联（mock LLM）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from robot.games.fastpath.compiler import RecipeCompiler
from robot.games.fastpath.customizer import Customizer
from robot.games.fastpath.llm_client import LLMResponse
from robot.games.fastpath.orchestrator import FastPathOrchestrator
from robot.games.fastpath.router import Router


_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_TEMPLATE_DIR = _BACKEND_ROOT / "games" / "_template"


_VALID_RECIPE_FRAGMENT = {
    "meta": {
        "title_i18n": {"en": "Banana Frenzy", "zh-Hans": "香蕉狂热"},
        "description_i18n": {"en": "Catch the falling bananas", "zh-Hans": "接住香蕉"},
        "icon": "🍌",
    },
    "theme": {"palette": "warm", "background_hint": "forest"},
    "entities": {
        "player": {"sprite_hint": "monkey"},
        "drop": {"sprite_hint": "banana", "score_value": 1},
        "obstacle": {"sprite_hint": "bomb", "lives_cost": 1},
    },
    "params": {
        "spawn_rate_per_sec": 1.8,
        "fall_speed_min": 220,
        "fall_speed_max": 420,
        "obstacle_probability": 0.25,
        "win_score": 30,
        "max_lives": 3,
    },
    "audio": {
        "bgm": "bgm_casual",
        "sfx_pickup": "pickup",
        "sfx_hit": "hit",
        "sfx_gameover": "level_fail",
    },
}


class _ScriptedLLM:
    """按调用顺序返回不同 parsed dict — Router 一次，Customizer 一次。"""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def chat_json(self, *, system: str, user: str, **kwargs) -> LLMResponse:
        if not self._responses:
            raise RuntimeError(f"scripted LLM ran out (call #{self.call_count + 1})")
        parsed = self._responses.pop(0)
        self.call_count += 1
        return LLMResponse(
            parsed=parsed,
            raw_text="{}",
            tokens_in=500,
            tokens_out=80,
            cache_read_input_tokens=400,
            model="mock",
            duration_ms=50,
        )


def _make_orchestrator(llm: _ScriptedLLM, *, threshold: float = 0.7) -> FastPathOrchestrator:
    return FastPathOrchestrator(
        router=Router(llm),
        customizer=Customizer(llm),
        compiler=RecipeCompiler(_TEMPLATE_DIR),
        confidence_threshold=threshold,
    )


@pytest.fixture(autouse=True)
def _disable_trace(monkeypatch):
    """orchestrator 内部会写 JSONL trace；测试时关闭避免磁盘 IO。"""
    monkeypatch.setenv("ROBOT_FASTPATH_TRACE_DISABLE", "1")


@pytest.mark.asyncio
async def test_orchestrator_full_fast_path_hit(tmp_path):
    llm = _ScriptedLLM([
        {"archetype_id": "catch_v2", "confidence": 0.93},  # Router
        _VALID_RECIPE_FRAGMENT,                            # Customizer
    ])
    orch = _make_orchestrator(llm)

    outcome = await orch.try_create(
        user_text="做一个猴子接香蕉的游戏",
        input_mode="body_pose",
        target_dir=tmp_path / "g1",
        request_id="req-1",
        user_id="user-1",
    )

    assert outcome.hit is True
    assert outcome.archetype_id == "catch_v2"
    assert outcome.confidence == pytest.approx(0.93)
    assert outcome.fallback_reason is None
    assert outcome.recipe is not None
    assert outcome.recipe.meta.title_i18n["en"] == "Banana Frenzy"
    assert outcome.compile_result is not None
    assert (outcome.compile_result.game_dir / "scenes" / "main.gd").exists()
    assert (outcome.compile_result.game_dir / "meta.json").exists()
    # 两次 LLM 调用都发生
    assert llm.call_count == 2
    # 元数据累积
    assert outcome.router_tokens_in == 500
    assert outcome.customizer_tokens_in == 500
    assert outcome.customizer_cache_read_input_tokens == 400


@pytest.mark.asyncio
async def test_orchestrator_no_archetype_match_fallback(tmp_path):
    llm = _ScriptedLLM([
        {"archetype_id": None, "confidence": 0.0},
    ])
    orch = _make_orchestrator(llm)

    outcome = await orch.try_create(
        user_text="回合制策略 RPG，多周目结局",
        input_mode="touch",
        target_dir=tmp_path / "g_miss",
        request_id="req-miss",
    )

    assert outcome.hit is False
    assert outcome.fallback_reason == "no_archetype_match"
    assert outcome.compile_result is None
    # Customizer 不应被调用
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_orchestrator_low_confidence_fallback(tmp_path):
    """archetype 命中但 confidence 低于阈值 → fallback。"""
    llm = _ScriptedLLM([
        {"archetype_id": "catch_v2", "confidence": 0.4},
    ])
    orch = _make_orchestrator(llm, threshold=0.7)

    outcome = await orch.try_create(
        user_text="模糊描述",
        input_mode="touch",
        target_dir=tmp_path / "g_low",
        request_id="req-low",
    )

    assert outcome.hit is False
    assert outcome.fallback_reason == "low_confidence"
    assert outcome.archetype_id == "catch_v2"
    assert outcome.confidence == pytest.approx(0.4)
    assert llm.call_count == 1  # 没走 Customizer


@pytest.mark.asyncio
async def test_orchestrator_low_threshold_admits_fuzzy_match(tmp_path):
    """阈值调低 → 中等置信度也走 fast path。"""
    llm = _ScriptedLLM([
        {"archetype_id": "catch_v2", "confidence": 0.4},
        _VALID_RECIPE_FRAGMENT,
    ])
    orch = _make_orchestrator(llm, threshold=0.3)

    outcome = await orch.try_create(
        user_text="接东西",
        input_mode="touch",
        target_dir=tmp_path / "g_thr",
        request_id="req-thr",
    )
    assert outcome.hit is True
    assert outcome.confidence == pytest.approx(0.4)
