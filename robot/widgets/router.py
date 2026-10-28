"""Widget marketplace API — explore / mine / like / favorite / remix / visibility.

每个 widget 在 ai_widgets 表里有唯一一行 + filesystem 上 widgets/{widget_id}/
一份目录（kind='ai' 时）。Tab 分布由两张视图表派生：
- explore_widget_view 全局共享（暂时）
- user_widget_view per-user（自创 + 收藏）

详细数据流见 service.py 注释。
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user, get_current_user_optional
from robot.db.database import get_db
from robot.db.models import AIWidget, User, UserWidgetView, WidgetFavorite, WidgetLike
from robot.i18n import _, get_locale
from robot.widgets import service as widget_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/widgets", tags=["widgets"])

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
WIDGETS_DIR = _BACKEND_ROOT / "widgets"

_VALID_VISIBILITY = {"public_remixable", "public_view_only", "private"}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _localize_title_desc(raw_title_i18n: str, raw_desc_i18n: str) -> tuple[str, str]:
    """从 JSON 字符串里取当前 locale 字段，做 en/zh-Hans 兜底。"""
    locale = get_locale()
    title = ""
    description = ""
    try:
        t_map = json.loads(raw_title_i18n) if raw_title_i18n else {}
        if isinstance(t_map, dict):
            title = (
                t_map.get(locale)
                or t_map.get("en")
                or t_map.get("zh-Hans")
                or ""
            )
    except Exception:
        title = raw_title_i18n or ""
    try:
        d_map = json.loads(raw_desc_i18n) if raw_desc_i18n else {}
        if isinstance(d_map, dict):
            description = (
                d_map.get(locale)
                or d_map.get("en")
                or d_map.get("zh-Hans")
                or ""
            )
    except Exception:
        description = raw_desc_i18n or ""
    return title, description


async def _build_owner_info(
    db: AsyncSession, raw_list: list[dict]
) -> dict[str, dict]:
    """根据 raw_list 中的 owner_id 批量拉一次 users 表 → id → owner 摘要。"""
    from sqlalchemy import select as _select
    from robot.db.models import User as _User
    ids = list({r.get("owner_id") for r in raw_list if r.get("owner_id")})
    if not ids:
        return {}
    rows = await db.execute(_select(_User).where(_User.id.in_(ids)))
    out: dict[str, dict] = {}
    for u in rows.scalars().all():
        out[u.id] = {
            "username": u.username,
            "display_name": u.display_name,
            "avatar_url": u.avatar_url,
        }
    return out


def _enrich_for_user(
    raw: dict, *, current_user_id: str | None,
    liked_set: set[str], favorited_set: set[str], created_set: set[str],
    owner_info: dict[str, dict] | None = None,
) -> dict:
    """把 service.get_*_list 返回的 raw dict 转成 iOS 端可消费的 widget 元数据。"""
    title, description = _localize_title_desc(
        raw.get("title_i18n_raw", ""), raw.get("description_i18n_raw", "")
    )
    try:
        title_i18n = json.loads(raw.get("title_i18n_raw", "") or "{}")
    except Exception:
        title_i18n = {}
    try:
        description_i18n = json.loads(
            raw.get("description_i18n_raw", "") or "{}"
        )
    except Exception:
        description_i18n = {}
    try:
        sizes = json.loads(raw.get("supported_sizes_raw", "") or "[]")
    except Exception:
        sizes = []

    widget_id = raw["widget_id"]
    is_owner = bool(current_user_id and raw.get("owner_id") == current_user_id)

    owner = (owner_info or {}).get(raw.get("owner_id") or "", {})
    return {
        "widget_id": widget_id,
        "owner_id": raw.get("owner_id"),
        "owner_username": owner.get("username"),
        "owner_display_name": owner.get("display_name"),
        "owner_avatar_url": owner.get("avatar_url"),
        "title": title,
        "description": description,
        "title_i18n": title_i18n,
        "description_i18n": description_i18n,
        "icon": raw.get("icon", ""),
        "supported_sizes": sizes,
        "kind": raw.get("kind", "ai"),
        "visibility": raw.get("visibility", "private"),
        "original_widget_id": raw.get("original_widget_id"),
        "like_count": raw.get("like_count", 0),
        "favorite_count": raw.get("favorite_count", 0),
        "remix_count": raw.get("remix_count", 0),
        "liked_by_me": widget_id in liked_set,
        "favorited_by_me": widget_id in favorited_set,
        "is_owner": is_owner,
        "view_type": raw.get("view_type"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


async def _user_flag_sets(
    db: AsyncSession, user_id: str | None, widget_ids: list[str]
) -> tuple[set[str], set[str], set[str]]:
    """一次 SQL 把 (liked, favorited, created) 三个 set 都拉出来 — 避免 N+1。"""
    if not user_id or not widget_ids:
        return set(), set(), set()

    liked_rows = (
        await db.execute(
            select(WidgetLike.widget_id).where(
                WidgetLike.user_id == user_id,
                WidgetLike.widget_id.in_(widget_ids),
            )
        )
    ).scalars().all()
    favorited_rows = (
        await db.execute(
            select(WidgetFavorite.widget_id).where(
                WidgetFavorite.user_id == user_id,
                WidgetFavorite.widget_id.in_(widget_ids),
            )
        )
    ).scalars().all()
    created_rows = (
        await db.execute(
            select(UserWidgetView.widget_id).where(
                UserWidgetView.user_id == user_id,
                UserWidgetView.widget_id.in_(widget_ids),
                UserWidgetView.view_type == "created",
            )
        )
    ).scalars().all()
    return set(liked_rows), set(favorited_rows), set(created_rows)


# ─────────────────────────────────────────────────────────────────────
# List endpoints — Explore / Mine
# ─────────────────────────────────────────────────────────────────────


@router.get("/explore")
async def list_explore(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """全局探索区列表 — 公开可见的所有 widget（含 builtin + AI public_*）。"""
    raw_list = await widget_service.get_explore_list(db, limit=200)
    widget_ids = [r["widget_id"] for r in raw_list]
    user_id = current_user.id if current_user else None
    liked, favorited, created = await _user_flag_sets(db, user_id, widget_ids)
    owner_info = await _build_owner_info(db, raw_list)

    return {
        "widgets": [
            _enrich_for_user(
                r,
                current_user_id=user_id,
                liked_set=liked,
                favorited_set=favorited,
                created_set=created,
                owner_info=owner_info,
            )
            for r in raw_list
        ]
    }


@router.get("/mine")
async def list_mine(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前用户「我的」Tab — 自创 + 收藏。"""
    raw_list = await widget_service.get_mine_list(db, user_id=current_user.id)
    widget_ids = [r["widget_id"] for r in raw_list]
    liked, favorited, created = await _user_flag_sets(
        db, current_user.id, widget_ids
    )
    owner_info = await _build_owner_info(db, raw_list)

    return {
        "widgets": [
            _enrich_for_user(
                r,
                current_user_id=current_user.id,
                liked_set=liked,
                favorited_set=favorited,
                created_set=created,
                owner_info=owner_info,
            )
            for r in raw_list
        ]
    }


# ─────────────────────────────────────────────────────────────────────
# Legacy compat — GET /api/widgets
# ─────────────────────────────────────────────────────────────────────


@router.get("")
async def list_widgets_legacy(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """向后兼容旧 iOS — 返回 mine + 内置 explore 作为 seeds。

    新 iOS 应该直接调 /explore + /mine。
    """
    user_widgets: list[dict] = []
    if current_user is not None:
        mine = await widget_service.get_mine_list(db, user_id=current_user.id)
        widget_ids = [r["widget_id"] for r in mine]
        liked, favorited, created = await _user_flag_sets(
            db, current_user.id, widget_ids
        )
        for r in mine:
            user_widgets.append(_enrich_for_user(
                r, current_user_id=current_user.id,
                liked_set=liked, favorited_set=favorited, created_set=created,
            ))
    explore = await widget_service.get_explore_list(db)
    seeds = [
        _enrich_for_user(
            r, current_user_id=None,
            liked_set=set(), favorited_set=set(), created_set=set(),
        )
        for r in explore if r.get("kind") == "builtin"
    ]
    return {"widgets": user_widgets, "seeds": seeds}


# ─────────────────────────────────────────────────────────────────────
# Files / download
# ─────────────────────────────────────────────────────────────────────


async def _check_files_access(
    db: AsyncSession, widget_id: str, user_id: str | None
) -> AIWidget:
    """校验当前请求能否访问该 widget 的源码。

    - private：仅 owner 可访问
    - public_*：任何人都可访问（包括匿名）
    - builtin：理论上不应该 fetch /files（无目录），但允许通过返回元数据
    """
    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))
    if row.visibility == "private" and row.owner_id != user_id:
        # 不暴露 widget 是否存在
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))
    return row


@router.get("/{widget_id}/download")
async def download_widget(
    widget_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """下载 widget zip。private 仅 owner 可下。builtin 无文件 → 404。"""
    user_id = current_user.id if current_user else None
    row = await _check_files_access(db, widget_id, user_id)
    if row.kind == "builtin":
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))

    zip_path = WIDGETS_DIR / f"{widget_id}.zip"
    widget_dir = WIDGETS_DIR / widget_id
    if not zip_path.exists():
        if widget_dir.is_dir():
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip", dir=WIDGETS_DIR)
            os.close(tmp_fd)
            try:
                shutil.make_archive(tmp_path[:-4], "zip", str(widget_dir))
                os.replace(tmp_path, str(zip_path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        else:
            raise HTTPException(status_code=404, detail=_("WIDGET_ZIP_NOT_FOUND"))
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"{widget_id}.zip",
    )


@router.get("/{widget_id}/files")
async def get_widget_files(
    widget_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """获取 widget 全部文本文件。private 仅 owner；builtin 无目录 → 空。"""
    user_id = current_user.id if current_user else None
    row = await _check_files_access(db, widget_id, user_id)
    if row.kind == "builtin":
        # 内置无目录可读 — iOS 端会用本地原生视图渲染
        return {"widget_id": widget_id, "files": {}, "kind": "builtin"}

    widget_dir = WIDGETS_DIR / widget_id
    if not widget_dir.is_dir():
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))

    text_exts = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg"}
    files: dict[str, str] = {}
    for f in widget_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in text_exts and f.name != "CLAUDE.md":
            rel = str(f.relative_to(widget_dir))
            try:
                files[rel] = f.read_text(encoding="utf-8")
            except Exception:
                pass
    return {"widget_id": widget_id, "files": files}


# ─────────────────────────────────────────────────────────────────────
# Like / Favorite
# ─────────────────────────────────────────────────────────────────────


@router.post("/{widget_id}/like")
async def toggle_like_endpoint(
    widget_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))

    liked, like_count = await widget_service.toggle_like(
        db, current_user.id, widget_id
    )
    await db.commit()
    await widget_service.invalidate_widget_caches(
        widget_id,
        affected_user_ids=[current_user.id, row.owner_id] if row.owner_id else [current_user.id],
        invalidate_explore=True,
    )
    return {"liked_by_me": liked, "like_count": like_count}


@router.post("/{widget_id}/favorite")
async def toggle_favorite_endpoint(
    widget_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))

    # widget_favorites 与 user_widget_view 解耦后，作者也能 favorite 自己 —
    # 完全 toggle，不再特判 owner。
    existing = (
        await db.execute(
            select(WidgetFavorite).where(
                WidgetFavorite.user_id == current_user.id,
                WidgetFavorite.widget_id == widget_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        deleted, fav_count = await widget_service.remove_favorite(
            db, current_user.id, widget_id
        )
        await db.commit()
        favorited = False
    else:
        inserted, fav_count = await widget_service.add_favorite(
            db, current_user.id, widget_id
        )
        await db.commit()
        favorited = True

    await widget_service.invalidate_widget_caches(
        widget_id,
        affected_user_ids=[current_user.id],
        invalidate_explore=True,
    )
    return {"favorited_by_me": favorited, "favorite_count": fav_count}


# ─────────────────────────────────────────────────────────────────────
# Visibility
# ─────────────────────────────────────────────────────────────────────


class VisibilityBody(BaseModel):
    visibility: str


# ─────────────────────────────────────────────────────────────────────
# Title (rename)
# ─────────────────────────────────────────────────────────────────────


class TitleBody(BaseModel):
    title: str


_WIDGET_TITLE_MAX_LEN = 40


@router.patch("/{widget_id}/title")
async def update_widget_title(
    widget_id: str,
    body: TitleBody = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner-only widget rename。和 game rename 对称：用户输入任一语言，
    LLM 补另一种语言写进 `title_i18n`。"""
    new_title = (body.title or "").strip()
    if not new_title:
        raise HTTPException(status_code=400, detail=_("WIDGET_TITLE_EMPTY"))
    if len(new_title) > _WIDGET_TITLE_MAX_LEN:
        raise HTTPException(status_code=400, detail=_("WIDGET_TITLE_TOO_LONG"))

    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))
    if row.kind == "builtin":
        raise HTTPException(
            status_code=403, detail=_("WIDGET_BUILTIN_VISIBILITY_LOCKED")
        )
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("WIDGET_NOT_OWNED"))

    from robot.agent.title_translator import translate_title
    title_i18n = await translate_title(new_title)

    try:
        await widget_service.update_title(db, widget_id, title_i18n=title_i18n)
    except LookupError:
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))
    await db.commit()
    await widget_service.invalidate_widget_caches(
        widget_id,
        affected_user_ids=[current_user.id],
        invalidate_explore=True,
    )
    return {
        "widget_id": widget_id,
        "title": new_title,
        "title_i18n": title_i18n,
    }


@router.patch("/{widget_id}/visibility")
async def update_visibility(
    widget_id: str,
    body: VisibilityBody = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.visibility not in _VALID_VISIBILITY:
        raise HTTPException(status_code=400, detail=_("WIDGET_VISIBILITY_INVALID"))

    row = (
        await db.execute(
            select(AIWidget).where(AIWidget.widget_id == widget_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))
    if row.kind == "builtin":
        raise HTTPException(status_code=403, detail=_("WIDGET_BUILTIN_VISIBILITY_LOCKED"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("WIDGET_NOT_OWNED"))

    try:
        await widget_service.set_visibility(db, widget_id, body.visibility)
    except ValueError:
        raise HTTPException(status_code=400, detail=_("WIDGET_VISIBILITY_INVALID"))
    await db.commit()
    await widget_service.invalidate_widget_caches(
        widget_id,
        affected_user_ids=[current_user.id],
        invalidate_explore=True,
    )
    return {"widget_id": widget_id, "visibility": body.visibility}


# ─────────────────────────────────────────────────────────────────────
# Remix
# ─────────────────────────────────────────────────────────────────────


@router.post("/{widget_id}/remix")
async def remix_widget_endpoint(
    widget_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """clone widget 文件 + 元数据，原作 remix_count++，新副本 visibility=private。"""
    try:
        new_row = await widget_service.clone_widget(
            db, source_widget_id=widget_id, new_owner_id=current_user.id,
        )
    except widget_service.RemixError as e:
        raise HTTPException(status_code=e.http_status, detail=_(e.code))

    await db.commit()
    await widget_service.invalidate_widget_caches(
        widget_id,
        affected_user_ids=[current_user.id],
        invalidate_explore=True,
    )
    await widget_service.invalidate_widget_caches(
        new_row.widget_id,
        affected_user_ids=[current_user.id],
        invalidate_explore=False,  # 新行默认 private 不影响 explore
    )

    return {
        "widget_id": new_row.widget_id,
        "original_widget_id": new_row.original_widget_id,
        "owner_id": new_row.owner_id,
        "visibility": new_row.visibility,
    }


# ─────────────────────────────────────────────────────────────────────
# Delete
# ─────────────────────────────────────────────────────────────────────


@router.delete("/{widget_id}")
async def delete_widget(
    widget_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """owner-only 删除。

    显式清理三张子表（widget_likes / user_widget_view / explore_widget_view）—
    生产 MySQL 上 FK ON DELETE CASCADE 会兜底，但 SQLite 测试不强制 FK，
    显式删除让两边行为一致，并且避免依赖 ORM cascade 配置漂移。
    """
    from sqlalchemy import delete as sa_delete
    from robot.db.models import ExploreWidgetView, UserWidgetView, WidgetFavorite, WidgetLike

    row = (
        await db.execute(select(AIWidget).where(AIWidget.widget_id == widget_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("WIDGET_NOT_FOUND"))
    if row.kind == "builtin":
        raise HTTPException(status_code=403, detail=_("WIDGET_SEED_NOT_DELETABLE"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("WIDGET_NOT_OWNED"))

    widget_dir = WIDGETS_DIR / widget_id
    zip_path = WIDGETS_DIR / f"{widget_id}.zip"

    if widget_dir.is_dir():
        shutil.rmtree(widget_dir, ignore_errors=True)
    if zip_path.exists():
        zip_path.unlink()

    # 显式清理子表
    await db.execute(
        sa_delete(ExploreWidgetView).where(ExploreWidgetView.widget_id == widget_id)
    )
    await db.execute(
        sa_delete(UserWidgetView).where(UserWidgetView.widget_id == widget_id)
    )
    await db.execute(
        sa_delete(WidgetLike).where(WidgetLike.widget_id == widget_id)
    )
    await db.execute(
        sa_delete(WidgetFavorite).where(WidgetFavorite.widget_id == widget_id)
    )
    await db.delete(row)
    await db.commit()
    await widget_service.invalidate_widget_caches(
        widget_id,
        affected_user_ids=[current_user.id],
        invalidate_explore=True,
    )

    logger.info(
        "[Widgets] deleted widget: %s (owner=%s)", widget_id, current_user.id
    )
    return {"status": "deleted", "widget_id": widget_id}
