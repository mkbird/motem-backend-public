"""Customizer — Fast Path 第 2 次 LLM 调用，做完整个性化。

输入：user_text + archetype_id（来自 Router）
输出：完整 GameRecipe（含 title / sprite_hint / palette / params / audio 全字段）

设计原则：
- archetype 决定**玩法骨架**；Customizer 决定**所有个性化字段**
- 同 archetype + 不同用户描述 → 必须产出视觉 / 文案 / 手感都不同的 recipe
  （单测有"多样性"门槛）
- 越界 / 缺失字段全部 clamp 到默认值，**永不抛错让 caller fallback**——
  这层只在 LLM 调用本身失败时才 raise
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from robot.games.fastpath.catalog import AssetCatalog
from robot.games.fastpath.llm_client import JSONChatClient, LLMCallError
from robot.games.fastpath.schemas import Director, GameRecipe, ItemTag
from robot.games.fastpath.strategies import STRATEGIES, ArchetypeStrategy

logger = logging.getLogger(__name__)


# v0.2 cheatsheet：放在 system prompt 末尾，教 LLM 如何选 director + tag。
# 用英文 + 双语示例（用户描述常是中文，触发词检测要看中文原文）。
_DIRECTOR_TAG_CHEATSHEET = """v0.2 — director + tag selection rules:

A. "directors" (0-3 items, all optional): pick modifiers that match user's tone.
   - "tense":        user says "紧张" / "刺激" / "intense" / "scary" / "frenetic"
   - "relaxing":     user says "解压" / "轻松" / "chill" / "easy" / "relax"
   - "chaotic":      user says "混乱" / "wild" / "crazy" / "魔性"
   - "puzzle":       user says "解谜" / "动脑" / "puzzle" / "思考"
   - "gravity_flip": user mentions "重力反转" / "颠倒" / "anti-gravity"
   - "slowmo":       user mentions "慢动作" / "slow motion" / "时间慢"
   - "magnet":       user mentions "磁铁" / "吸引" / "magnet"
   - "dark":         user mentions "黑暗" / "闪烁" / "blackout"
   - "wind":         user mentions "风" / "wind" / "侧风"
   - "boss_round":   user mentions "boss" / "巨型" / "大魔王"
   Default: empty array [] when no clear modifier hint. NEVER force-pick.

B. "hidden_tech_enabled": boolean, default false.
   - Set TRUE only when user EXPLICITLY asks for "硬核" / "hardcore" /
     "challenging" / "深度" / "skill mastery" style.
   - Otherwise leave as false (preserves friendlier difficulty curve).

C. Entity "tag" field (each entity may set independently, default "neutral"):
   - "neutral":  generic items (food 包子/寿司/水果, animals 狗/猫/恐龙, vehicles 车/船)
   - "fire":     anything fire-related (🔥/熔岩/火球/heatable obstacles)
   - "water":    water/ice/liquid items (💧/❄️/🌊/💦)
   - "grass":    plants/leaves/wood (🌿/🍃/🌳/🌱)
   - "metal":    coins/iron/steel/钢铁 (🪙/⚙️/🥫)
   - "explosive": bombs/firecrackers (💣/🧨/🎆)
   When unsure → "neutral" (safe default, no chemistry trigger)."""


class CustomizerError(Exception):
    """Customizer 不可恢复错误（LLM 失败 / 解析全军覆没）。caller 应 fallback。"""


@dataclass
class CustomizerCall:
    recipe: GameRecipe
    tokens_in: int
    tokens_out: int
    cache_read_input_tokens: int
    duration_ms: int
    model: str
    raw_response: str


class Customizer:
    def __init__(
        self,
        llm: JSONChatClient,
        catalog: AssetCatalog | None = None,
        *,
        max_tokens: int = 900,
        temperature: float = 0.8,  # 偏高鼓励多样性，反"千篇一律"
    ) -> None:
        self._llm = llm
        self._catalog = catalog or AssetCatalog.default()
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def customize(
        self,
        *,
        user_text: str,
        archetype_id: str,
        confidence: float = 1.0,
    ) -> CustomizerCall:
        strategy_cls = STRATEGIES.get(archetype_id)
        if strategy_cls is None:
            raise CustomizerError(f"unknown archetype_id={archetype_id!r}")
        strategy = strategy_cls()

        system = self._build_system_prompt(strategy)
        user = self._build_user_message(user_text, archetype_id)

        try:
            resp = await self._llm.chat_json(
                system=system,
                user=user,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except LLMCallError as e:
            raise CustomizerError(f"LLM call failed: {e}") from e

        recipe = self._build_recipe(
            parsed=resp.parsed,
            archetype_id=archetype_id,
            confidence=confidence,
        )

        return CustomizerCall(
            recipe=recipe,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            cache_read_input_tokens=resp.cache_read_input_tokens,
            duration_ms=resp.duration_ms,
            model=resp.model,
            raw_response=resp.raw_text,
        )

    # ─── prompt 构造 ─────────────────────────────────────────────────────────

    def _build_system_prompt(self, strategy: ArchetypeStrategy) -> str:
        catalog_dump = self._catalog.list_for_prompt()
        # v0.2: 自动 inject directors / hidden_tech_enabled / entity tag 字段到
        # 每个 strategy 的 customizer schema，避免每个 strategy 自己重复声明
        # 这些通用字段（v2 archetype 加进来也无须改自己的 schema）。
        injected_schema = self._inject_v0_2_fields(strategy.customizer_schema)
        schema_text = json.dumps(injected_schema, ensure_ascii=False, indent=2)
        examples_block = "\n\n".join(
            f"Example {i+1}:\nuser_input: {ex['user_input']}\noutput:\n{json.dumps(ex['recipe_fragment'], ensure_ascii=False, indent=2)}"
            for i, ex in enumerate(strategy.customizer_examples)
        )
        catalog_text = json.dumps(catalog_dump, ensure_ascii=False, indent=2)

        return (
            f"You are a game personalization specialist for the `{strategy.archetype_id}` "
            "archetype. Given a user's description, output a complete GameRecipe JSON "
            "that captures the user's intent in: title (bilingual), description, icon, "
            "theme palette, theme background, entity emojis, gameplay parameters, "
            "audio choices, **plus optional v0.2 fields below**.\n\n"
            "Constrained-value catalog (you MUST pick `palette`, `background_hint`, "
            "`bgm`, `sfx_*` from these exact strings — values outside fall back to "
            "defaults):\n"
            f"{catalog_text}\n\n"
            "Output schema (STRICT):\n"
            f"{schema_text}\n\n"
            f"{examples_block}\n\n"
            "Rules:\n"
            "- title_i18n.en: 1-3 words, catchy, no quotes / no punctuation. "
            "  zh-Hans: 2-5 Chinese characters.\n"
            "- description_i18n: one concise sentence each. en in English, zh-Hans in "
            "  Simplified Chinese.\n"
            "- icon: a SINGLE emoji that fits the theme.\n"
            "- **entity emojis** (`player.emoji`, `drop.emoji`, `obstacle.emoji`): "
            "  pick the single Unicode emoji that BEST visually represents what the "
            "  user described. Examples: 包子→🥟  寿司→🍣  火锅→🍲  恐龙→🦖  "
            "  机器人→🤖  飞机→✈️  汽车→🚗. If a concept has no direct emoji, pick "
            "  the most visually similar one (never default to 🍌/🐵 unless the user "
            "  actually mentioned banana/monkey).\n"
            "- palette / background_hint / bgm / sfx_* MUST come from the catalog above.\n"
            "- params: tune to user's described difficulty (easy / normal / hard).\n"
            "- Be DIVERSE: same archetype + different user inputs must produce visibly "
            "  different recipes (different titles, different emojis, different themes).\n\n"
            f"{_DIRECTOR_TAG_CHEATSHEET}\n\n"
            "Output the JSON object ONLY, no markdown, no commentary."
        )

    @staticmethod
    def _inject_v0_2_fields(schema: dict) -> dict:
        """把 v0.2 通用字段（directors / hidden_tech_enabled / entity tag）加到
        strategy schema 的 properties 里。deepcopy 避免污染 ClassVar 上的原始
        schema。

        v0.2 字段全部 OPTIONAL —— 不进 required 列表，让 LLM 在不确定时可以
        省略（缺省走默认值 directors=[] / hidden_tech_enabled=False / tag="neutral"
        全部等价 v0.1 行为）。
        """
        s = copy.deepcopy(schema)
        props = s.setdefault("properties", {})

        # 顶层：directors
        props["directors"] = {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [d.value for d in Director],
            },
            "maxItems": 3,
            "description": (
                "Optional. 0-3 game modifiers based on user's mood/tone. "
                "See cheatsheet at end of system prompt for selection rules."
            ),
        }

        # 顶层：hidden_tech_enabled
        props["hidden_tech_enabled"] = {
            "type": "boolean",
            "description": (
                "Optional, default false. Enable ONLY if user explicitly asks "
                "for 'challenging' / 'hardcore' / '硬核' / '挑战'."
            ),
        }

        # entities.{player, drop, obstacle, ...}.tag
        # 每个 archetype 的 entities 字段名可能不同（dodger 用 hazard），所以
        # 遍历所有 entity sub-schema 注入 tag。
        entities_schema = props.get("entities", {})
        entities_props = entities_schema.get("properties", {}) if isinstance(entities_schema, dict) else {}
        tag_field = {
            "type": "string",
            "enum": [t.value for t in ItemTag],
            "description": (
                "Optional, default 'neutral'. Chemistry tag for collision reactions "
                "(only takes effect when 'chaotic' or 'puzzle' director is active). "
                "See cheatsheet for tag selection rules."
            ),
        }
        for ent_key in entities_props:
            ent_schema = entities_props[ent_key]
            if isinstance(ent_schema, dict):
                ent_props = ent_schema.setdefault("properties", {})
                ent_props["tag"] = tag_field

        return s

    @staticmethod
    def _build_user_message(user_text: str, archetype_id: str) -> str:
        return (
            f"Archetype: {archetype_id}\n"
            f"User description: {user_text}\n\n"
            "Output the complete GameRecipe JSON now."
        )

    # ─── 解析 ────────────────────────────────────────────────────────────────

    def _build_recipe(
        self,
        *,
        parsed: dict[str, Any],
        archetype_id: str,
        confidence: float,
    ) -> GameRecipe:
        """从 LLM 输出构造 GameRecipe，单字段不合法时只对该字段降级，不放弃整个 recipe。"""
        payload: dict[str, Any] = {
            "archetype_id": archetype_id,
            "confidence": confidence,
        }
        for key in ("meta", "theme", "entities", "params", "audio"):
            if isinstance(parsed.get(key), dict):
                payload[key] = parsed[key]
        # v0.2 顶层字段：LLM 输出对了直接用；类型不对就让 Pydantic 走 piecewise fallback。
        if isinstance(parsed.get("directors"), list):
            payload["directors"] = parsed["directors"]
        if isinstance(parsed.get("hidden_tech_enabled"), bool):
            payload["hidden_tech_enabled"] = parsed["hidden_tech_enabled"]

        try:
            return GameRecipe(**payload)
        except ValidationError as e:
            logger.warning(
                "[Fastpath/Customizer] full recipe validation failed; "
                "rebuilding piecewise: %s",
                e,
            )

        # Piecewise rebuild —  哪个 sub-schema 坏哪个降级到默认
        recipe = GameRecipe(archetype_id=archetype_id, confidence=confidence)
        for key in ("meta", "theme", "entities", "params", "audio"):
            sub = parsed.get(key)
            if not isinstance(sub, dict):
                continue
            try:
                sub_field = type(getattr(recipe, key))(**sub)
                setattr(recipe, key, sub_field)
            except (ValidationError, TypeError, ValueError) as sub_e:
                logger.warning(
                    "[Fastpath/Customizer] sub-field %s invalid, using default: %s",
                    key, sub_e,
                )
        # v0.2 顶层字段 piecewise 回退：每个字段单独 try，失败时保持默认。
        # 注意 Pydantic 没开 validate_assignment，attribute 赋值不会自动校验 →
        # 这里手动用 Director enum 构造触发 ValueError。
        from robot.games.fastpath.schemas import Director as _Director
        directors_val = parsed.get("directors")
        if isinstance(directors_val, list):
            try:
                if len(directors_val) > 3:
                    logger.warning(
                        "[Fastpath/Customizer] directors len=%d > 3, truncating",
                        len(directors_val),
                    )
                    directors_val = directors_val[:3]
                recipe.directors = [_Director(d) for d in directors_val]
            except (ValueError, TypeError) as sub_e:
                logger.warning(
                    "[Fastpath/Customizer] directors invalid, using default: %s", sub_e,
                )
        hidden_tech_val = parsed.get("hidden_tech_enabled")
        if isinstance(hidden_tech_val, bool):
            recipe.hidden_tech_enabled = hidden_tech_val
        return recipe
