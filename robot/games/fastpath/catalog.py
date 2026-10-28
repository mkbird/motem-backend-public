"""Asset catalog — Customizer 输出的 sprite_hint / palette 等字段在这里
落到具体的视觉资源。

Phase 1 只用 `color`（RGB tuple）。Compiler 生成 GDScript 时用 draw_circle /
draw_rect + Color() 渲染实体，**不依赖 PNG 文件**。Phase 1 Week 3 起会扩展到
sprite-based 渲染，那时 `asset_path` 字段才会被用上。

Fuzzy match 策略：完全匹配 → 子串匹配 → 默认 fallback。**永不抛错**——
匹配不到时退化到 default，保证 Compiler 总能产出可玩游戏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# ─── Catalog 类型 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CatalogEntry:
    id: str
    color: tuple[float, float, float]    # RGB in 0..1 — sprite 缺失时退化用
    aliases: tuple[str, ...] = ()        # 模糊匹配辅助词
    emoji: str = ""                      # 渲染成 sprite PNG 的 emoji（Phase 2）


@dataclass(frozen=True)
class Palette:
    """4 色调色板：bg / accent / good / bad — 对应背景 / 玩家 / 可接物 / 障碍。"""

    id: str
    bg: tuple[float, float, float]
    accent: tuple[float, float, float]
    good: tuple[float, float, float]
    bad: tuple[float, float, float]


@dataclass(frozen=True)
class BackgroundEntry:
    id: str
    sky: tuple[float, float, float]
    aliases: tuple[str, ...] = ()


# ─── Catalog 数据 ────────────────────────────────────────────────────────────

_CHARACTERS: tuple[CatalogEntry, ...] = (
    CatalogEntry("monkey",    (0.78, 0.55, 0.30), ("ape", "猴"),         "🐵"),
    CatalogEntry("dog",       (0.86, 0.66, 0.42), ("puppy", "狗"),       "🐶"),
    CatalogEntry("cat",       (0.55, 0.55, 0.60), ("kitty", "猫"),       "🐱"),
    CatalogEntry("robot",     (0.65, 0.70, 0.78), ("机器人", "bot"),     "🤖"),
    CatalogEntry("wizard",    (0.55, 0.32, 0.78), ("mage", "巫师"),      "🧙"),
    CatalogEntry("ninja",     (0.20, 0.20, 0.25), ("shinobi", "忍者"),   "🥷"),
    CatalogEntry("astronaut", (0.88, 0.90, 0.95), ("spaceman", "宇航员"), "🧑‍🚀"),
    CatalogEntry("alien",     (0.40, 0.85, 0.55), ("外星人", "ufo"),     "👽"),
    CatalogEntry("chef",      (0.95, 0.92, 0.85), ("cook", "厨师"),      "🧑‍🍳"),
    CatalogEntry("panda",     (0.92, 0.92, 0.92), ("熊猫",),             "🐼"),
)

_OBJECTS: tuple[CatalogEntry, ...] = (
    # 水果 / 食物 — 中文用户的"接 XXX"游戏 80%+ 都是食物
    CatalogEntry("banana",     (0.96, 0.85, 0.30), ("香蕉",),                      "🍌"),
    CatalogEntry("apple",      (0.86, 0.20, 0.20), ("苹果",),                      "🍎"),
    CatalogEntry("orange",     (0.95, 0.55, 0.18), ("橙",),                        "🍊"),
    CatalogEntry("peach",      (0.95, 0.65, 0.65), ("桃子", "桃"),                 "🍑"),
    CatalogEntry("strawberry", (0.95, 0.20, 0.30), ("草莓",),                      "🍓"),
    CatalogEntry("watermelon", (0.30, 0.75, 0.40), ("西瓜",),                      "🍉"),
    CatalogEntry("grape",      (0.55, 0.30, 0.78), ("葡萄",),                      "🍇"),
    CatalogEntry("dumpling",   (0.95, 0.92, 0.80), ("包子", "饺子", "bao"),         "🥟"),
    CatalogEntry("rice_ball",  (0.92, 0.92, 0.92), ("饭团", "rice"),               "🍙"),
    CatalogEntry("bread",      (0.90, 0.72, 0.45), ("面包",),                      "🍞"),
    CatalogEntry("cookie",     (0.78, 0.55, 0.30), ("饼干",),                      "🍪"),
    CatalogEntry("cake",       (0.95, 0.85, 0.75), ("蛋糕",),                      "🍰"),
    CatalogEntry("donut",      (0.95, 0.78, 0.55), ("甜甜圈",),                    "🍩"),
    CatalogEntry("mushroom",   (0.85, 0.45, 0.45), ("蘑菇",),                      "🍄"),
    # 抽象 / 收集物
    CatalogEntry("coin",       (0.97, 0.78, 0.18), ("金币", "gold"),               "🪙"),
    CatalogEntry("star",       (0.98, 0.85, 0.30), ("星星",),                      "⭐"),
    CatalogEntry("heart",      (0.92, 0.30, 0.45), ("心",),                        "❤️"),
    CatalogEntry("gem",        (0.40, 0.78, 0.95), ("宝石", "diamond"),            "💎"),
    CatalogEntry("present",    (0.85, 0.30, 0.55), ("礼物", "gift"),               "🎁"),
    CatalogEntry("candy",      (0.95, 0.55, 0.80), ("糖", "sweet"),                "🍬"),
)

_OBSTACLES: tuple[CatalogEntry, ...] = (
    CatalogEntry("bomb",      (0.18, 0.18, 0.20), ("炸弹",),  "💣"),
    CatalogEntry("rock",      (0.45, 0.42, 0.40), ("石头", "stone"), "🪨"),
    CatalogEntry("spike",     (0.55, 0.55, 0.60), ("刺",),    "🌵"),
    CatalogEntry("fire",      (0.95, 0.35, 0.15), ("火",),    "🔥"),
    CatalogEntry("anvil",     (0.30, 0.30, 0.35), ("铁砧",),  "🗿"),
    CatalogEntry("lightning", (0.90, 0.85, 0.25), ("闪电",),  "⚡"),
)

_BACKGROUNDS: tuple[BackgroundEntry, ...] = (
    BackgroundEntry("beach",      (0.55, 0.85, 0.95), ("海滩", "sea")),
    BackgroundEntry("forest",     (0.30, 0.55, 0.35), ("森林", "woods")),
    BackgroundEntry("space",      (0.05, 0.05, 0.18), ("太空", "galaxy")),
    BackgroundEntry("city",       (0.55, 0.60, 0.70), ("城市", "urban")),
    BackgroundEntry("cave",       (0.25, 0.20, 0.25), ("洞穴",)),
    BackgroundEntry("underwater", (0.10, 0.40, 0.70), ("水下", "ocean")),
)

_PALETTES: tuple[Palette, ...] = (
    Palette("warm",       (0.99, 0.86, 0.66), (0.92, 0.42, 0.20), (0.96, 0.78, 0.25), (0.20, 0.20, 0.20)),
    Palette("cool",       (0.70, 0.88, 0.96), (0.20, 0.55, 0.85), (0.30, 0.85, 0.78), (0.95, 0.30, 0.30)),
    Palette("vibrant",    (0.18, 0.18, 0.25), (0.95, 0.25, 0.65), (0.30, 0.95, 0.55), (0.95, 0.85, 0.10)),
    Palette("pastel",     (0.95, 0.92, 0.95), (0.65, 0.75, 0.90), (0.85, 0.92, 0.65), (0.95, 0.65, 0.70)),
    Palette("monochrome", (0.90, 0.90, 0.92), (0.30, 0.30, 0.30), (0.55, 0.55, 0.55), (0.10, 0.10, 0.10)),
    Palette("neon",       (0.05, 0.05, 0.08), (0.20, 0.95, 0.95), (0.95, 0.95, 0.20), (0.95, 0.20, 0.55)),
)


# ─── Fuzzy match helpers ─────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _match(items: Iterable[CatalogEntry | BackgroundEntry | Palette], hint: str):
    """完全匹配 → 子串匹配 → None。永不抛错；caller 决定 fallback。"""
    h = _normalize(hint)
    if not h:
        return None
    items_list = list(items)
    # exact id
    for it in items_list:
        if _normalize(it.id) == h:
            return it
    # alias exact
    for it in items_list:
        aliases = getattr(it, "aliases", ()) or ()
        for a in aliases:
            if _normalize(a) == h:
                return it
    # substring on id
    for it in items_list:
        if h in _normalize(it.id) or _normalize(it.id) in h:
            return it
    # substring on aliases
    for it in items_list:
        aliases = getattr(it, "aliases", ()) or ()
        for a in aliases:
            if h in _normalize(a) or _normalize(a) in h:
                return it
    return None


# ─── 公开 API ────────────────────────────────────────────────────────────────

class AssetCatalog:
    """sprite_hint / palette / background_hint → 具体资源。"""

    def __init__(
        self,
        characters: tuple[CatalogEntry, ...] = _CHARACTERS,
        objects: tuple[CatalogEntry, ...] = _OBJECTS,
        obstacles: tuple[CatalogEntry, ...] = _OBSTACLES,
        backgrounds: tuple[BackgroundEntry, ...] = _BACKGROUNDS,
        palettes: tuple[Palette, ...] = _PALETTES,
    ) -> None:
        self._characters = characters
        self._objects = objects
        self._obstacles = obstacles
        self._backgrounds = backgrounds
        self._palettes = palettes

    @classmethod
    def default(cls) -> "AssetCatalog":
        return cls()

    def match_character(self, hint: str) -> CatalogEntry:
        return _match(self._characters, hint) or self._characters[0]  # default: monkey

    def match_object(self, hint: str) -> CatalogEntry:
        return _match(self._objects, hint) or self._objects[0]  # default: banana

    def match_obstacle(self, hint: str) -> CatalogEntry:
        return _match(self._obstacles, hint) or self._obstacles[0]  # default: bomb

    def match_background(self, hint: str) -> BackgroundEntry:
        return _match(self._backgrounds, hint) or self._backgrounds[0]  # default: beach

    def match_palette(self, hint: str) -> Palette:
        return _match(self._palettes, hint) or self._palettes[0]  # default: warm

    # 给 Customizer prompt 用：序列化成可读列表
    # Phase 2 W1.5 起 sprite 走 emoji 直出，characters/objects/obstacles
    # catalog 不再暴露给 LLM（LLM 直接选 Unicode emoji 字符）。
    # 只保留 enum-constrained 字段：palette / background / audio。
    def list_for_prompt(self) -> dict:
        return {
            "palettes": [p.id for p in self._palettes],
            "backgrounds": [b.id for b in self._backgrounds],
            "bgm": list(_VALID_BGM),
            "sfx": list(_VALID_SFX),
        }


# 实测在 robot/games/assets/audio/manifest.json 的 ID（2026-05 抽样）。
# Customizer 必须从这两个 list 里挑，挑别的会被 _harvest_audio 静默丢弃
# → zip 里没该音频 → iOS 端 AudioManager.play() no-op。
# 列表保持手动短列表（不动态读 manifest）：让 Customizer prompt token 可控。
_VALID_BGM: tuple[str, ...] = (
    "bgm_playful", "bgm_action", "bgm_calm", "bgm_chase",
    "bgm_dreamy", "bgm_mystery", "bgm_8bit", "bgm_8bit_bright",
)
_VALID_SFX: tuple[str, ...] = (
    "pickup", "hit", "level_fail", "level_complete",
    "score_up", "life_lost", "click", "jump", "dash", "collide",
)
