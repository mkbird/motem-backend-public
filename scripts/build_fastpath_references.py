"""生成 fast path archetype 的 reference instance。

用硬编码 GameRecipe 跑 Compiler，产出 N 个不同主题的 catch_v1 游戏到
`games/_fastpath_references/<name>/`。**0 LLM 调用，纯确定性**。

用途：
1. 冷启动展示——iOS 首次安装时可以预装这几个，让用户立刻有得玩
2. CI 回归 fixture——每次 Compiler / strategy 改动跑一遍，截图对比
3. 给 Customizer 提供 ground-truth examples——验证 Customizer 生成的
   recipe 跟手工精修的差距

用法：
    python scripts/build_fastpath_references.py            # 写到默认目录
    python scripts/build_fastpath_references.py /tmp/refs  # 写到指定目录
    python scripts/build_fastpath_references.py --force    # 覆盖已存在的
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from robot.games.fastpath import RecipeCompiler
from robot.games.fastpath.schemas import (
    Audio,
    EntitySpec,
    Entities,
    GameRecipe,
    Meta,
    Params,
    Theme,
)


_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = _BACKEND_ROOT / "games" / "_template"
_DEFAULT_OUT = _BACKEND_ROOT / "games" / "_fastpath_references"


# 3 个不同主题的硬编码 catch_v1 recipe — 视觉 / 文案 / 手感差异最大化。
# 故意手工写，跟 Customizer LLM 输出做对比的 ground truth。
REFERENCES: list[tuple[str, GameRecipe]] = [
    (
        "catch_v1_banana_frenzy",
        GameRecipe(
            archetype_id="catch_v1",
            confidence=1.0,
            meta=Meta(
                title_i18n={"en": "Banana Frenzy", "zh-Hans": "香蕉狂热"},
                description_i18n={
                    "en": "A jungle monkey catches falling bananas while dodging coconuts.",
                    "zh-Hans": "丛林里的猴子接住落下的香蕉，躲开椰子。",
                },
                icon="🐵",
            ),
            theme=Theme(palette="warm", background_hint="forest"),
            entities=Entities(
                player=EntitySpec(sprite_hint="monkey", control="face_x"),
                drop=EntitySpec(sprite_hint="banana", score_value=1),
                obstacle=EntitySpec(sprite_hint="rock", lives_cost=1),
            ),
            params=Params(
                spawn_rate_per_sec=1.8,
                fall_speed_min=200, fall_speed_max=420,
                obstacle_probability=0.22,
                win_score=30, max_lives=4,
                catcher_radius=46.0,
            ),
            audio=Audio(bgm="bgm_playful"),
        ),
    ),
    (
        "catch_v1_stardust",
        GameRecipe(
            archetype_id="catch_v1",
            confidence=1.0,
            meta=Meta(
                title_i18n={"en": "Stardust Catcher", "zh-Hans": "拾星者"},
                description_i18n={
                    "en": "Glide through space and collect stardust before meteors hit.",
                    "zh-Hans": "在太空中漂浮，在陨石击中前收集星尘。",
                },
                icon="✨",
            ),
            theme=Theme(palette="neon", background_hint="space"),
            entities=Entities(
                player=EntitySpec(sprite_hint="astronaut", control="face_x"),
                drop=EntitySpec(sprite_hint="star", score_value=1),
                obstacle=EntitySpec(sprite_hint="lightning", lives_cost=1),
            ),
            params=Params(
                spawn_rate_per_sec=2.4,
                fall_speed_min=260, fall_speed_max=520,
                obstacle_probability=0.30,
                win_score=40, max_lives=3,
                catcher_radius=42.0,
            ),
            audio=Audio(bgm="bgm_action"),
        ),
    ),
    (
        "catch_v1_fruit_basket",
        GameRecipe(
            archetype_id="catch_v1",
            confidence=1.0,
            meta=Meta(
                title_i18n={"en": "Fruit Basket", "zh-Hans": "水果篮子"},
                description_i18n={
                    "en": "Help the chef catch fresh fruits dropping from the orchard.",
                    "zh-Hans": "帮厨师接住果园里掉下来的新鲜水果。",
                },
                icon="🍎",
            ),
            theme=Theme(palette="pastel", background_hint="beach"),
            entities=Entities(
                player=EntitySpec(sprite_hint="chef", control="face_x"),
                drop=EntitySpec(sprite_hint="apple", score_value=1),
                obstacle=EntitySpec(sprite_hint="spike", lives_cost=1),
            ),
            params=Params(
                spawn_rate_per_sec=1.4,
                fall_speed_min=180, fall_speed_max=340,
                obstacle_probability=0.18,
                win_score=20, max_lives=5,
                catcher_radius=50.0,
            ),
            audio=Audio(bgm="bgm_calm"),
        ),
    ),
]


def main() -> int:
    p = argparse.ArgumentParser(description="Build hardcoded fast-path reference games.")
    p.add_argument("out", nargs="?", default=str(_DEFAULT_OUT),
                   help=f"output directory (default: {_DEFAULT_OUT})")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing references")
    args = p.parse_args()

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    compiler = RecipeCompiler(_TEMPLATE_DIR)
    summary: list[tuple[str, str, str]] = []

    for name, recipe in REFERENCES:
        target = out_root / name
        if target.exists():
            if not args.force:
                print(f"  ⏭  {name} exists (use --force to rebuild)")
                continue
            shutil.rmtree(target)

        result = compiler.compile(recipe, target)
        summary.append((
            name,
            recipe.meta.title_i18n["en"],
            recipe.meta.title_i18n["zh-Hans"],
        ))
        print(f"  ✓ {name} → {target}")
        print(f"      title:  {recipe.meta.title_i18n['en']} / {recipe.meta.title_i18n['zh-Hans']}")
        print(f"      theme:  {recipe.theme.palette} / {recipe.theme.background_hint}")
        print(f"      sprites: {recipe.entities.player.sprite_hint} / "
              f"{recipe.entities.drop.sprite_hint} / {recipe.entities.obstacle.sprite_hint}")
        print(f"      files:  {len(result.files_written)} written")

    print(f"\nBuilt {len(summary)} reference games at: {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
