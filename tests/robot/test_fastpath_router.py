"""Router 单测 — mock LLM client，验证决策逻辑。"""

from __future__ import annotations

import pytest

from robot.games.fastpath.llm_client import LLMCallError, LLMResponse
from robot.games.fastpath.router import Router


class _MockLLM:
    """最小 JSONChatClient 实现，注入 parsed dict 或异常。"""

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
            tokens_in=120,
            tokens_out=20,
            cache_read_input_tokens=100,
            model="mock-model",
            duration_ms=80,
        )


@pytest.mark.asyncio
async def test_router_returns_high_confidence_hit():
    llm = _MockLLM(parsed={"archetype_id": "catch_v2", "confidence": 0.92})
    router = Router(llm)

    call = await router.route("做一个猴子接香蕉的游戏", input_mode="body_pose")

    assert call.decision.archetype_id == "catch_v2"
    assert call.decision.confidence == pytest.approx(0.92)
    assert call.tokens_in == 120
    assert call.cache_read_input_tokens == 100


@pytest.mark.asyncio
async def test_router_returns_null_when_no_match():
    llm = _MockLLM(parsed={"archetype_id": None, "confidence": 0.0})
    router = Router(llm)

    call = await router.route("做一个回合制 RPG", input_mode="touch")
    assert call.decision.archetype_id is None
    assert call.decision.confidence == 0.0


@pytest.mark.asyncio
async def test_router_unknown_archetype_forced_null():
    """LLM 返回了 STRATEGIES 里没有的 archetype_id → 强制 null。"""
    llm = _MockLLM(parsed={"archetype_id": "fancy_made_up_thing", "confidence": 0.95})
    router = Router(llm)

    call = await router.route("奇怪游戏", input_mode="touch")
    assert call.decision.archetype_id is None
    assert call.decision.confidence == 0.0


@pytest.mark.asyncio
async def test_router_confidence_clamped():
    """confidence 越界 → clamp 到 [0, 1]。"""
    for raw_conf in (2.5, -0.5, "not_a_number"):
        llm = _MockLLM(parsed={"archetype_id": "catch_v2", "confidence": raw_conf})
        router = Router(llm)
        call = await router.route("接东西", input_mode="touch")
        assert 0.0 <= call.decision.confidence <= 1.0


@pytest.mark.asyncio
async def test_router_null_archetype_forces_zero_confidence():
    """LLM 自相矛盾："archetype_id=null + confidence=0.9" → confidence 强制 0。"""
    llm = _MockLLM(parsed={"archetype_id": None, "confidence": 0.9})
    router = Router(llm)
    call = await router.route("奇怪", input_mode="touch")
    assert call.decision.archetype_id is None
    assert call.decision.confidence == 0.0


@pytest.mark.asyncio
async def test_router_llm_failure_returns_null_decision():
    """LLMCallError → 不抛错，返回 archetype=null + conf=0，让 caller 走 slow path。"""
    llm = _MockLLM(raise_exc=LLMCallError("network down"))
    router = Router(llm)

    call = await router.route("接苹果", input_mode="body_pose")
    assert call.decision.archetype_id is None
    assert call.decision.confidence == 0.0
    assert call.tokens_in == 0


@pytest.mark.asyncio
async def test_router_prompt_lists_archetypes():
    """system prompt 必须列出所有已注册 archetype。"""
    llm = _MockLLM(parsed={"archetype_id": "catch_v2", "confidence": 0.9})
    router = Router(llm)
    await router.route("接东西", input_mode="touch")

    assert llm.last_system is not None
    assert "catch_v2" in llm.last_system
    assert "Catch-and-collect" in llm.last_system or "catch" in llm.last_system.lower()
    # P2.3 起 input_mode 不再喂给 router LLM —— smoke 实测发现 input_mode
    # 与 archetype 描述里的 input 强约束冲突会让 router 过度保守返 null。
    # input_mode 留给 customizer 在生成 GDScript 时按需翻译。
    assert "input_mode" not in (llm.last_user or "")
    # 用户描述本身仍要进 prompt
    assert "接东西" in (llm.last_user or "")


@pytest.mark.asyncio
async def test_router_lists_all_registered_archetypes():
    """Phase 2 W3：注册表里所有 archetype 必须自动出现在 Router prompt。"""
    from robot.games.fastpath.strategies import STRATEGIES
    llm = _MockLLM(parsed={"archetype_id": "catch_v2", "confidence": 0.9})
    router = Router(llm)
    await router.route("x", input_mode="touch")

    for arch_id in STRATEGIES:
        assert arch_id in (llm.last_system or ""), \
            f"Router prompt missing registered archetype {arch_id!r}"


@pytest.mark.asyncio
async def test_router_picks_dodger_for_dodge_intent():
    """LLM 返回 dodger_v1 → Router 接受（registry 里有，confidence 合法）。"""
    llm = _MockLLM(parsed={"archetype_id": "dodger_v2", "confidence": 0.88})
    router = Router(llm)
    call = await router.route("操控飞船躲开陨石", input_mode="body_pose")
    assert call.decision.archetype_id == "dodger_v2"
    assert call.decision.confidence == pytest.approx(0.88)
