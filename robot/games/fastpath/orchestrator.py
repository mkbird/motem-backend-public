"""FastPathOrchestrator — Router + Customizer + Compiler 串联 + 置信度网关。

调用方（streaming.py）只需要：
    outcome = await orchestrator.try_create(
        user_text=...,
        input_mode=...,
        target_dir=...,
        request_id=...,
        user_id=...,
    )
    if outcome.hit:
        # 用 outcome.compile_result + outcome.recipe 走 game_ready 推送
    else:
        # fallback 到现有 Claude Code SDK 链路

Orchestrator **不做**：
- DB 写入 ai_games 表
- WebSocket 推 game_ready 通知
- ZIP 打包
这些由 caller 在 fast path 命中后继续完成（Phase 1 Week 3 接入 streaming.py 时再做）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from robot.games.fastpath.compiler import CompileError, RecipeCompiler
from robot.games.fastpath.customizer import Customizer, CustomizerError
from robot.games.fastpath.observability import (
    emit_fast_path_hit,
    emit_slow_path_fallback,
)
from robot.games.fastpath.router import Router
from robot.games.fastpath.schemas import CompileResult, GameRecipe

logger = logging.getLogger(__name__)


DEFAULT_CONFIDENCE_THRESHOLD = 0.7


@dataclass
class FastPathOutcome:
    """Orchestrator 单次调用的最终结论。"""

    hit: bool
    recipe: GameRecipe | None
    compile_result: CompileResult | None
    fallback_reason: str | None
    confidence: float
    archetype_id: str | None

    # 累积的 LLM 元数据 — 给 caller / observability 用
    router_tokens_in: int = 0
    router_tokens_out: int = 0
    router_duration_ms: int = 0
    customizer_tokens_in: int = 0
    customizer_tokens_out: int = 0
    customizer_cache_read_input_tokens: int = 0
    customizer_duration_ms: int = 0
    compiler_duration_ms: int = 0
    wall_clock_ms: int = 0


class FastPathOrchestrator:
    def __init__(
        self,
        router: Router,
        customizer: Customizer,
        compiler: RecipeCompiler,
        *,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._router = router
        self._customizer = customizer
        self._compiler = compiler
        self._threshold = confidence_threshold

    async def try_create(
        self,
        *,
        user_text: str,
        input_mode: str,
        target_dir: Path,
        request_id: str,
        user_id: str | None = None,
    ) -> FastPathOutcome:
        t_start = time.monotonic()

        # ─── 1. Router ───────────────────────────────────────────────────────
        rcall = await self._router.route(user_text, input_mode=input_mode)
        decision = rcall.decision

        if decision.archetype_id is None or decision.confidence < self._threshold:
            reason = (
                "no_archetype_match"
                if decision.archetype_id is None
                else "low_confidence"
            )
            wall_ms = int((time.monotonic() - t_start) * 1000)
            emit_slow_path_fallback(
                request_id=request_id,
                user_id=user_id,
                archetype_id=decision.archetype_id,
                confidence=decision.confidence,
                fallback_reason=reason,
                router_tokens_in=rcall.tokens_in,
                router_tokens_out=rcall.tokens_out,
                wall_clock_ms=wall_ms,
                final_outcome="pending_slow_path",
            )
            return FastPathOutcome(
                hit=False,
                recipe=None,
                compile_result=None,
                fallback_reason=reason,
                confidence=decision.confidence,
                archetype_id=decision.archetype_id,
                router_tokens_in=rcall.tokens_in,
                router_tokens_out=rcall.tokens_out,
                router_duration_ms=rcall.duration_ms,
                wall_clock_ms=wall_ms,
            )

        # ─── 2. Customizer ───────────────────────────────────────────────────
        try:
            ccall = await self._customizer.customize(
                user_text=user_text,
                archetype_id=decision.archetype_id,
                confidence=decision.confidence,
            )
        except CustomizerError as e:
            logger.warning("[Fastpath/Orchestrator] customizer failed: %s", e)
            wall_ms = int((time.monotonic() - t_start) * 1000)
            emit_slow_path_fallback(
                request_id=request_id,
                user_id=user_id,
                archetype_id=decision.archetype_id,
                confidence=decision.confidence,
                fallback_reason="customizer_error",
                router_tokens_in=rcall.tokens_in,
                router_tokens_out=rcall.tokens_out,
                wall_clock_ms=wall_ms,
                final_outcome="pending_slow_path",
            )
            return FastPathOutcome(
                hit=False, recipe=None, compile_result=None,
                fallback_reason="customizer_error",
                confidence=decision.confidence,
                archetype_id=decision.archetype_id,
                router_tokens_in=rcall.tokens_in,
                router_tokens_out=rcall.tokens_out,
                router_duration_ms=rcall.duration_ms,
                wall_clock_ms=wall_ms,
            )

        # ─── 3. Compiler ─────────────────────────────────────────────────────
        t_compile_start = time.monotonic()
        try:
            compile_result = self._compiler.compile(ccall.recipe, target_dir, input_mode=input_mode)
        except CompileError as e:
            logger.warning("[Fastpath/Orchestrator] compiler failed: %s", e)
            wall_ms = int((time.monotonic() - t_start) * 1000)
            emit_slow_path_fallback(
                request_id=request_id,
                user_id=user_id,
                archetype_id=decision.archetype_id,
                confidence=decision.confidence,
                fallback_reason="compile_error",
                router_tokens_in=rcall.tokens_in,
                router_tokens_out=rcall.tokens_out,
                wall_clock_ms=wall_ms,
                final_outcome="pending_slow_path",
            )
            return FastPathOutcome(
                hit=False, recipe=ccall.recipe, compile_result=None,
                fallback_reason="compile_error",
                confidence=decision.confidence,
                archetype_id=decision.archetype_id,
                router_tokens_in=rcall.tokens_in,
                router_tokens_out=rcall.tokens_out,
                router_duration_ms=rcall.duration_ms,
                customizer_tokens_in=ccall.tokens_in,
                customizer_tokens_out=ccall.tokens_out,
                customizer_cache_read_input_tokens=ccall.cache_read_input_tokens,
                customizer_duration_ms=ccall.duration_ms,
                wall_clock_ms=wall_ms,
            )
        compiler_ms = int((time.monotonic() - t_compile_start) * 1000)

        wall_ms = int((time.monotonic() - t_start) * 1000)
        emit_fast_path_hit(
            request_id=request_id,
            user_id=user_id,
            archetype_id=decision.archetype_id,
            confidence=decision.confidence,
            router_tokens_in=rcall.tokens_in,
            router_tokens_out=rcall.tokens_out,
            customizer_tokens_in=ccall.tokens_in,
            customizer_tokens_out=ccall.tokens_out,
            customizer_cache_read_input_tokens=ccall.cache_read_input_tokens,
            compiler_duration_ms=compiler_ms,
            wall_clock_ms=wall_ms,
        )

        return FastPathOutcome(
            hit=True,
            recipe=ccall.recipe,
            compile_result=compile_result,
            fallback_reason=None,
            confidence=decision.confidence,
            archetype_id=decision.archetype_id,
            router_tokens_in=rcall.tokens_in,
            router_tokens_out=rcall.tokens_out,
            router_duration_ms=rcall.duration_ms,
            customizer_tokens_in=ccall.tokens_in,
            customizer_tokens_out=ccall.tokens_out,
            customizer_cache_read_input_tokens=ccall.cache_read_input_tokens,
            customizer_duration_ms=ccall.duration_ms,
            compiler_duration_ms=compiler_ms,
            wall_clock_ms=wall_ms,
        )
