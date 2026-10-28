"""Game Fast Path subsystem.

把用户文本 → archetype 分类 → 个性化 GameRecipe → 确定性拼装 → Godot 工程。
跳过 Claude Code SDK 的 multi-turn agent loop，命中 archetype 时单次 LLM 调用
即可完成创建。详见 docs/game-fastpath-implementation-plan.md。

模块结构：
- schemas        Pydantic models（GameRecipe / RouterDecision / CompileResult）
- catalog        AssetCatalog（角色 / 物体 / 障碍 / 背景 / 配色 fuzzy match）
- strategies     ArchetypeStrategy 实现（catch_v1 起步）
- compiler       RecipeCompiler — GameRecipe → Godot 工程目录
- observability  JSONL trace writer
- router         L1 Router（Week 2 接入）
- customizer     Customizer（Week 2 接入）
"""

from robot.games.fastpath.schemas import (
    GameRecipe,
    RouterDecision,
    CompileResult,
)
from robot.games.fastpath.catalog import AssetCatalog
from robot.games.fastpath.compiler import RecipeCompiler, CompileError

__all__ = [
    "GameRecipe",
    "RouterDecision",
    "CompileResult",
    "AssetCatalog",
    "RecipeCompiler",
    "CompileError",
]
