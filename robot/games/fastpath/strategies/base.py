"""ArchetypeStrategy 抽象基类。

每个 archetype（catch / dodger / slicer / whack）独立一个子类，集中提供：
1. router_description：喂给 L1 Router 的一句话分类描述
2. customizer_schema：喂给 Customizer 的 JSON schema（约束输出）
3. customizer_examples：few-shot examples
4. render_ai_zone：根据 GameRecipe 生成 main.gd 的 AI ZONE 内容
5. render_i18n_strings：根据 GameRecipe 生成 i18n_strings.gd 内容

Compiler 只负责拷骨架 + 调用 strategy 的 render_* 方法 + 写 meta.json。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from robot.games.fastpath.catalog import AssetCatalog
from robot.games.fastpath.schemas import GameRecipe


class ArchetypeStrategy(ABC):
    archetype_id: ClassVar[str]
    router_description: ClassVar[str]
    customizer_schema: ClassVar[dict]
    customizer_examples: ClassVar[list[dict]]

    @abstractmethod
    def render_ai_zone(self, recipe: GameRecipe, catalog: AssetCatalog) -> str:
        """生成 main.gd 的 AI ZONE 部分（从 _init_game 开始的所有 hook）。"""

    @abstractmethod
    def render_i18n_strings(self, recipe: GameRecipe) -> str:
        """生成完整的 scripts/i18n_strings.gd 内容。"""

    def required_assets(self, recipe: GameRecipe, catalog: AssetCatalog) -> dict[str, Path]:
        """**Legacy** (Phase 1)：从 catalog 拷预渲染 PNG 到 game/assets/。

        新代码请用 `prepare_assets()`。本方法保留是为 reference instances 跑
        legacy recipe（sprite_hint-only）时还能 work。

        Returns: dict mapping "assets/<filename>.png" → source PNG abs path.
        """
        return {}

    def prepare_assets(
        self,
        recipe: GameRecipe,
        catalog: AssetCatalog,
        target_dir: Path,
    ) -> list[str]:
        """Phase 2+ 主路径：把 recipe.entities.*.emoji 现场渲染成 PNG 写到
        target_dir/assets/*.png。

        子类按需 override；默认实现遍历 `required_assets()` 做 legacy 拷贝
        以保持向后兼容（Phase 1 reference 仍可 compile）。

        Returns: 写入的 game-relative 路径列表（如 "assets/player.png"）。
        """
        import shutil
        from robot.games.fastpath import emoji_renderer

        written: list[str] = []
        for game_rel, source in self.required_assets(recipe, catalog).items():
            if not source.is_file():
                continue
            dest = target_dir / game_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            written.append(game_rel)
        return written
