"""Game DB service helpers — used by game_tools and the router.

写在 router 之外的目的：agent tools 在非 request-scoped 协程里也要写库，
直接 import router 会把 FastAPI 的 dependency injection 拉进来。

本模块两个职责：
1. 维护「单源 + 视图表」存储层（ai_games + user_game_view + explore_game_view），
   把所有事件性写入封装成幂等 helper，避免 router 和 agent tool 各自 ad-hoc
   写一份导致状态漂移。
2. 提供两个面向 list 端点的 cache-aside helper（get_explore_list /
   get_mine_list），把 redis 读写策略集中在这里。

与 widget service 的关键差异：
- clone_game 多两步 Godot-specific 处理：剔除 `.godot/` 编辑器缓存目录、
  重新打 zip（widget 的 zip 是 backend 后续按需打，game 的 zip 在 create
  时就生成了）
- input_mode 字段在 upsert / clone 全程透传
"""

import asyncio
import hashlib
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.database import async_session
from robot.db.models import (
    AIGame,
    ExploreGameView,
    GameLike,
    UserGameView,
)
from robot.db.redis import redis_client

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """Naive UTC now —— 替代 Py3.12 弃用的 ``datetime.utcnow()``。

    返回值与原 ``datetime.utcnow()`` 行为完全一致（无 tzinfo 的 naive UTC）。
    保留 naive 语义是为了兼容 ``robot/db/models.py`` 里所有 ``DateTime``
    （非 ``DateTime(timezone=True)``）列；带 tz 的 datetime 写入会被 ORM
    自动 strip，但 naive 在所有 driver 行为都一致，避免依赖 driver 实现。
    将来若全表改成 timezone-aware 列，此函数返回值改为不去除 tzinfo 即可。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────────
# Filesystem
# ─────────────────────────────────────────────────────────────────────

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
GAMES_DIR = _BACKEND_ROOT / "games"


def _gen_game_id(title: str = "") -> str:
    """ai_<ts>_<md5[:6]>，与 widget / 老 game_tools._generate_game_id 同源。"""
    ts = int(time.time())
    seed = f"{ts}-{title}-{time.perf_counter()}"
    rand = hashlib.md5(seed.encode()).hexdigest()[:6]
    return f"ai_{ts}_{rand}"


# ─────────────────────────────────────────────────────────────────────
# Cache keys & invalidation
# ─────────────────────────────────────────────────────────────────────

_CACHE_VERSION = "v1"
_CACHE_TTL_LIST = 60       # 探索 / 我的列表
_CACHE_TTL_META = 300      # 单 game 元数据


def _key_explore() -> str:
    return f"games:explore:{_CACHE_VERSION}"


def _key_mine(user_id: str) -> str:
    return f"games:mine:{user_id}:{_CACHE_VERSION}"


def _key_meta(game_id: str) -> str:
    return f"games:meta:{game_id}:{_CACHE_VERSION}"


async def _invalidate_explore() -> None:
    try:
        await redis_client.delete(_key_explore())
    except Exception as e:
        logger.warning("[Games] redis DEL explore failed: %s", e)


async def _invalidate_mine(user_id: str) -> None:
    if not user_id:
        return
    try:
        await redis_client.delete(_key_mine(user_id))
    except Exception as e:
        logger.warning("[Games] redis DEL mine(%s) failed: %s", user_id, e)


async def _invalidate_meta(game_id: str) -> None:
    try:
        await redis_client.delete(_key_meta(game_id))
    except Exception as e:
        logger.warning("[Games] redis DEL meta(%s) failed: %s", game_id, e)


async def invalidate_game_caches(
    game_id: str,
    *,
    affected_user_ids: list[str] | None = None,
    invalidate_explore: bool = True,
) -> None:
    """统一的失效入口 — 当 visibility / counts / favorites 任意变更时调一次。"""
    await _invalidate_meta(game_id)
    if invalidate_explore:
        await _invalidate_explore()
    for uid in affected_user_ids or []:
        await _invalidate_mine(uid)


# ─────────────────────────────────────────────────────────────────────
# Single-row writers
# ─────────────────────────────────────────────────────────────────────


async def upsert_game_row(
    *,
    game_id: str,
    owner_id: str,
    title: str,
    title_i18n: dict,
    description: str = "",
    description_i18n: dict | None = None,
    icon: str = "🎮",
    input_mode: str = "touch",
    template_from: str = "",
    session_id: str | None = None,
    visibility: str = "private",
    kind: str = "ai",
    original_game_id: str | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Insert or update an AIGame row. Idempotent.

    创建路径上同时把 (owner_id, game_id, 'created') 写到 user_game_view —
    这样新创建的 game 立刻在作者的 Mine Tab 出现。

    `db` 可选 — agent tools 在请求外（cron / WS）调用时不传，函数内部新开
    `async_session`；测试 / 端点调用时传入已有 session 共享事务。
    """
    if not owner_id and kind != "builtin":
        logger.debug("[Games] skip upsert (no owner): %s", game_id)
        return

    if description_i18n is None:
        description_i18n = {"en": description or "", "zh-Hans": description or ""}

    if db is not None:
        await _do_upsert(
            db, game_id=game_id, owner_id=owner_id,
            title=title, title_i18n=title_i18n,
            description=description, description_i18n=description_i18n,
            icon=icon, input_mode=input_mode,
            template_from=template_from, session_id=session_id,
            visibility=visibility, kind=kind,
            original_game_id=original_game_id,
        )
        await invalidate_game_caches(
            game_id,
            affected_user_ids=[owner_id] if owner_id else [],
            invalidate_explore=True,
        )
        return

    async with async_session() as session:  # type: AsyncSession
        is_new = await _do_upsert(
            session, game_id=game_id, owner_id=owner_id,
            title=title, title_i18n=title_i18n,
            description=description, description_i18n=description_i18n,
            icon=icon, input_mode=input_mode,
            template_from=template_from, session_id=session_id,
            visibility=visibility, kind=kind,
            original_game_id=original_game_id,
        )
        await session.commit()
        logger.info(
            "[Games] upserted DB row: %s (owner=%s, kind=%s, visibility=%s, input=%s, new=%s)",
            game_id, owner_id, kind, visibility, input_mode, is_new,
        )

    await invalidate_game_caches(
        game_id,
        affected_user_ids=[owner_id] if owner_id else [],
        invalidate_explore=not is_new or visibility != "private",
    )


async def _do_upsert(
    db: AsyncSession,
    *,
    game_id: str,
    owner_id: str,
    title: str,
    title_i18n: dict,
    description: str,
    description_i18n: dict,
    icon: str,
    input_mode: str,
    template_from: str,
    session_id: str | None,
    visibility: str,
    kind: str,
    original_game_id: str | None,
) -> bool:
    """执行 upsert + user_game_view 写入；不 commit 不 invalidate cache。返回 is_new。"""
    existing = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    now = _utcnow()
    is_new = existing is None
    if is_new:
        db.add(AIGame(
            game_id=game_id,
            owner_id=owner_id or None,
            title=title,
            title_i18n=json.dumps(title_i18n, ensure_ascii=False),
            description=description or None,
            description_i18n=json.dumps(description_i18n, ensure_ascii=False),
            icon=icon,
            input_mode=input_mode,
            kind=kind,
            visibility=visibility,
            original_game_id=original_game_id,
            template_from=template_from,
            session_id=session_id,
            like_count=0, favorite_count=0, share_count=0, remix_count=0,
            created_at=now, updated_at=now,
        ))
        # 作者自己的 Mine Tab 索引行（builtin 没有 owner，跳过）
        if owner_id:
            view_existing = (
                await db.execute(
                    select(UserGameView).where(
                        UserGameView.user_id == owner_id,
                        UserGameView.game_id == game_id,
                    )
                )
            ).scalar_one_or_none()
            if view_existing is None:
                db.add(UserGameView(
                    user_id=owner_id,
                    game_id=game_id,
                    view_type="created",
                    added_at=now,
                ))
    else:
        existing.title = title
        existing.title_i18n = json.dumps(title_i18n, ensure_ascii=False)
        existing.description = description or None
        existing.description_i18n = json.dumps(description_i18n, ensure_ascii=False)
        existing.icon = icon
        # input_mode 不在 modify 路径改（设计上由 creator UI 控制）
        if template_from:
            existing.template_from = template_from
        if session_id:
            existing.session_id = session_id
        existing.updated_at = now
    await db.flush()
    return is_new


async def delete_game_rows_for_owner(owner_id: str) -> list[str]:
    """删除某用户拥有的全部游戏行。FK CASCADE 会自动清 user_game_view /
    explore_game_view / game_likes（widget 同款）。"""
    if not owner_id:
        return []
    async with async_session() as db:  # type: AsyncSession
        rows = (
            await db.execute(select(AIGame).where(AIGame.owner_id == owner_id))
        ).scalars().all()
        ids = [r.game_id for r in rows]
        for r in rows:
            await db.delete(r)
        await db.commit()
    for gid in ids:
        await invalidate_game_caches(gid, invalidate_explore=True)
    await _invalidate_mine(owner_id)
    return ids


# ─────────────────────────────────────────────────────────────────────
# View-table maintainers (visibility / favorite / explore index)
# ─────────────────────────────────────────────────────────────────────


async def add_to_explore(db: AsyncSession, game_id: str) -> None:
    """幂等：作者把 visibility 改为 public_* 时调用。"""
    existing = (
        await db.execute(
            select(ExploreGameView).where(ExploreGameView.game_id == game_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(ExploreGameView(game_id=game_id, added_at=_utcnow()))


async def remove_from_explore(db: AsyncSession, game_id: str) -> None:
    """幂等：作者把 visibility 改回 private 时调用。"""
    await db.execute(
        delete(ExploreGameView).where(ExploreGameView.game_id == game_id)
    )


async def update_title(
    db: AsyncSession, game_id: str, title_i18n: dict, primary_title: str
) -> AIGame:
    """改 title — 同时刷新 legacy `title` 字段和 `title_i18n` JSON。

    Args:
        title_i18n: 完整双语 dict（由 title_translator 补齐）
        primary_title: 用户原始输入（直接进 legacy `title` 字段，
            保持「用户输入即所见」原则）
    """
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(game_id)
    row.title = primary_title
    row.title_i18n = json.dumps(title_i18n, ensure_ascii=False)
    row.updated_at = _utcnow()
    return row


async def set_visibility(
    db: AsyncSession, game_id: str, new_visibility: str
) -> AIGame:
    """切换 visibility 并同步 explore_game_view。返回更新后的 AIGame。"""
    valid = {"public_remixable", "public_view_only", "private"}
    if new_visibility not in valid:
        raise ValueError(f"invalid visibility: {new_visibility}")

    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(game_id)

    old = row.visibility
    row.visibility = new_visibility
    row.updated_at = _utcnow()

    if old == "private" and new_visibility != "private":
        await add_to_explore(db, game_id)
    elif old != "private" and new_visibility == "private":
        await remove_from_explore(db, game_id)
    # public_remixable <-> public_view_only：保持在 explore，不动
    return row


async def add_favorite(
    db: AsyncSession, user_id: str, game_id: str
) -> tuple[bool, int]:
    """幂等添加 favorite — 返回 (newly_inserted, new_favorite_count)。

    分流：
    - 当前用户是该游戏 owner → 写 ai_games.owner_favorited（避免与 user_game_view
      `view_type='created'` 行冲突）
    - 非 owner → 在 user_game_view 写新行 view_type='favorited'

    幂等：已是 favorited 状态再调 → 返 (False, 当前 count) 不重复 +1。
    """
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        return False, 0

    is_owner = (row.owner_id == user_id)
    if is_owner:
        if row.owner_favorited:
            return False, row.favorite_count or 0
        row.owner_favorited = True
        row.favorite_count = (row.favorite_count or 0) + 1
        return True, row.favorite_count

    # 非 owner — 走 user_game_view 路径
    existing = (
        await db.execute(
            select(UserGameView).where(
                UserGameView.user_id == user_id,
                UserGameView.game_id == game_id,
                UserGameView.view_type == "favorited",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False, row.favorite_count or 0

    db.add(UserGameView(
        user_id=user_id, game_id=game_id,
        view_type="favorited", added_at=_utcnow(),
    ))
    row.favorite_count = (row.favorite_count or 0) + 1
    return True, row.favorite_count


async def remove_favorite(
    db: AsyncSession, user_id: str, game_id: str
) -> tuple[bool, int]:
    """幂等取消 favorite — 返回 (newly_deleted, new_favorite_count)。

    与 add_favorite 同款分流：owner 走 ai_games.owner_favorited，非 owner
    走 user_game_view view_type='favorited' 行删除。
    """
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        return False, 0

    is_owner = (row.owner_id == user_id)
    if is_owner:
        if not row.owner_favorited:
            return False, row.favorite_count or 0
        row.owner_favorited = False
        row.favorite_count = max(0, (row.favorite_count or 0) - 1)
        return True, row.favorite_count

    existing = (
        await db.execute(
            select(UserGameView).where(
                UserGameView.user_id == user_id,
                UserGameView.game_id == game_id,
                UserGameView.view_type == "favorited",
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return False, row.favorite_count or 0

    await db.delete(existing)
    row.favorite_count = max(0, (row.favorite_count or 0) - 1)
    return True, row.favorite_count


async def toggle_like(
    db: AsyncSession, user_id: str, game_id: str
) -> tuple[bool, int]:
    """toggle game_likes 表 + ai_games.like_count。

    双写期：同时更新 game_stats.like_count（兼容老 router 路径未迁移完）。
    返回 (now_liked, new_like_count)。
    """
    from robot.db.models import GameStats  # late import 避免循环

    existing = (
        await db.execute(
            select(GameLike).where(
                GameLike.user_id == user_id,
                GameLike.game_id == game_id,
            )
        )
    ).scalar_one_or_none()
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one()

    if existing is None:
        db.add(GameLike(user_id=user_id, game_id=game_id))
        row.like_count = (row.like_count or 0) + 1
        liked = True
    else:
        await db.delete(existing)
        row.like_count = max(0, (row.like_count or 0) - 1)
        liked = False

    # 双写 game_stats（保留兼容期）
    stats_row = (
        await db.execute(select(GameStats).where(GameStats.game_id == game_id))
    ).scalar_one_or_none()
    if stats_row is None:
        stats_row = GameStats(
            game_id=game_id, share_count=0, like_count=row.like_count
        )
        db.add(stats_row)
    else:
        stats_row.like_count = row.like_count
        stats_row.updated_at = _utcnow()

    return liked, row.like_count


async def record_share(db: AsyncSession, game_id: str) -> int:
    """+1 share_count（双写 ai_games + game_stats）。"""
    from robot.db.models import GameStats

    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(game_id)
    row.share_count = (row.share_count or 0) + 1

    stats_row = (
        await db.execute(select(GameStats).where(GameStats.game_id == game_id))
    ).scalar_one_or_none()
    if stats_row is None:
        stats_row = GameStats(
            game_id=game_id, share_count=row.share_count, like_count=row.like_count
        )
        db.add(stats_row)
    else:
        stats_row.share_count = row.share_count
        stats_row.updated_at = _utcnow()

    return row.share_count


# ─────────────────────────────────────────────────────────────────────
# Remix
# ─────────────────────────────────────────────────────────────────────


class RemixError(Exception):
    """业务约束失败 — code 用 i18n key，detail 给 caller 直接抛 HTTP 400。"""

    def __init__(self, code: str, *, http_status: int = 400):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


async def clone_game(
    db: AsyncSession,
    *,
    source_game_id: str,
    new_owner_id: str,
) -> AIGame:
    """复制 Godot 工程目录 + 元数据 + DB 行；原作 remix_count++。

    校验链：
    - source 存在
    - source.kind != 'builtin'（内置无目录可拷）
    - source.visibility == 'public_remixable'
    - source.owner_id != new_owner_id（作者修改自己应该走 modify_game 工具）

    Godot 工程隔离：
    - 拷贝时**剔除 `.godot/` 目录**（编辑器缓存，会污染副本）
    - meta.json 改写 game_id / owner_id / template_from / original_game_id /
      created_at / modified_at；history 重置为单条 "Remixed from {source_id}"
    - 重新打 zip（与 game_tools 创建路径同款 shutil.make_archive）
    - input_mode 透传源
    """
    source = (
        await db.execute(select(AIGame).where(AIGame.game_id == source_game_id))
    ).scalar_one_or_none()
    if source is None:
        logger.warning(
            "[RemixDiag] clone rejected: source missing source_game_id=%s requester=%s",
            source_game_id, new_owner_id,
        )
        raise RemixError("GAME_NOT_FOUND", http_status=404)
    logger.info(
        "[RemixDiag] clone source source_game_id=%s source_owner=%s requester=%s "
        "kind=%s visibility=%s remix_count=%s original_game_id=%s",
        source_game_id, source.owner_id, new_owner_id, source.kind,
        source.visibility, source.remix_count, source.original_game_id,
    )
    if source.kind == "builtin":
        logger.warning(
            "[RemixDiag] clone rejected: builtin source_game_id=%s requester=%s",
            source_game_id, new_owner_id,
        )
        raise RemixError("GAME_BUILTIN_NOT_REMIXABLE")
    if source.visibility != "public_remixable":
        logger.warning(
            "[RemixDiag] clone rejected: not remixable source_game_id=%s requester=%s visibility=%s",
            source_game_id, new_owner_id, source.visibility,
        )
        raise RemixError("GAME_NOT_REMIXABLE")
    if source.owner_id == new_owner_id:
        logger.warning(
            "[RemixDiag] clone rejected: own source_game_id=%s requester=%s",
            source_game_id, new_owner_id,
        )
        raise RemixError("GAME_CANNOT_REMIX_OWN")

    # 幂等：同一用户对同一原作再次 remix 时，返回已存在的副本而不是再 clone
    # 一份。否则用户多次 tap 游戏内 ✏️ 编辑（每次都把 alice 的"打飞机游戏"
    # fork 一遍）会让 ai_games 表 + Mine 列表无限堆积同一原作的多份副本。
    # 一份原作 → 一份副本就够了，再次 remix 应该等价于"打开已有的副本继续改"。
    existing = (
        await db.execute(
            select(AIGame).where(
                AIGame.original_game_id == source_game_id,
                AIGame.owner_id == new_owner_id,
            ).order_by(AIGame.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "[RemixDiag] clone idempotent existing_remix=%s requester=%s source_game_id=%s "
            "visibility=%s owner_id=%s",
            existing.game_id, new_owner_id, source_game_id,
            existing.visibility, existing.owner_id,
        )
        return existing

    src_dir = GAMES_DIR / source_game_id
    if not src_dir.is_dir():
        logger.error(
            "[RemixDiag] clone rejected: source dir missing source_game_id=%s requester=%s path=%s",
            source_game_id, new_owner_id, src_dir,
        )
        raise RemixError("GAME_SOURCE_MISSING_DIR", http_status=500)

    new_id = _gen_game_id(source.title)
    dst_dir = GAMES_DIR / new_id
    if dst_dir.exists():
        new_id = _gen_game_id(source.title) + "_dup"
        dst_dir = GAMES_DIR / new_id

    # copytree + 剔除 .godot/ 编辑器缓存。大目录拷贝是阻塞 IO，
    # 走线程池避免阻塞 event loop。
    def _ignore_godot_cache(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n == ".godot" or n.endswith(".import")]

    await asyncio.to_thread(
        shutil.copytree, src_dir, dst_dir, ignore=_ignore_godot_cache,
    )

    # 改写 meta.json
    meta_path = dst_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    meta["game_id"] = new_id
    meta["owner_id"] = new_owner_id
    meta["template_from"] = source_game_id
    meta["original_game_id"] = source_game_id
    meta["created_at"] = int(time.time())
    meta["modified_at"] = int(time.time())

    # 继承原作的 Claude Code 会话历史。会话存在 game_dir **sibling** 目录
    # `<games_root>/<game_id>.claude/`（**不能放进 game_dir** 因为 game_dir
    # 会被打包成 zip 发到 iOS，会话 jsonl 含原作者 prompts / tool calls /
    # thinking blocks，下发等于隐私泄漏 + 几百 KB 流量浪费）。
    #
    # 因为 sibling 不在 src_dir 内，前面的 shutil.copytree 不会带过来 —— 用
    # `inherit_session_from` helper 显式 copy + 改名内部 slug，让 SDK 在新
    # cwd 下能命中 resume。失败一律降级到「副本另起对话」，不让 remix 因为
    # 会话搬运而失败。
    from robot.games._session_paths import inherit_session_from
    inherited_sid: str | None = None
    src_sid = (source.session_id or "").strip()
    if src_sid:
        inherited_sid = await asyncio.to_thread(
            inherit_session_from, src_dir, dst_dir, src_sid,
        )
        if inherited_sid:
            logger.info(
                "[Games] clone_game inherited session %s… from %s for %s",
                src_sid[:12], source_game_id, new_id,
            )
        else:
            # 源是 legacy 游戏（session 在全局 ~/.claude/projects/，而非 sibling）
            # 或 jsonl 已丢。不做迁移：第一次 modify 会自然走 retry-without-resume
            # 从此 dst 走全新会话。
            logger.info(
                "[Games] clone_game source %s has session_id but inheritance "
                "failed (no sibling / jsonl missing); will start fresh for %s",
                source_game_id, new_id,
            )

    meta["session_id"] = inherited_sid
    meta["history"] = [{"role": "system", "content": f"Remixed from {source_game_id}"}]
    meta["visibility"] = "private"
    # input_mode 必须保留（透传源）
    if "input_mode" not in meta:
        meta["input_mode"] = source.input_mode
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 重新打 zip（与 create_game 行为一致），走线程池
    zip_path = GAMES_DIR / f"{new_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    await asyncio.to_thread(
        shutil.make_archive,
        str(zip_path.with_suffix("")), "zip", str(dst_dir),
    )

    # 写新行
    now = _utcnow()
    new_row = AIGame(
        game_id=new_id,
        owner_id=new_owner_id,
        title=source.title,
        title_i18n=source.title_i18n,
        description=source.description,
        description_i18n=source.description_i18n,
        icon=source.icon,
        input_mode=source.input_mode,  # ★ 透传 input_mode
        kind="ai",
        visibility="private",
        original_game_id=source_game_id,
        template_from=source.template_from or "",
        session_id=inherited_sid,
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    )
    db.add(new_row)
    db.add(UserGameView(
        user_id=new_owner_id, game_id=new_id,
        view_type="created", added_at=now,
    ))

    # 原作 remix_count++（ORM mutation 同步 identity map）
    source.remix_count = (source.remix_count or 0) + 1

    logger.info(
        "[RemixDiag] clone created source_game_id=%s requester=%s new_game_id=%s "
        "new_owner=%s visibility=%s original_game_id=%s source_remix_count=%s",
        source_game_id, new_owner_id, new_id, new_row.owner_id, new_row.visibility,
        new_row.original_game_id, source.remix_count,
    )

    return new_row


# ─────────────────────────────────────────────────────────────────────
# List endpoints — cache-aside
# ─────────────────────────────────────────────────────────────────────


def _read_cover_set_at(game_id: str) -> int | None:
    """cover.png 的 mtime（unix sec）。文件 IO，每次 _row_to_dict 调一次，
    但结果会随 _row_to_dict 的输出一起 cache 进 redis 60s，cache hit 后 zero IO。"""
    p = GAMES_DIR / game_id / "cover.png"
    if not p.exists():
        return None
    try:
        return int(p.stat().st_mtime)
    except Exception:
        return None


def _read_content_modified_at(game_id: str) -> int | None:
    """meta.json.modified_at（unix sec），读不到 fallback zip mtime。
    文件 IO，同上 cache 一起。"""
    meta_path = GAMES_DIR / game_id / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            v = meta.get("modified_at")
            if isinstance(v, int):
                return v
            if isinstance(v, (str, float)):
                try:
                    return int(float(v))
                except Exception:
                    pass
        except Exception:
            pass
    zip_path = GAMES_DIR / f"{game_id}.zip"
    if zip_path.exists():
        try:
            return int(zip_path.stat().st_mtime)
        except Exception:
            pass
    return None


def _row_to_dict(row: AIGame) -> dict:
    """AIGame ORM row → JSON-serializable dict（i18n 字段保留为字符串供端点解析）。

    把 cover_set_at / content_modified_at 的 file IO 在这里做一次 — 结果
    随 dict 一起进 redis explore/mine cache（60s TTL），cache hit 路径就不
    再读文件了。从「每个 list request N 次 file IO」降到「cache miss N 次 +
    cache hit 0 次」。
    """
    return {
        "game_id": row.game_id,
        "owner_id": row.owner_id,
        "title": row.title,
        "title_i18n_raw": row.title_i18n,
        "description": row.description,
        "description_i18n_raw": row.description_i18n,
        "icon": row.icon,
        "input_mode": row.input_mode,
        "kind": row.kind,
        "visibility": row.visibility,
        "original_game_id": row.original_game_id,
        "template_from": row.template_from,
        "session_id": row.session_id,
        "like_count": row.like_count,
        "favorite_count": row.favorite_count,
        "share_count": row.share_count,
        "remix_count": row.remix_count,
        "owner_favorited": bool(row.owner_favorited),
        # cover / content 时间戳 — 文件 IO 提到这里，让后续 cache 命中零 IO
        "_cover_set_at": _read_cover_set_at(row.game_id),
        "_content_modified_at": _read_content_modified_at(row.game_id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def get_explore_list(
    db: AsyncSession, *, limit: int = 200
) -> list[dict]:
    """读 explore_game_view JOIN ai_games。Redis cache 60s。"""
    key = _key_explore()
    try:
        cached = await redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("[Games] redis GET explore failed: %s", e)

    rows = (
        await db.execute(
            select(AIGame)
            .join(ExploreGameView, ExploreGameView.game_id == AIGame.game_id)
            .order_by(
                ExploreGameView.sort_score.desc(),
                ExploreGameView.added_at.desc(),
                # game_id 作为最终稳定 tiebreaker —— 防 sort_score / added_at
                # 平局时跨页排序乱跳。前端 offset 分页依赖此稳定性。
                AIGame.game_id.asc(),
            )
            .limit(limit)
        )
    ).scalars().all()
    result = [_row_to_dict(r) for r in rows]

    try:
        await redis_client.setex(key, _CACHE_TTL_LIST, json.dumps(result))
    except Exception as e:
        logger.warning("[Games] redis SETEX explore failed: %s", e)
    return result


async def get_mine_list(
    db: AsyncSession, *, user_id: str
) -> list[dict]:
    """读 user_game_view JOIN ai_games，限定当前用户。Redis cache 60s。"""
    if not user_id:
        return []
    key = _key_mine(user_id)
    try:
        cached = await redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("[Games] redis GET mine failed: %s", e)

    rows_with_view = (
        await db.execute(
            select(AIGame, UserGameView.view_type, UserGameView.added_at)
            .join(UserGameView, UserGameView.game_id == AIGame.game_id)
            .where(UserGameView.user_id == user_id)
            .order_by(
                UserGameView.added_at.desc(),
                # 二级稳定排序 —— 前端 offset 分页依赖（见 router.list_explore 同款注释）
                AIGame.game_id.asc(),
            )
        )
    ).all()
    result = []
    for row, view_type, added_at in rows_with_view:
        d = _row_to_dict(row)
        d["view_type"] = view_type
        d["mine_added_at"] = added_at.isoformat() if added_at else None
        result.append(d)

    try:
        await redis_client.setex(key, _CACHE_TTL_LIST, json.dumps(result))
    except Exception as e:
        logger.warning("[Games] redis SETEX mine failed: %s", e)
    return result
