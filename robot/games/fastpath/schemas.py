"""GameRecipe schema + 路由 / 编译相关 Pydantic models.

GameRecipe v0.1 — Phase 1 只覆盖接物（catch_v1）所需字段。后续 archetype
按需扩展，**新增字段从尾部追加**保持向后兼容；不兼容时升 schema_version。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = "0.2"  # 0.2: 加 ItemTag / Director / hidden_tech_enabled

I18nText = dict[str, str]  # {"en": "...", "zh-Hans": "..."}


# ─── Phase 2 systems-emergence 字段 ──────────────────────────────────────────


class ItemTag(str, Enum):
    """物品的化学反应类型 — 跟 GDScript Chemistry.Tag enum 严格 1:1 对应。

    Customizer / LLM 输出 string value（"fire" / "water" ...），strategy compile
    时把字符串 → Chemistry.Tag.* int（FIRE=1, WATER=2, ...）写进 GDScript。
    NEUTRAL 是默认值，对应 Chemistry 矩阵里"什么都不发生"——老 entity 没标
    tag 仍能跑（碰撞返 no_op，score_mul/damage_mul 都是 1.0）。
    """

    NEUTRAL = "neutral"
    FIRE = "fire"
    WATER = "water"
    GRASS = "grass"
    METAL = "metal"
    EXPLOSIVE = "explosive"


class Director(str, Enum):
    """LLM 当 game director 时选的玩法 modifier — 跟 GDScript Director.setup()
    接受的 string flag 严格 1:1 对应。

    10 个 director 三档分级（见 _template_fastpath/scripts/director.gd 注释）：
    - 状态查询型: tense / relaxing / chaotic / puzzle —— strategy 在 spawn / 难度
      用 difficulty_mods() 应用
    - 自动应用型: slowmo / puzzle —— Director.setup() 直接改 Engine.time_scale
    - helper 函数型: gravity_flip / magnet / wind / dark / boss_round —— strategy
      调对应 helper 函数取参数应用
    """

    TENSE = "tense"
    RELAXING = "relaxing"
    CHAOTIC = "chaotic"
    PUZZLE = "puzzle"
    GRAVITY_FLIP = "gravity_flip"
    SLOWMO = "slowmo"
    MAGNET = "magnet"
    DARK = "dark"
    WIND = "wind"
    BOSS_ROUND = "boss_round"


class Meta(BaseModel):
    """游戏元数据（标题 / 描述 / 图标）。所有展示字段由 Customizer 生成。"""

    title_i18n: I18nText = Field(default_factory=lambda: {"en": "Game", "zh-Hans": "游戏"})
    description_i18n: I18nText = Field(
        default_factory=lambda: {"en": "Play and have fun", "zh-Hans": "畅玩"}
    )
    icon: str = "🎮"

    @field_validator("title_i18n", "description_i18n")
    @classmethod
    def _ensure_bilingual(cls, v: I18nText) -> I18nText:
        en = (v.get("en") or "").strip()
        zh = (v.get("zh-Hans") or v.get("zh") or "").strip()
        if not en or not zh:
            raise ValueError("i18n text must include both 'en' and 'zh-Hans'")
        return {"en": en, "zh-Hans": zh}


class Theme(BaseModel):
    """主题相关，决定整体观感。值必须命中 AssetCatalog。"""

    palette: str = "warm"          # AssetCatalog.palettes 之一
    background_hint: str = "beach"  # AssetCatalog.backgrounds 之一


class EntitySpec(BaseModel):
    """单个游戏实体（玩家 / 可接物 / 障碍）。

    `emoji` 是 Phase 2 W1.5 起的**主字段**——Customizer 直接输出 emoji 字符，
    Compiler 用 PIL+Apple Color Emoji 现场渲染成 PNG。`sprite_hint` 保留作
    legacy + reference instance 用：catalog ID（如 "monkey"），有此字段时
    Compiler 优先用 catalog 预渲染的 PNG（速度 0ms vs 100ms render）。
    """

    emoji: str = ""                 # Phase 2+ primary: any Unicode emoji
    sprite_hint: str = "default"    # Phase 1 legacy: catalog ID
    score_value: int = 1            # 拾取得分（drop 用）
    lives_cost: int = 1             # 命中扣命（obstacle 用）
    control: Literal["face_x", "tilt_x", "static"] = "face_x"
    # v0.2 新增：chemistry tag。Customizer 根据用户描述选材质类型，strategy
    # 在 chemistry 启用时（Director.chemistry_enabled() == true）查表反应。
    # 默认 NEUTRAL → 走 no_op → 完全等价 v0.1 行为，老 recipe 不需要改。
    tag: ItemTag = ItemTag.NEUTRAL


class Entities(BaseModel):
    player: EntitySpec = Field(default_factory=lambda: EntitySpec(emoji="🐵", sprite_hint="monkey", control="face_x"))
    drop: EntitySpec = Field(default_factory=lambda: EntitySpec(emoji="🍌", sprite_hint="banana", score_value=1))
    obstacle: EntitySpec = Field(default_factory=lambda: EntitySpec(emoji="💣", sprite_hint="bomb", lives_cost=1))


class Params(BaseModel):
    """游戏可调参数。Customizer 在合理范围内填值，Compiler 不再二次校验。"""

    spawn_rate_per_sec: float = Field(default=1.8, ge=0.2, le=8.0)
    fall_speed_min: float = Field(default=200.0, ge=50.0, le=900.0)
    fall_speed_max: float = Field(default=400.0, ge=80.0, le=1500.0)
    obstacle_probability: float = Field(default=0.25, ge=0.0, le=0.8)
    win_score: int = Field(default=30, ge=5, le=200)
    max_lives: int = Field(default=3, ge=1, le=10)
    catcher_radius: float = Field(default=44.0, ge=20.0, le=120.0)

    @field_validator("fall_speed_max")
    @classmethod
    def _max_ge_min(cls, v: float, info) -> float:
        fall_min = info.data.get("fall_speed_min", 200.0)
        if v < fall_min:
            raise ValueError(f"fall_speed_max ({v}) must be >= fall_speed_min ({fall_min})")
        return v


class Audio(BaseModel):
    """音频选择。值必须存在于 robot/games/assets/audio/manifest.json。

    BGM 默认 `bgm_playful`（casual / 接物玩法最合适）；SFX 默认 pickup/hit/
    level_fail 都已验证在 manifest 里。Customizer 输出未命中 manifest 的
    名字时 `_harvest_audio` 会 warn 但 zip 里没该音频文件——iOS 端
    AudioManager.play() 会静默失败，不会 crash。
    """

    bgm: str = "bgm_playful"
    sfx_pickup: str = "pickup"
    sfx_hit: str = "hit"
    sfx_gameover: str = "level_fail"


class GameRecipe(BaseModel):
    """完整游戏配方 — Customizer 的输出 / Compiler 的输入。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    archetype_id: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    meta: Meta = Field(default_factory=Meta)
    theme: Theme = Field(default_factory=Theme)
    entities: Entities = Field(default_factory=Entities)
    params: Params = Field(default_factory=Params)
    audio: Audio = Field(default_factory=Audio)
    # v0.2 — LLM 当 game director：从 10 个 modifier 里挑 0-3 个组合，每个
    # archetype 在编译时按这个组合改 spawn / 速度 / lives / chemistry 启用。
    # max_length=3 防止 LLM 一股脑全选导致 modifier 互相冲突 + 玩家体感混乱。
    # 默认 [] = 完全等价 v0.1 行为（老 recipe 不需要改）。
    directors: list[Director] = Field(default_factory=list, max_length=3)
    # v0.2 — 是否启用 archetype 的"隐藏 tech"路径（charge_catch / charge_slash /
    # multi_touch_chord / disguise_targets 等 skill ceiling 机制）。strategy
    # 在 P3+ 写 v2 实现时把对应代码段用这个 flag 包起来。默认 False = 走 v1
    # 行为，让 LLM / 用户能控制深度档位。
    hidden_tech_enabled: bool = False


# ─── Router 输出 ──────────────────────────────────────────────────────────────

class RouterDecision(BaseModel):
    """L1 Router 的输出 — 纯分类决策。"""

    model_config = ConfigDict(extra="forbid")

    archetype_id: str | None
    confidence: float = Field(ge=0.0, le=1.0)


# ─── Compiler 输出 ────────────────────────────────────────────────────────────

class CompileResult(BaseModel):
    """RecipeCompiler 的输出。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    game_dir: Path
    archetype_id: str
    files_written: list[str]
