"""Fast Path 命中后的"完赛"步骤——把 Compiler 输出的 game_dir 走通
audio harvest / godot import / zip / DB / WebSocket push，等同于 SDK
路径在 `create_game` 工具尾部做的事。

设计原则：
- 复用 `game_tools` 里的现成 helper，不重写
- 出错时 raise FinalizeError，由 streaming.py 决定 fallback 还是直接报错
- 不在本模块发 `game_creation_failed` 事件——那是 caller 的职责
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from robot.games.fastpath.schemas import GameRecipe

logger = logging.getLogger(__name__)


class FinalizeError(Exception):
    """Finalize 流程失败。caller 应 fallback 到 slow path 或报错给用户。"""


async def finalize_fastpath_game(
    *,
    recipe: GameRecipe,
    game_dir: Path,
    owner_id: str,
    user_id: str,
    input_mode: str,
    description: str,
) -> str:
    """完成 fast path 命中后的全部后续步骤。

    输入：Compiler 已经写好 game_dir（含 main.gd / i18n_strings.gd / meta.json /
    audio / fonts / project.godot / assets/background.png）。
    输出：成功时返回 game_id（=game_dir.name），失败 raise FinalizeError。

    步骤跟 game_tools.create_game 尾部对齐：
      1. _harvest_audio    把 manifest 引用的音频拷进 game_dir/audio/
      2. _import_assets    跑 godot --headless --import 生成 .ctex / .import
      3. _generate_script_cache 预编译 GDScript 缓存
      4. zip → games/{game_id}.zip
      5. upsert_game_row   DB
      6. ws_manager.send_to_user game_ready 推送到 iOS
    """
    # 延迟 import 避免循环依赖（game_tools 也要用 fastpath...?  不会，但保
    # 持懒加载也方便测试 mock）
    from robot.agent.tools import _context as ctx
    from robot.agent.tools.game_tools import (
        GAMES_DIR,
        _generate_script_cache,
        _harvest_audio,
        _import_assets,
    )

    game_id = game_dir.name
    title_i18n = recipe.meta.title_i18n
    description_i18n = recipe.meta.description_i18n
    title = title_i18n.get("zh-Hans") if ctx.get_user_lang().startswith("zh") else title_i18n["en"]
    icon = recipe.meta.icon

    t0 = time.time()

    # 1. audio harvest（复用 SDK 路径同款；fast path 生成的 main.gd 里
    # `AudioManager.play("pickup")` 等 ID 串会被识别）
    try:
        _harvest_audio(game_dir)
    except Exception as e:
        raise FinalizeError(f"audio harvest failed: {e}") from e

    # 2. godot --headless --import；失败也算 fatal——iOS SwiftGodotKit 加载
    # res:// 资源需要 .import 元数据
    try:
        _import_assets(game_dir)
    except Exception as e:
        raise FinalizeError(f"godot import failed: {e}") from e

    # 3. script cache（best-effort，失败不阻塞）
    try:
        _generate_script_cache(game_dir)
    except Exception as e:
        logger.warning("[Fastpath/Finalize] _generate_script_cache failed (non-fatal): %s", e)

    # 4. zip
    try:
        zip_path = GAMES_DIR / f"{game_id}.zip"
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(game_dir))
        zip_kb = zip_path.stat().st_size // 1024
        logger.info("[Fastpath/Finalize] zip %s.zip (%dKB)", game_id, zip_kb)
    except Exception as e:
        raise FinalizeError(f"zip failed: {e}") from e

    # 5. DB upsert — best-effort，DB 不可用时 meta.json 还在盘上，下次
    # alembic 迁移会回灌
    try:
        from robot.games.service import upsert_game_row
        await upsert_game_row(
            game_id=game_id,
            owner_id=str(owner_id or ""),
            title=title,
            title_i18n=dict(title_i18n),
            description=description,
            description_i18n=dict(description_i18n),
            icon=icon,
            input_mode=input_mode,
            template_from="",  # fastpath 生成不来自模板 clone
            session_id="",
            visibility="private",
            kind="ai",
        )
    except Exception as e:
        logger.warning("[Fastpath/Finalize] upsert_game_row failed (non-fatal): %s", e)

    # 6. WebSocket game_ready
    if ctx._ws_manager:
        try:
            await ctx._ws_manager.send_to_user(
                owner_id,
                user_id,
                {
                    "type": "robot_command",
                    "data": {
                        "action": "game_ready",
                        "game_id": game_id,
                        "title": title,
                        "title_i18n": dict(title_i18n),
                        "description_i18n": dict(description_i18n),
                        "icon": icon,
                        "input_mode": input_mode,
                    },
                },
            )
            logger.info(
                "[Fastpath/Finalize] game_ready sent to iOS | game_id=%s wall=%.1fs",
                game_id, time.time() - t0,
            )
        except Exception as e:
            # game_ready 推送失败不算 fatal——下次 iOS 拉列表会看到
            logger.warning("[Fastpath/Finalize] game_ready push failed (non-fatal): %s", e)
    else:
        logger.warning("[Fastpath/Finalize] ws_manager not available, game_ready NOT sent")

    # 7. Web (HTML5) export + publish — async, fire-and-forget, gated by
    # settings.web_export_enabled. Refreshes the public /play/{game_id}/ link.
    try:
        import asyncio

        from robot.games.web_export import export_and_publish_async

        asyncio.create_task(export_and_publish_async(game_id))
    except Exception as e:  # noqa: BLE001 - scheduling must never break finalize
        logger.warning("[Fastpath/Finalize] web export schedule failed (non-fatal): %s", e)

    return game_id
