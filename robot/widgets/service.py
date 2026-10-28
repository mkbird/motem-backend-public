"""Widget DB service helpers — used by widget_tools and the router.

写在 router 之外的目的：agent tools 在非 request-scoped 的协程里也要写库，
直接 import router 会把 FastAPI 的 dependency injection 拉进来。

本模块两个职责：
1. 维护「单源 + 两张视图」存储层（ai_widgets + user_widget_view +
   explore_widget_view），把所有事件性写入封装成幂等 helper，避免
   router 和 agent tool 各自 ad-hoc 写一份导致状态漂移。
2. 提供两个面向 list 端点的 cache-aside helper（get_explore_list /
   get_mine_list），把 redis 读写策略集中在这里。
"""

import hashlib
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path


def _utcnow() -> datetime:
    """Naive UTC datetime — replacement for deprecated _utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from robot.db.database import async_session
from robot.db.models import (
    AIWidget,
    ExploreWidgetView,
    UserWidgetView,
    WidgetFavorite,
    WidgetLike,
)
from robot.db.redis import redis_client

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Filesystem
# ─────────────────────────────────────────────────────────────────────

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
WIDGETS_DIR = _BACKEND_ROOT / "widgets"


def _gen_widget_id() -> str:
    """与 widget_tools._gen_widget_id 同源，避免循环 import。"""
    ts = int(time.time())
    rand = hashlib.md5(f"{ts}-{time.perf_counter()}".encode()).hexdigest()[:6]
    return f"aw_{ts}_{rand}"


# ─────────────────────────────────────────────────────────────────────
# Cache keys & invalidation
# ─────────────────────────────────────────────────────────────────────

_CACHE_VERSION = "v1"
_CACHE_TTL_LIST = 60       # 探索 / 我的列表
_CACHE_TTL_META = 300      # 单 widget 元数据


def _key_explore() -> str:
    return f"widgets:explore:{_CACHE_VERSION}"


def _key_mine(user_id: str) -> str:
    return f"widgets:mine:{user_id}:{_CACHE_VERSION}"


def _key_meta(widget_id: str) -> str:
    return f"widgets:meta:{widget_id}:{_CACHE_VERSION}"


async def _invalidate_explore() -> None:
    try:
        await redis_client.delete(_key_explore())
    except Exception as e:
        logger.warning("[Widgets] redis DEL explore failed: %s", e)


async def _invalidate_mine(user_id: str) -> None:
    if not user_id:
        return
    try:
        await redis_client.delete(_key_mine(user_id))
    except Exception as e:
        logger.warning("[Widgets] redis DEL mine(%s) failed: %s", user_id, e)


async def _invalidate_meta(widget_id: str) -> None:
    try:
        await redis_client.delete(_key_meta(widget_id))
    except Exception as e:
        logger.warning("[Widgets] redis DEL meta(%s) failed: %s", widget_id, e)


async def invalidate_widget_caches(
    widget_id: str,
    *,
    affected_user_ids: list[str] | None = None,
    invalidate_explore: bool = True,
) -> None:
    """统一的失效入口 — 当 visibility / counts / favorites 任意变更时调一次。"""
    await _invalidate_meta(widget_id)
    if invalidate_explore:
        await _invalidate_explore()
    for uid in affected_user_ids or []:
        await _invalidate_mine(uid)


# ─────────────────────────────────────────────────────────────────────
# Single-row writers
# ─────────────────────────────────────────────────────────────────────


async def upsert_widget_row(
    *,
    widget_id: str,
    owner_id: str,
    title_i18n: dict,
    description_i18n: dict,
    supported_sizes: list[str],
    base_template_id: str = "",
    visibility: str = "private",
    kind: str = "ai",
    original_widget_id: str | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Insert or update an AIWidget row. Idempotent.

    创建路径上同时把 (owner_id, widget_id, 'created') 写到 user_widget_view —
    这样新创建的 widget 立刻在作者的 Mine Tab 出现。

    `db` 可选 — agent tools 在请求外（cron / WS）调用时不传，函数内部
    新开 `async_session`；测试 / 端点调用时传入已有 session 共享事务。
    """
    if not owner_id and kind != "builtin":
        logger.debug("[Widgets] skip upsert (no owner): %s", widget_id)
        return

    if db is not None:
        await _do_upsert(
            db, widget_id=widget_id, owner_id=owner_id,
            title_i18n=title_i18n, description_i18n=description_i18n,
            supported_sizes=supported_sizes, base_template_id=base_template_id,
            visibility=visibility, kind=kind,
            original_widget_id=original_widget_id,
        )
        await invalidate_widget_caches(
            widget_id,
            affected_user_ids=[owner_id] if owner_id else [],
            invalidate_explore=True,
        )
        return

    async with async_session() as session:  # type: AsyncSession
        is_new = await _do_upsert(
            session, widget_id=widget_id, owner_id=owner_id,
            title_i18n=title_i18n, description_i18n=description_i18n,
            supported_sizes=supported_sizes, base_template_id=base_template_id,
            visibility=visibility, kind=kind,
            original_widget_id=original_widget_id,
        )
        await session.commit()
        logger.info(
            "[Widgets] upserted DB row: %s (owner=%s, kind=%s, visibility=%s, new=%s)",
            widget_id, owner_id, kind, visibility, is_new,
        )

    # 缓存失效
    await invalidate_widget_caches(
        widget_id,
        affected_user_ids=[owner_id] if owner_id else [],
        invalidate_explore=not is_new or visibility != "private",
    )


async def _do_upsert(
    db: AsyncSession,
    *,
    widget_id: str,
    owner_id: str,
    title_i18n: dict,
    description_i18n: dict,
    supported_sizes: list[str],
    base_template_id: str,
    visibility: str,
    kind: str,
    original_widget_id: str | None,
) -> bool:
    """执行 upsert + user_widget_view 写入；不 commit 不 invalidate cache。返回 is_new。"""
    existing = (
        await db.execute(select(AIWidget).where(AIWidget.widget_id == widget_id))
    ).scalar_one_or_none()
    now = _utcnow()
    is_new = existing is None
    if is_new:
        db.add(AIWidget(
            widget_id=widget_id,
            owner_id=owner_id or None,
            title_i18n=json.dumps(title_i18n, ensure_ascii=False),
            description_i18n=json.dumps(description_i18n, ensure_ascii=False),
            supported_sizes=json.dumps(supported_sizes),
            base_template_id=base_template_id,
            visibility=visibility,
            kind=kind,
            original_widget_id=original_widget_id,
            like_count=0,
            favorite_count=0,
            remix_count=0,
            created_at=now,
            updated_at=now,
        ))
        # 作者自己的 Mine Tab 索引行（builtin 没有 owner，跳过）
        if owner_id:
            view_existing = (
                await db.execute(
                    select(UserWidgetView).where(
                        UserWidgetView.user_id == owner_id,
                        UserWidgetView.widget_id == widget_id,
                    )
                )
            ).scalar_one_or_none()
            if view_existing is None:
                db.add(UserWidgetView(
                    user_id=owner_id,
                    widget_id=widget_id,
                    view_type="created",
                    added_at=now,
                ))
    else:
        existing.title_i18n = json.dumps(title_i18n, ensure_ascii=False)
        existing.description_i18n = json.dumps(description_i18n, ensure_ascii=False)
        existing.supported_sizes = json.dumps(supported_sizes)
        if base_template_id:
            existing.base_template_id = base_template_id
        existing.updated_at = now
    await db.flush()
    return is_new


async def delete_widget_rows_for_owner(
    owner_id: str, *, db: AsyncSession | None = None,
) -> list[str]:
    """Return widget_ids owned by the user, deleting rows. Used by account deletion.

    显式清理子表（widget_likes / user_widget_view / explore_widget_view）—
    生产 MySQL 的 FK CASCADE 会兜底，但显式 delete 让 SQLite 测试也能验
    完整行为。`db` 可选：测试传 fixture session，生产/cron 不传 → 自开 session。
    """
    if not owner_id:
        return []

    if db is not None:
        ids = await _do_delete_for_owner(db, owner_id)
        for wid in ids:
            await invalidate_widget_caches(wid, invalidate_explore=True)
        await _invalidate_mine(owner_id)
        return ids

    async with async_session() as session:  # type: AsyncSession
        ids = await _do_delete_for_owner(session, owner_id)
        await session.commit()
    for wid in ids:
        await invalidate_widget_caches(wid, invalidate_explore=True)
    await _invalidate_mine(owner_id)
    return ids


async def _do_delete_for_owner(db: AsyncSession, owner_id: str) -> list[str]:
    """共享实现：删除 owner 的所有 widget + 子表关联。不 commit，由 caller 决定。"""
    from sqlalchemy import delete as sa_delete

    rows = (
        await db.execute(select(AIWidget).where(AIWidget.owner_id == owner_id))
    ).scalars().all()
    ids = [r.widget_id for r in rows]
    for wid in ids:
        await db.execute(
            sa_delete(ExploreWidgetView).where(ExploreWidgetView.widget_id == wid)
        )
        await db.execute(
            sa_delete(UserWidgetView).where(UserWidgetView.widget_id == wid)
        )
        await db.execute(
            sa_delete(WidgetLike).where(WidgetLike.widget_id == wid)
        )
        await db.execute(
            sa_delete(WidgetFavorite).where(WidgetFavorite.widget_id == wid)
        )
    for r in rows:
        await db.delete(r)
    await db.flush()
    return ids


# ─────────────────────────────────────────────────────────────────────
# View-table maintainers (visibility / favorite / explore index)
# ─────────────────────────────────────────────────────────────────────


async def add_to_explore(db: AsyncSession, widget_id: str) -> None:
    """幂等：作者把 visibility 改为 public_* 时调用。"""
    existing = (
        await db.execute(
            select(ExploreWidgetView).where(
                ExploreWidgetView.widget_id == widget_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(ExploreWidgetView(widget_id=widget_id, added_at=_utcnow()))


async def remove_from_explore(db: AsyncSession, widget_id: str) -> None:
    """幂等：作者把 visibility 改回 private 时调用。"""
    await db.execute(
        delete(ExploreWidgetView).where(ExploreWidgetView.widget_id == widget_id)
    )


async def update_title(
    db: AsyncSession, widget_id: str, title_i18n: dict
) -> AIWidget:
    """改 widget title — 写入 title_i18n JSON。"""
    row = (
        await db.execute(select(AIWidget).where(AIWidget.widget_id == widget_id))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(widget_id)
    row.title_i18n = json.dumps(title_i18n, ensure_ascii=False)
    row.updated_at = _utcnow()
    return row


async def set_visibility(
    db: AsyncSession, widget_id: str, new_visibility: str
) -> AIWidget:
    """切换 visibility 并同步 explore_widget_view。返回更新后的 AIWidget。"""
    valid = {"public_remixable", "public_view_only", "private"}
    if new_visibility not in valid:
        raise ValueError(f"invalid visibility: {new_visibility}")

    row = (
        await db.execute(select(AIWidget).where(AIWidget.widget_id == widget_id))
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(widget_id)

    old = row.visibility
    row.visibility = new_visibility
    row.updated_at = _utcnow()

    # 同步 explore index
    if old == "private" and new_visibility != "private":
        await add_to_explore(db, widget_id)
    elif old != "private" and new_visibility == "private":
        await remove_from_explore(db, widget_id)
    # public_remixable <-> public_view_only：保持在 explore，不动
    return row


async def add_favorite(
    db: AsyncSession, user_id: str, widget_id: str
) -> tuple[bool, int]:
    """幂等添加 favorite — 返回 (newly_inserted, new_favorite_count)。

    收藏关系存于 widget_favorites（与 user_widget_view 解耦），所以作者
    可以收藏自己创建的组件。"""
    existing = (
        await db.execute(
            select(WidgetFavorite).where(
                WidgetFavorite.user_id == user_id,
                WidgetFavorite.widget_id == widget_id,
            )
        )
    ).scalar_one_or_none()
    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False, row.favorite_count if row else 0

    db.add(WidgetFavorite(
        user_id=user_id, widget_id=widget_id,
        created_at=_utcnow(),
    ))
    if row is not None:
        row.favorite_count = (row.favorite_count or 0) + 1
        return True, row.favorite_count
    return True, 0


async def remove_favorite(
    db: AsyncSession, user_id: str, widget_id: str
) -> tuple[bool, int]:
    """幂等取消 favorite — 返回 (newly_deleted, new_favorite_count)。"""
    existing = (
        await db.execute(
            select(WidgetFavorite).where(
                WidgetFavorite.user_id == user_id,
                WidgetFavorite.widget_id == widget_id,
            )
        )
    ).scalar_one_or_none()
    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        return False, row.favorite_count if row else 0

    await db.delete(existing)
    if row is not None:
        row.favorite_count = max(0, (row.favorite_count or 0) - 1)
        return True, row.favorite_count
    return True, 0


async def toggle_like(
    db: AsyncSession, user_id: str, widget_id: str
) -> tuple[bool, int]:
    """toggle widget_likes，返回 (now_liked, new_like_count)。"""
    existing = (
        await db.execute(
            select(WidgetLike).where(
                WidgetLike.user_id == user_id,
                WidgetLike.widget_id == widget_id,
            )
        )
    ).scalar_one_or_none()
    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(widget_id)

    if existing is None:
        db.add(WidgetLike(user_id=user_id, widget_id=widget_id))
        row.like_count = (row.like_count or 0) + 1
        liked = True
    else:
        await db.delete(existing)
        row.like_count = max(0, (row.like_count or 0) - 1)
        liked = False

    return liked, row.like_count


# ─────────────────────────────────────────────────────────────────────
# Remix
# ─────────────────────────────────────────────────────────────────────


class RemixError(Exception):
    """业务约束失败 — code 用 i18n key，detail 给 caller 直接抛 HTTP 400。"""

    def __init__(self, code: str, *, http_status: int = 400):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


async def clone_widget(
    db: AsyncSession,
    *,
    source_widget_id: str,
    new_owner_id: str,
) -> AIWidget:
    """复制 widget 文件 + 元数据 + DB 行；原作 remix_count++。

    校验链：
    - source 存在
    - source.kind != 'builtin'（内置无目录可拷）
    - source.visibility == 'public_remixable'
    - source.owner_id != new_owner_id（作者修改自己应该走 modify_widget 工具）
    """
    source = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == source_widget_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise RemixError("WIDGET_NOT_FOUND", http_status=404)
    if source.kind == "builtin":
        raise RemixError("WIDGET_BUILTIN_NOT_REMIXABLE")
    if source.visibility != "public_remixable":
        raise RemixError("WIDGET_NOT_REMIXABLE")
    if source.owner_id == new_owner_id:
        raise RemixError("WIDGET_CANNOT_REMIX_OWN")

    # 文件层拷贝
    src_dir = WIDGETS_DIR / source_widget_id
    if not src_dir.is_dir():
        raise RemixError("WIDGET_SOURCE_MISSING_DIR", http_status=500)

    new_id = _gen_widget_id()
    dst_dir = WIDGETS_DIR / new_id
    if dst_dir.exists():
        # md5 collision 极小，偏执处理一次
        new_id = _gen_widget_id() + "_dup"
        dst_dir = WIDGETS_DIR / new_id

    try:
        shutil.copytree(src_dir, dst_dir)
    except Exception as e:
        raise RemixError("WIDGET_SOURCE_COPY_FAILED", http_status=500) from e

    try:
        # 改写 meta.json
        meta_path = dst_dir / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        meta["widget_id"] = new_id
        meta["owner_id"] = new_owner_id
        meta["template_from"] = source_widget_id
        meta["original_widget_id"] = source_widget_id
        meta["created_at"] = int(time.time())
        meta["modified_at"] = int(time.time())
        # remix 副本默认私有 — 用户后续可以自己改可见性
        meta["visibility"] = "private"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 确保新副本没有遗留 zip，让首次 download 重新打包
        (WIDGETS_DIR / f"{new_id}.zip").unlink(missing_ok=True)

        # 写新行 — 复制 source 的 i18n / sizes
        now = _utcnow()
        new_row = AIWidget(
            widget_id=new_id,
            owner_id=new_owner_id,
            title_i18n=source.title_i18n,
            description_i18n=source.description_i18n,
            supported_sizes=source.supported_sizes,
            base_template_id=source.base_template_id,
            visibility="private",
            kind="ai",
            original_widget_id=source_widget_id,
            like_count=0,
            favorite_count=0,
            remix_count=0,
            created_at=now,
            updated_at=now,
        )
        db.add(new_row)
        db.add(UserWidgetView(
            user_id=new_owner_id, widget_id=new_id,
            view_type="created", added_at=now,
        ))

        # 原作 remix_count++（用 ORM mutation 保持 identity map 同步）
        source.remix_count = (source.remix_count or 0) + 1

        return new_row
    except Exception:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise


# ─────────────────────────────────────────────────────────────────────
# List endpoints — cache-aside
# ─────────────────────────────────────────────────────────────────────


_DEFAULT_AI_ICON = "🧩"


def _read_icon_from_meta(widget_id: str) -> str:
    """读 widgets/{id}/meta.json 取 icon emoji。

    AI widget 的 icon 不存 ai_widgets 表（避免 schema 漂移），meta.json 是
    单一真相源。列表端点用这个，结果会被 redis 缓存 60s，单次列表的 N 次
    磁盘 read 远低于 cache miss 之外的开销。

    历史 widget 不少 `icon: null`（早期 AI 没填），fallback 到 puzzle 拼图
    avoid 让 Mine tab 的缩略图变成空白盒。
    """
    meta_path = WIDGETS_DIR / widget_id / "meta.json"
    if not meta_path.is_file():
        return _DEFAULT_AI_ICON
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        icon = meta.get("icon")
        if isinstance(icon, str) and icon.strip():
            return icon
    except Exception:
        pass
    return _DEFAULT_AI_ICON


# 内置 widget 不在 filesystem，icon 直接硬编码 — 与 iOS ProductCard 视觉对齐
_BUILTIN_ICONS = {
    "builtin_time":    "🕒",
    "builtin_weather": "☀️",
    "builtin_todo":    "✓",
    "builtin_focus":   "🍅",
    "builtin_photo":   "🖼",
}


def _row_to_dict(row: AIWidget) -> dict:
    """AIWidget ORM row → JSON-serializable dict（i18n 字段保留为字符串供端点解析）。"""
    if row.kind == "builtin":
        icon = _BUILTIN_ICONS.get(row.widget_id, "🧩")
    else:
        icon = _read_icon_from_meta(row.widget_id)
    return {
        "widget_id": row.widget_id,
        "owner_id": row.owner_id,
        "title_i18n_raw": row.title_i18n,
        "description_i18n_raw": row.description_i18n,
        "supported_sizes_raw": row.supported_sizes,
        "base_template_id": row.base_template_id,
        "visibility": row.visibility,
        "kind": row.kind,
        "original_widget_id": row.original_widget_id,
        "like_count": row.like_count,
        "favorite_count": row.favorite_count,
        "remix_count": row.remix_count,
        "icon": icon,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def get_explore_list(
    db: AsyncSession, *, limit: int = 200
) -> list[dict]:
    """读 explore_widget_view JOIN ai_widgets。Redis cache 60s。

    返回 raw dict（不含 user-specific flags 比如 liked_by_me），
    端点拿到后再 enrich per-user。
    """
    key = _key_explore()
    try:
        cached = await redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("[Widgets] redis GET explore failed: %s", e)

    rows = (
        await db.execute(
            select(AIWidget)
            .join(
                ExploreWidgetView,
                ExploreWidgetView.widget_id == AIWidget.widget_id,
            )
            .order_by(
                ExploreWidgetView.sort_score.desc(),
                ExploreWidgetView.added_at.desc(),
            )
            .limit(limit)
        )
    ).scalars().all()
    result = [_row_to_dict(r) for r in rows]

    try:
        await redis_client.setex(key, _CACHE_TTL_LIST, json.dumps(result))
    except Exception as e:
        logger.warning("[Widgets] redis SETEX explore failed: %s", e)
    return result


async def get_mine_list(
    db: AsyncSession, *, user_id: str
) -> list[dict]:
    """Mine = 自创 (user_widget_view view_type='created') UNION 收藏 (widget_favorites)。

    去重逻辑：自创优先（如果作者也收藏了自己的，view_type 显示 'created'）。
    Redis cache 60s。
    """
    if not user_id:
        return []
    key = _key_mine(user_id)
    try:
        cached = await redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning("[Widgets] redis GET mine failed: %s", e)

    # 1. 自创（view_type='created'）
    created_rows = (
        await db.execute(
            select(AIWidget, UserWidgetView.added_at)
            .join(
                UserWidgetView,
                UserWidgetView.widget_id == AIWidget.widget_id,
            )
            .where(
                UserWidgetView.user_id == user_id,
                UserWidgetView.view_type == "created",
            )
        )
    ).all()
    created_ids = {r.widget_id for r, _ in created_rows}

    # 2. 收藏（不含已自创的，避免重复行）
    fav_query = (
        select(AIWidget, WidgetFavorite.created_at)
        .join(
            WidgetFavorite,
            WidgetFavorite.widget_id == AIWidget.widget_id,
        )
        .where(WidgetFavorite.user_id == user_id)
    )
    if created_ids:
        fav_query = fav_query.where(~AIWidget.widget_id.in_(created_ids))
    favorite_rows = (await db.execute(fav_query)).all()

    # 合并并按 added_at desc 排序
    all_entries: list[tuple] = []
    for row, added_at in created_rows:
        all_entries.append((row, "created", added_at))
    for row, added_at in favorite_rows:
        all_entries.append((row, "favorited", added_at))
    all_entries.sort(key=lambda t: t[2] or datetime.min, reverse=True)

    result = []
    for row, view_type, added_at in all_entries:
        d = _row_to_dict(row)
        d["view_type"] = view_type
        d["mine_added_at"] = added_at.isoformat() if added_at else None
        result.append(d)

    try:
        await redis_client.setex(key, _CACHE_TTL_LIST, json.dumps(result))
    except Exception as e:
        logger.warning("[Widgets] redis SETEX mine failed: %s", e)
    return result
