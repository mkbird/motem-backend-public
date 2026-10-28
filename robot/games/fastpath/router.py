"""L1 Router — Fast Path 第 1 次 LLM 调用，做纯分类决策。

输入：用户原文 + input_mode
输出：archetype_id（或 null）+ confidence

不做：抽 title / 抽参数 / 选 sprite。那些是 Customizer 的事。
为什么拆出来：低置信度直接转 slow path，不付 Customizer 的 token 钱。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from robot.games.fastpath.llm_client import JSONChatClient, LLMCallError
from robot.games.fastpath.schemas import RouterDecision
from robot.games.fastpath.strategies import STRATEGIES

logger = logging.getLogger(__name__)


@dataclass
class RouterCall:
    """Router 一次调用的完整结果（含 LLM 元数据用于观测埋点）。"""

    decision: RouterDecision
    tokens_in: int
    tokens_out: int
    cache_read_input_tokens: int
    duration_ms: int
    model: str
    raw_response: str  # 仅 debug 用


class Router:
    """L1 Router — 路由到具体 archetype 或返回 null。"""

    def __init__(
        self,
        llm: JSONChatClient,
        *,
        max_tokens: int = 200,
        temperature: float = 0.0,
    ) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def route(self, user_text: str, input_mode: str = "touch") -> RouterCall:
        system = self._build_system_prompt()
        user = self._build_user_message(user_text, input_mode)

        try:
            resp = await self._llm.chat_json(
                system=system,
                user=user,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except LLMCallError as e:
            logger.warning("[Fastpath/Router] LLM call failed: %s", e)
            return RouterCall(
                decision=RouterDecision(archetype_id=None, confidence=0.0),
                tokens_in=0, tokens_out=0, cache_read_input_tokens=0,
                duration_ms=0, model="", raw_response="",
            )

        decision = self._parse_decision(resp.parsed)
        return RouterCall(
            decision=decision,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            cache_read_input_tokens=resp.cache_read_input_tokens,
            duration_ms=resp.duration_ms,
            model=resp.model,
            raw_response=resp.raw_text,
        )

    # ─── prompt 构造 ─────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        archetype_lines = []
        for arch_id, strategy_cls in STRATEGIES.items():
            archetype_lines.append(f"- `{arch_id}`: {strategy_cls.router_description}")
        archetypes_block = "\n".join(archetype_lines)

        return (
            "You are a game-archetype classifier for a desktop AI robot. "
            "Given a user's game description, decide which pre-built archetype best "
            "matches the GAMEPLAY (not theme / asset details).\n\n"
            "Available archetypes:\n"
            f"{archetypes_block}\n\n"
            "Rules:\n"
            "- Match on CORE GAMEPLAY MECHANIC, not on theme. "
            "  E.g. 'catch falling bananas' AND 'catch falling stars' are both `catch_v1`.\n"
            "- IGNORE mood / difficulty / visual modifiers when matching. "
            "  E.g. '黑暗中打地鼠' is still `whack_v1` (modifier=dark); '慢动作切水果' "
            "  is still `slicer_v1` (modifier=slowmo); '硬核躲陨石' is still `dodger_v1` "
            "  (modifier=hardcore). These modifiers are handled downstream, not here.\n"
            "- If the user's description does NOT clearly map to one of the listed "
            "  archetypes, return archetype_id=null. Do NOT force a match.\n"
            "- confidence: 0.0-1.0. 0.9+ = obvious match; 0.5-0.8 = plausible but ambiguous; "
            "  <0.5 = weak match (caller will fallback to a heavier path).\n"
            "- Be CONSERVATIVE. Returning null is better than a wrong archetype.\n\n"
            "Output STRICT JSON only, no markdown, no explanation:\n"
            '{"archetype_id": "catch_v1" | null, "confidence": 0.0-1.0}\n'
        )

    @staticmethod
    def _build_user_message(user_text: str, input_mode: str) -> str:
        # 不把 input_mode 喂给 LLM —— P2.3 smoke 实测发现 input_mode=body_pose
        # 跟 whack/slicer 描述里的 "NO body-pose" / "hand swipe" 冲突 → router
        # 过度保守返 null，错失 3 个本应命中的 archetype（黑暗打地鼠 / 慢动作切
        # 水果 / 拍金币）。Router 只按用户文本分类玩法，input_mode 留给 customizer
        # 在生成 GDScript 时按需翻译（v2 各 archetype 都支持多种 input）。
        _ = input_mode  # 保留参数签名向后兼容
        return (
            f"User description: {user_text}\n\n"
            "Classify the game archetype. JSON only."
        )

    # ─── 解析 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_decision(parsed: dict) -> RouterDecision:
        """从 LLM 返回的 dict 提取 decision，越界 / 不合法字段全部 clamp 到安全值。"""
        archetype_id = parsed.get("archetype_id")
        if isinstance(archetype_id, str):
            archetype_id = archetype_id.strip() or None
        else:
            archetype_id = None

        # 不在注册表里 → 强制 null + confidence 0
        if archetype_id is not None and archetype_id not in STRATEGIES:
            logger.warning(
                "[Fastpath/Router] LLM returned unknown archetype_id=%r, forcing null",
                archetype_id,
            )
            archetype_id = None

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        # archetype_id 为 null 时，confidence 强制 0（不允许 LLM 说"我不知道但很确定"）
        if archetype_id is None:
            confidence = 0.0

        return RouterDecision(archetype_id=archetype_id, confidence=confidence)
