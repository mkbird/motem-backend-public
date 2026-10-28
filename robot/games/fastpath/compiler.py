"""RecipeCompiler — GameRecipe JSON → Godot 工程目录。

纯 Python，零 LLM 调用。流程：
1. 拷 `_template/` 骨架到 target_dir
2. 截断 `scenes/main.gd` 里 AI ZONE 的占位 hook（pass # TODO(AI) 那一段），
   把 strategy 渲染的 GDScript 拼上去
3. 用 strategy 渲染的 i18n_strings.gd 覆盖默认那份
4. 写 meta.json（含 archetype_id / recipe_version / provenance="fastpath" /
   完整 recipe 快照，供后续 modify 路径识别）

不在本文件内做的事：
- gdparse 静态校验（Phase 1 暂不强制，先让架构端到端跑通；后续可加）
- background.png 主题替换（Phase 1 用 _template 自带默认 PNG）
- sprite PNG 注入（Phase 1 用 draw_circle/draw_rect，不依赖 sprite 文件）

失败模式：CompileError raise，caller（streaming.py 网关）决定 fallback 到 SDK。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from robot.games.fastpath.catalog import AssetCatalog
from robot.games.fastpath.schemas import CompileResult, GameRecipe
from robot.games.fastpath.strategies import STRATEGIES

logger = logging.getLogger(__name__)


# 截断标记：main.gd 中这一行（含）开始的所有内容都会被替换为 strategy 输出。
# 与 _template/scenes/main.gd 当前内容严格对齐；若骨架重构需同步修改。
_AI_ZONE_TRUNCATE_MARKER = "# REQUIRED: called once when player taps to start"


class CompileError(Exception):
    """Compiler 不可恢复错误。caller 应 fallback 到 slow path。"""


@dataclass
class _IgnoreSpec:
    """copytree ignore — 跳过这些路径不入新游戏目录。"""

    names: tuple[str, ...] = (
        ".godot",          # Godot 编辑器缓存
        ".import",         # 旧版 Godot import 缓存
        "__pycache__",
    )

    def __call__(self, _src: str, names: list[str]) -> list[str]:
        skip: list[str] = []
        for n in names:
            if n in self.names or n.endswith(".import"):
                skip.append(n)
        return skip


class RecipeCompiler:
    def __init__(
        self,
        template_dir: Path,
        catalog: AssetCatalog | None = None,
        addon_dirs: tuple[Path, ...] = (),
    ) -> None:
        """fast path 骨架编译器。

        ``template_dir``：硬件接口契约层（默认 `games/_template/`，两条路径共享）。
        ``addon_dirs``：在 base 之上叠加的目录，按顺序 ``dirs_exist_ok=True`` 拷
        贝。fast path 用来叠 ``games/_template_fastpath/`` 里的 juice helpers /
        result_card 等 fast 专属基础设施。缺失的 addon dir 被静默跳过（让 v2
        基础设施分批接入时不至于 boot 失败）。
        """
        if not template_dir.is_dir():
            raise CompileError(f"template_dir not found: {template_dir}")
        self.template_dir = template_dir
        self.addon_dirs = addon_dirs
        self.catalog = catalog or AssetCatalog.default()

    # ─── 主入口 ──────────────────────────────────────────────────────────────

    def compile(self, recipe: GameRecipe, target_dir: Path, input_mode: str = "touch") -> CompileResult:
        strategy_cls = STRATEGIES.get(recipe.archetype_id)
        if strategy_cls is None:
            raise CompileError(
                f"unknown archetype_id={recipe.archetype_id!r}; "
                f"known: {sorted(STRATEGIES.keys())}"
            )
        strategy = strategy_cls()

        if target_dir.exists():
            raise CompileError(f"target_dir already exists: {target_dir}")

        target_dir.parent.mkdir(parents=True, exist_ok=True)

        # 1. 拷骨架（base 协议层 + 可选 addon 层）
        shutil.copytree(self.template_dir, target_dir, ignore=_IgnoreSpec())
        for addon in self.addon_dirs:
            if addon.is_dir():
                shutil.copytree(
                    addon, target_dir,
                    ignore=_IgnoreSpec(), dirs_exist_ok=True,
                )

        files_written: list[str] = []

        # 2. 改写 main.gd（截断 AI ZONE 占位 + 拼 strategy 输出）
        main_gd_path = target_dir / "scenes" / "main.gd"
        if not main_gd_path.exists():
            raise CompileError(f"template missing scenes/main.gd at {main_gd_path}")
        skeleton = main_gd_path.read_text(encoding="utf-8")
        truncated = _truncate_at_ai_zone(skeleton)
        ai_zone = strategy.render_ai_zone(recipe, self.catalog)
        main_gd_path.write_text(truncated + ai_zone, encoding="utf-8")
        files_written.append("scenes/main.gd")

        # 2b. 让 strategy 准备 sprite assets（Phase 2+ 路径：emoji 渲染；
        # Phase 1 legacy：catalog 拷 PNG）。失败 → _safe_load_texture 返
        # null → GDScript 自动 fallback draw_circle，不阻塞流程。
        try:
            asset_paths = strategy.prepare_assets(recipe, self.catalog, target_dir)
            files_written.extend(asset_paths)
        except Exception as e:
            logger.warning("[Fastpath] prepare_assets failed (non-fatal): %s", e)

        # 3. 覆盖 i18n_strings.gd
        i18n_path = target_dir / "scripts" / "i18n_strings.gd"
        i18n_content = strategy.render_i18n_strings(recipe)
        i18n_path.write_text(i18n_content, encoding="utf-8")
        files_written.append("scripts/i18n_strings.gd")

        # 4. 写 meta.json
        meta_path = target_dir / "meta.json"
        meta_payload = {
            "schema_version": recipe.schema_version,
            "archetype_id": recipe.archetype_id,
            "provenance": "fastpath",
            "created_at": int(time.time()),
            "title": recipe.meta.title_i18n.get("en", ""),
            "title_i18n": recipe.meta.title_i18n,
            "description": recipe.meta.description_i18n.get("en", ""),
            "description_i18n": recipe.meta.description_i18n,
            "icon": recipe.meta.icon,
            # input_mode 必须和 finalize.py 写进 DB（ai_games.input_mode）的值一致。
            # 历史 P0：compiler 漏写这个 key → fast path 游戏 meta.json 无 input_mode，
            # 而 DB 是真实值（archetype 一律 body_pose），导致下载到端上的 meta 与
            # 列表/DB 不一致，体感游戏摄像头开不起来。详见 robot/CLAUDE.md / motem-studio。
            "input_mode": input_mode,
            "recipe": recipe.model_dump(mode="json"),
        }
        meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        files_written.append("meta.json")

        logger.info(
            "[Fastpath] compiled archetype=%s title=%r target=%s",
            recipe.archetype_id, recipe.meta.title_i18n.get("en", ""), target_dir,
        )

        return CompileResult(
            game_dir=target_dir,
            archetype_id=recipe.archetype_id,
            files_written=files_written,
        )


# ─── helpers ─────────────────────────────────────────────────────────────────

def _truncate_at_ai_zone(skeleton: str) -> str:
    """切掉 _template/scenes/main.gd 从 AI ZONE 第一个 hook 注释开始的所有内容。
    若 marker 不在文件里（骨架重构）→ CompileError，避免拼出语法损坏的 main.gd。
    """
    idx = skeleton.find(_AI_ZONE_TRUNCATE_MARKER)
    if idx < 0:
        raise CompileError(
            f"AI ZONE truncate marker not found in skeleton main.gd: "
            f"{_AI_ZONE_TRUNCATE_MARKER!r}"
        )
    return skeleton[:idx]
