"""Game marketplace API — explore / mine / like / favorite / remix / visibility.

每个 game 在 ai_games 表里有唯一一行 + filesystem 上 games/{game_id}/ 一份
Godot 工程目录（kind='ai' 时）。Tab 分布由两张视图表派生：
- explore_game_view 全局共享（暂时）
- user_game_view per-user（自创 + 收藏）

兼容期：老 game_stats / game_likes 表保留双写，下个 release 后再 drop。
旧 GET /api/games 端点继续工作（自动 fall-through 到新 explore + 自创 mine 拼接），
让老 iOS 不升级也能 list。
"""

import asyncio
import hashlib
import io
import json
import time
import logging
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user, get_current_user_optional
from robot.db.database import get_db
from robot.db.models import (
    AIGame,
    GameLike,
    GameStats,
    User,
    UserGameView,
)
from robot.i18n import _, get_locale
from robot.games import service as game_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/games", tags=["games"])

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
GAMES_DIR = _BACKEND_ROOT / "games"
AUDIO_ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "audio"
AUDIO_LIBRARY_DIR = AUDIO_ASSETS_DIR / "library"
BGM_PREVIEW_DIR = AUDIO_ASSETS_DIR / "bgm_previews"
AUDIO_MANIFEST = AUDIO_ASSETS_DIR / "manifest.json"

_VALID_VISIBILITY = {"public_remixable", "public_view_only", "private"}

# zip 体积上限（防止恶意超大 AI 游戏）
_ZIP_WARN_BYTES = 50 * 1024 * 1024
_ZIP_REJECT_BYTES = 100 * 1024 * 1024

# Per-game asyncio.Lock 保护 zip on-demand 重打（download endpoint）。
#
# 没有这把锁，两个并发 GET /{id}/download 请求碰上 zip 缺失时会同时进
# `make_archive` 分支 —— 两个 to_thread 同时往 `<game_id>.zip` 写，第二个
# overwrite 第一个产生半截 / 损坏的 zip，客户端解压失败误报「下载失败」。
#
# 用 `defaultdict(asyncio.Lock)` 让每个 game_id 一把独立锁，互不阻塞；
# 使用频率极低（仅 zip 缺失分支命中），常驻内存可忽略。Process 重启自然
# 清空，没必要做 LRU。
from collections import defaultdict

_zip_repackage_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Cover 上传上限 + 输出尺寸
_COVER_MAX_BYTES = 5 * 1024 * 1024
# 输出 cover 的最大宽度（保持原比例缩放，不裁切）。
# iOS 是 landscape iPhone 16 Pro Max，全屏 2868×1320，按 max_width=1280 缩放后
# ~1280×590，文件 ~150-300KB，质量足够还原游戏画面。
_COVER_MAX_WIDTH = 1280

# Game asset (background image) upload limits — per-image and per-batch
# 单图 8MB；单次 batch 最多 10 张。批量上限既是性能约束也是 abuse 防护
# （AI agent 看到几十张 asset 文件也容易选择困难）。
_ASSET_MAX_BYTES = 8 * 1024 * 1024
_ASSET_MAX_BATCH = 10
# 安全的 asset 文件名字符集（小写字母 / 数字 / 下划线 / 点）。
# 不允许 `/`、`\`、`..`、空格 —— 防 path traversal 和 Godot 加载怪异路径。
import re as _re
_ASSET_FILENAME_SAFE_RE = _re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9]+)?$")
_ASSET_ALLOWED_EXTS = {"png", "jpg", "jpeg", "webp"}
# 解码后 RGB 像素总数上限 — 防 PIL decode bomb：5MB 上传字节量看似不大，
# 但一张 7000×7000 PNG 解出来 RGB 会占 ~150MB 内存，几十用户并发上传可
# 直接 OOM。16MP（≈ 4096×4096）足够覆盖任何手机截图，且解码内存 < 50MB。
_COVER_MAX_PIXELS = 4096 * 4096


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
    """根据 raw_list 中的 owner_id 批量拉一次 users 表，返回 id → owner 摘要。

    iOS 端 AuthorBadge 用 display_name + avatar_url + username（点击跳 profile）。
    一次 SELECT IN 取代 N 次单查；缓存仅本次请求生命周期。
    """
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
    """raw dict (来自 service.get_*_list) → iOS 端可消费的 game 元数据。"""
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

    game_id = raw["game_id"]
    is_owner = bool(current_user_id and raw.get("owner_id") == current_user_id)

    # owner 走 ai_games.owner_favorited 列；非 owner 走 user_game_view favorited_set
    if is_owner:
        favorited_by_me = bool(raw.get("owner_favorited"))
    else:
        favorited_by_me = game_id in favorited_set

    owner = (owner_info or {}).get(raw.get("owner_id") or "", {})
    return {
        "game_id": game_id,
        "owner_id": raw.get("owner_id"),
        "owner_username": owner.get("username"),
        "owner_display_name": owner.get("display_name"),
        "owner_avatar_url": owner.get("avatar_url"),
        "title": title or raw.get("title", ""),
        "description": description,
        "title_i18n": title_i18n,
        "description_i18n": description_i18n,
        "icon": raw.get("icon", "🎮"),
        "input_mode": raw.get("input_mode", "touch"),
        "kind": raw.get("kind", "ai"),
        "visibility": raw.get("visibility", "private"),
        "original_game_id": raw.get("original_game_id"),
        "template_from": raw.get("template_from") or "",
        "session_id": raw.get("session_id"),
        "like_count": raw.get("like_count", 0),
        "favorite_count": raw.get("favorite_count", 0),
        "share_count": raw.get("share_count", 0),
        "remix_count": raw.get("remix_count", 0),
        "liked_by_me": game_id in liked_set,
        "favorited_by_me": favorited_by_me,
        "is_owner": is_owner,
        "view_type": raw.get("view_type"),
        # cover_url 由 cover_set_at 推导：cache hit 时 mtime 已在 raw dict 里，
        # 不再 stat 文件；cache miss 时 service._row_to_dict 读一次 mtime 写
        # 进 raw 一并 cache。
        "cover_url": (f"/api/games/{game_id}/cover"
                       if raw.get("_cover_set_at") is not None else None),
        "cover_set_at": raw.get("_cover_set_at"),
        "content_modified_at": raw.get("_content_modified_at"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


async def _user_flag_sets(
    db: AsyncSession, user_id: str | None, game_ids: list[str]
) -> tuple[set[str], set[str], set[str]]:
    """一次 SQL 把 (liked, favorited, created) 三个 set 都拉出来 — 避免 N+1。"""
    if not user_id or not game_ids:
        return set(), set(), set()

    liked_rows = (
        await db.execute(
            select(GameLike.game_id).where(
                GameLike.user_id == user_id,
                GameLike.game_id.in_(game_ids),
            )
        )
    ).scalars().all()
    view_rows = (
        await db.execute(
            select(UserGameView.game_id, UserGameView.view_type).where(
                UserGameView.user_id == user_id,
                UserGameView.game_id.in_(game_ids),
            )
        )
    ).all()
    favorited = {gid for gid, vt in view_rows if vt == "favorited"}
    created = {gid for gid, vt in view_rows if vt == "created"}
    return set(liked_rows), favorited, created


def _game_dir_exists(game_id: str) -> bool:
    return (GAMES_DIR / game_id).is_dir()


async def _ensure_ai_game_row(
    db: AsyncSession, game_id: str
) -> AIGame | None:
    """获取 ai_games 行；若不存在但 fs 上有目录 + meta.json，则懒回灌一行。

    产线场景：alembic 迁移漏扫某个老 ai_* 目录，或者新生成游戏 DB 写入失败但
    fs 写入成功。回灌让端点不至于 404，又把数据补齐。

    fs 上也没有 → 返回 None（caller 抛 404）。
    """
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is not None:
        return row
    # fs fallback
    game_dir = GAMES_DIR / game_id
    meta_file = game_dir / "meta.json"
    if not game_dir.is_dir() or not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    now = game_service._utcnow()
    title = meta.get("title", game_id)
    title_i18n_raw = meta.get("title_i18n") or {"en": title, "zh-Hans": title}
    desc = meta.get("description", "") or ""
    desc_i18n_raw = meta.get("description_i18n") or {"en": desc, "zh-Hans": desc}
    row = AIGame(
        game_id=game_id,
        owner_id=meta.get("owner_id") or None,
        title=title,
        title_i18n=json.dumps(title_i18n_raw, ensure_ascii=False),
        description=desc or None,
        description_i18n=json.dumps(desc_i18n_raw, ensure_ascii=False),
        icon=meta.get("icon", "🎮"),
        input_mode=meta.get("input_mode", "touch"),
        kind="ai",
        visibility="private",
        original_game_id=None,
        template_from=meta.get("template_from", "") or "",
        session_id=meta.get("session_id"),
        like_count=0, favorite_count=0, share_count=0, remix_count=0,
        created_at=now, updated_at=now,
    )
    db.add(row)
    if meta.get("owner_id"):
        view_existing = (
            await db.execute(
                select(UserGameView).where(
                    UserGameView.user_id == meta["owner_id"],
                    UserGameView.game_id == game_id,
                )
            )
        ).scalar_one_or_none()
        if view_existing is None:
            db.add(UserGameView(
                user_id=meta["owner_id"], game_id=game_id,
                view_type="created", added_at=now,
            ))
    await db.flush()
    logger.info("[Games] lazy-rehydrated ai_games row from fs: %s", game_id)
    return row


# ─────────────────────────────────────────────────────────────────────
# Template bundle — used by Motem Studio (macOS) to fetch the same
# `_template/` skeleton and slow-path CLAUDE.md that the iOS backend
# generation pipeline uses. macOS keeps a local mirror so a terminal
# user's claude/codex/gemini can author games against the identical
# contract iOS games are generated from.
# ─────────────────────────────────────────────────────────────────────


_TEMPLATE_DIR = GAMES_DIR / "_template"
_TEMPLATE_SLOWPATH_CLAUDE_MD = GAMES_DIR / "_template_slowpath" / "CLAUDE.md"
_TEMPLATE_BUNDLE_PATH = GAMES_DIR / ".template-bundle.tar.gz"
_TEMPLATE_BUNDLE_ETAG_PATH = GAMES_DIR / ".template-bundle.etag"
_TEMPLATE_BUNDLE_LOCK = asyncio.Lock()


def _template_bundle_files() -> list[Path]:
    """Files included in the bundle, deterministic order. `.godot` /
    `.git` engine artefacts are skipped at tar time. The audio manifest
    is included only as an etag input (not packed) so the bundle
    invalidates when the audio catalogue changes — the macOS Motem
    client receives a freshly-rendered CLAUDE.md."""
    files: list[Path] = []
    if _TEMPLATE_DIR.is_dir():
        for path in sorted(_TEMPLATE_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(_TEMPLATE_DIR).parts
            if ".godot" in rel_parts or ".git" in rel_parts:
                continue
            files.append(path)
    if _TEMPLATE_SLOWPATH_CLAUDE_MD.is_file():
        files.append(_TEMPLATE_SLOWPATH_CLAUDE_MD)
    if AUDIO_MANIFEST.is_file():
        files.append(AUDIO_MANIFEST)
    return files


def _template_bundle_etag(files: list[Path]) -> str:
    """Stable hash of `(rel_path, mtime_ns)` for every input. Any edit
    to a template file (or addition/removal) changes the etag."""
    h = hashlib.sha256()
    for path in files:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        h.update(str(path).encode("utf-8"))
        h.update(b":")
        h.update(str(mtime_ns).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:32]


def _pack_template_bundle_sync(etag: str) -> None:
    """Write `_template/` and `_template_slowpath/CLAUDE.md` into a
    tar.gz next to the games dir. Caller holds `_TEMPLATE_BUNDLE_LOCK`.

    The archive layout matches the on-disk layout under `games/`:

        _template/
            project.godot
            scenes/main.tscn
            ...
        _template_slowpath/
            CLAUDE.md
    """
    tmp = _TEMPLATE_BUNDLE_PATH.with_suffix(".tar.gz.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)

    def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if ".godot" in parts or ".git" in parts or ".DS_Store" in parts:
            return None
        return info

    with tarfile.open(tmp, "w:gz") as tar:
        if _TEMPLATE_DIR.is_dir():
            tar.add(_TEMPLATE_DIR, arcname="_template", filter=_tar_filter)
        if _TEMPLATE_SLOWPATH_CLAUDE_MD.is_file():
            # Substitute the live audio catalogue into the CLAUDE.md
            # placeholder before packing so macOS clients receive the
            # same rendered content the backend SDK injects. Avoids
            # shipping a literal `{audio_section}` token to the model.
            from robot.agent.tools.game_tools import _render_slowpath_claude_md
            rendered = _render_slowpath_claude_md().encode("utf-8")
            info = tarfile.TarInfo(name="_template_slowpath/CLAUDE.md")
            info.size = len(rendered)
            info.mtime = int(_TEMPLATE_SLOWPATH_CLAUDE_MD.stat().st_mtime)
            info.mode = 0o644
            import io as _io
            tar.addfile(info, _io.BytesIO(rendered))
    tmp.replace(_TEMPLATE_BUNDLE_PATH)
    _TEMPLATE_BUNDLE_ETAG_PATH.write_text(etag, encoding="utf-8")


async def _ensure_template_bundle(etag: str) -> None:
    """Repack the tar.gz only when the etag has drifted from the cached
    value. Concurrent requests share one rebuild via the lock."""
    if (
        _TEMPLATE_BUNDLE_PATH.exists()
        and _TEMPLATE_BUNDLE_ETAG_PATH.exists()
        and _TEMPLATE_BUNDLE_ETAG_PATH.read_text(encoding="utf-8").strip() == etag
    ):
        return
    async with _TEMPLATE_BUNDLE_LOCK:
        # Double-check after acquiring the lock; the first waiter may
        # have already finished by the time we got here.
        if (
            _TEMPLATE_BUNDLE_PATH.exists()
            and _TEMPLATE_BUNDLE_ETAG_PATH.exists()
            and _TEMPLATE_BUNDLE_ETAG_PATH.read_text(encoding="utf-8").strip() == etag
        ):
            return
        await asyncio.to_thread(_pack_template_bundle_sync, etag)
        logger.info("[Games] re-packed template bundle, etag=%s", etag)


@router.get("/template-bundle")
async def get_template_bundle(if_none_match: str | None = Header(default=None)):
    """Return the Godot template skeleton + slow-path CLAUDE.md as a
    `tar.gz`. The ETag is derived from input file mtimes, so a client
    that sends `If-None-Match: "<etag>"` matching the current bundle
    receives a 304 with no body.

    Used by Motem Studio's `TemplateBundleSync` on app launch /
    post-login. No auth required — templates are public content.
    """
    files = _template_bundle_files()
    if not files:
        raise HTTPException(status_code=404, detail="template not available")
    etag = _template_bundle_etag(files)
    quoted_etag = f'"{etag}"'
    if if_none_match and if_none_match.strip() == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})

    await _ensure_template_bundle(etag)
    return FileResponse(
        path=str(_TEMPLATE_BUNDLE_PATH),
        media_type="application/gzip",
        filename="template-bundle.tar.gz",
        headers={"ETag": quoted_etag},
    )


# ─────────────────────────────────────────────────────────────────────
# Upload — used by Motem Studio (macOS) to publish a locally-authored
# game so the same owner's iPhone receives it through the normal
# `game_ready` push pipeline.
# ─────────────────────────────────────────────────────────────────────


_GAME_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,63}$")
_UPLOAD_REJECT_BYTES = _ZIP_REJECT_BYTES


def _gen_macos_game_id() -> str:
    """Stable but unique id for games uploaded from macOS Motem Studio.
    Mirrors the `ai_<unix_ts>_<md5[:6]>` convention with a `mac_`
    prefix so the source platform is recoverable at a glance."""
    ts = int(time.time())
    salt = hashlib.md5(f"{ts}-{time.perf_counter_ns()}".encode()).hexdigest()[:6]
    return f"mac_{ts}_{salt}"


def _safe_zip_member(name: str) -> bool:
    """Reject absolute paths, parent-traversal segments, and Windows
    drive letters. Plain-jane relative paths only."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if ":" in name:
        return False
    parts = Path(name).parts
    return ".." not in parts


@router.post("", status_code=201)
async def upload_game(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept a zipped Godot project + meta.json from macOS Motem Studio,
    persist it to disk, register the DB row, and notify the same user's
    iPhone via `game_ready` so the existing iOS download path picks it up.

    Request: `multipart/form-data` with one file field `file`. The zip
    must contain `project.godot` and `meta.json` at the top level (a
    single wrapping directory is auto-unwrapped).

    Response: `{ game_id, title_i18n, input_mode }`.
    """
    body = await file.read()
    if len(body) > _UPLOAD_REJECT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=_("GAME_OPERATION_ERROR", e="zip too large"),
        )

    with tempfile.TemporaryDirectory(prefix="motem-upload-") as tmp_str:
        tmp = Path(tmp_str)
        zip_path = tmp / "incoming.zip"
        zip_path.write_bytes(body)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    if not _safe_zip_member(member):
                        raise HTTPException(
                            status_code=400,
                            detail=f"unsafe zip member: {member!r}",
                        )
                extracted_root = tmp / "extracted"
                extracted_root.mkdir()
                zf.extractall(extracted_root)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="not a valid zip")

        # If the zip wraps everything inside a single directory, descend
        # so `project.godot` is at the root we operate against.
        project_root = extracted_root
        roots = [p for p in project_root.iterdir() if p.name != "__MACOSX"]
        if len(roots) == 1 and roots[0].is_dir():
            project_root = roots[0]

        project_godot = project_root / "project.godot"
        meta_path = project_root / "meta.json"
        if not project_godot.is_file():
            raise HTTPException(status_code=400, detail="missing project.godot")
        if not meta_path.is_file():
            raise HTTPException(status_code=400, detail="missing meta.json")

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"meta.json malformed: {exc}")

        # Resolve and validate game_id. The macOS Publish flow may
        # reuse an existing id (modify-and-republish) or omit it for
        # fresh creations.
        requested_id = meta.get("game_id")
        if requested_id:
            if not _GAME_ID_PATTERN.match(requested_id):
                raise HTTPException(status_code=400, detail="invalid game_id")
            game_id = requested_id
        else:
            game_id = _gen_macos_game_id()
            meta["game_id"] = game_id

        # If this id already exists, only the owner may overwrite.
        existing = await db.scalar(
            select(AIGame).where(AIGame.game_id == game_id)
        )
        if existing is not None and existing.owner_id != current_user.id:
            raise HTTPException(status_code=403, detail="game_id taken")

        # Persist `meta.json` with any normalisations (game_id) and
        # mark `modified_at` so iOS knows to re-download.
        meta["owner_id"] = current_user.id
        meta["modified_at"] = int(time.time())
        meta.setdefault("created_at", meta["modified_at"])
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Move into the canonical location, replacing any existing
        # owner-controlled copy. Repack the zip so `/download` serves
        # the cleaned-up version.
        dst_dir = GAMES_DIR / game_id
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.move(str(project_root), str(dst_dir))

        zip_dst = GAMES_DIR / f"{game_id}.zip"
        if zip_dst.exists():
            zip_dst.unlink()
        await asyncio.to_thread(
            shutil.make_archive,
            str(zip_dst.with_suffix("")), "zip", str(dst_dir),
        )

    # Pull the user-facing fields out of meta with sensible defaults.
    title = (meta.get("title") or "Untitled").strip() or "Untitled"
    title_i18n = meta.get("title_i18n") or {"en": title, "zh-Hans": title}
    description = meta.get("description") or ""
    description_i18n = meta.get("description_i18n") or {
        "en": description, "zh-Hans": description,
    }
    icon = meta.get("icon") or "🎮"
    input_mode = meta.get("input_mode") or "touch"
    if input_mode not in {"touch", "body_pose"}:
        input_mode = "touch"

    await game_service.upsert_game_row(
        game_id=game_id,
        owner_id=str(current_user.id),
        title=title,
        title_i18n=title_i18n,
        description=description,
        description_i18n=description_i18n,
        icon=icon,
        input_mode=input_mode,
        template_from="",
        session_id=None,
        visibility="private",
        kind="ai",
        db=db,
    )
    await db.commit()

    # Notify the same user's iPhone so iOS's existing download flow
    # picks the game up. Failures here are non-fatal — the game is
    # already on disk and addressable via /api/games.
    try:
        from robot.websocket import ws_manager

        await ws_manager.send_to_user(
            str(current_user.id),
            str(current_user.id),
            {
                "type": "robot_command",
                "data": {
                    "action": "game_ready",
                    "game_id": game_id,
                    "title": title,
                    "title_i18n": title_i18n,
                    "description_i18n": description_i18n,
                    "icon": icon,
                    "input_mode": input_mode,
                },
            },
        )
        logger.info(
            "[Games] uploaded from macOS + game_ready broadcast: %s", game_id,
        )
    except Exception as exc:
        logger.warning(
            "[Games] uploaded %s but game_ready broadcast failed: %s",
            game_id, exc,
        )

    return {
        "game_id": game_id,
        "title_i18n": title_i18n,
        "input_mode": input_mode,
    }


# ─────────────────────────────────────────────────────────────────────
# List endpoints — Explore / Mine
# ─────────────────────────────────────────────────────────────────────


@router.get("/explore")
async def list_explore(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """全局探索区列表 — 公开可见的所有 game（含 builtin + AI public_*）。

    Offset 分页 + 稳定二级排序（service 内 ORDER BY game_id asc 兜底）。
    service 仍然缓存"前 200 条"全量，避免每页都打 DB；在路由层做切片返回，
    next_offset / has_more 由切片位置算出。
    """
    raw_list = await game_service.get_explore_list(db, limit=200)
    total = len(raw_list)
    page_rows = raw_list[offset:offset + limit]
    game_ids = [r["game_id"] for r in page_rows]
    user_id = current_user.id if current_user else None
    liked, favorited, created = await _user_flag_sets(db, user_id, game_ids)
    owner_info = await _build_owner_info(db, page_rows)

    return {
        "games": [
            _enrich_for_user(
                r,
                current_user_id=user_id,
                liked_set=liked,
                favorited_set=favorited,
                created_set=created,
                owner_info=owner_info,
            )
            for r in page_rows
        ],
        "next_offset": offset + len(page_rows),
        "has_more": offset + len(page_rows) < total,
    }


@router.get("/mine")
async def list_mine(
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前用户「我的」Tab — 自创 + 收藏。Offset 分页，见 list_explore 同款。"""
    raw_list = await game_service.get_mine_list(db, user_id=current_user.id)
    total = len(raw_list)
    page_rows = raw_list[offset:offset + limit]
    game_ids = [r["game_id"] for r in page_rows]
    liked, favorited, created = await _user_flag_sets(
        db, current_user.id, game_ids
    )
    owner_info = await _build_owner_info(db, page_rows)

    return {
        "games": [
            _enrich_for_user(
                r,
                current_user_id=current_user.id,
                liked_set=liked,
                favorited_set=favorited,
                created_set=created,
                owner_info=owner_info,
            )
            for r in page_rows
        ],
        "next_offset": offset + len(page_rows),
        "has_more": offset + len(page_rows) < total,
    }


# ─────────────────────────────────────────────────────────────────────
# Legacy compat — GET /api/games（旧 iOS 仍能 list）
# ─────────────────────────────────────────────────────────────────────


@router.get("")
async def list_games_legacy(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """向后兼容旧 iOS — 把 explore + 用户自创 + fs 上未迁移的游戏合并后返回。

    新 iOS 应该直接调 /explore + /mine。fs walk fallback 用于：
    - 老 ai_* 目录还没被 alembic 数据迁移触达
    - 老 iOS 用户的 game_stats 路径仍有效
    """
    explore = await game_service.get_explore_list(db)
    mine = []
    if current_user is not None:
        mine = await game_service.get_mine_list(db, user_id=current_user.id)

    by_id: dict[str, dict] = {r["game_id"]: r for r in explore}
    for r in mine:
        by_id.setdefault(r["game_id"], r)

    # fs walk fallback — 老游戏未迁移到 ai_games 时也能 list（兼容期）
    if GAMES_DIR.is_dir():
        for d in sorted(GAMES_DIR.iterdir()):
            if not d.is_dir() or not d.name.startswith("ai_"):
                continue
            # Skip per-game CLAUDE_CONFIG_DIR siblings (see game_tools.py:819).
            # They start with ai_ too but hold SDK session jsonl, not a game.
            if d.name.endswith(".claude"):
                continue
            if d.name in by_id:
                continue
            meta_file = d / "meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    meta = {"title": d.name}
            else:
                # 没 meta.json：用目录名当 title 兜底（老 ai_* 可能漏写）
                meta = {"title": d.name}
            # 用 fs 上的 game_stats 老表填计数
            stats_row = (
                await db.execute(
                    select(GameStats).where(GameStats.game_id == d.name)
                )
            ).scalar_one_or_none()
            by_id[d.name] = {
                "game_id": d.name,
                "owner_id": meta.get("owner_id"),
                "title": meta.get("title", d.name),
                "title_i18n_raw": json.dumps(meta.get("title_i18n") or {}, ensure_ascii=False),
                "description": meta.get("description", ""),
                "description_i18n_raw": json.dumps(meta.get("description_i18n") or {}, ensure_ascii=False),
                "icon": meta.get("icon", "🎮"),
                "input_mode": meta.get("input_mode", "touch"),
                "kind": "ai",
                "visibility": "private",  # 兼容期当 private 处理（老用户改 visibility 后会回灌）
                "original_game_id": None,
                "template_from": meta.get("template_from", "") or "",
                "session_id": meta.get("session_id"),
                "like_count": stats_row.like_count if stats_row else 0,
                "favorite_count": 0,
                "share_count": stats_row.share_count if stats_row else 0,
                "remix_count": 0,
                "created_at": None,
                "updated_at": None,
            }

    game_ids = list(by_id.keys())
    user_id = current_user.id if current_user else None
    liked, favorited, created = await _user_flag_sets(db, user_id, game_ids)

    return {
        "games": [
            _enrich_for_user(
                r,
                current_user_id=user_id,
                liked_set=liked,
                favorited_set=favorited,
                created_set=created,
            )
            for r in by_id.values()
        ]
    }


# ─────────────────────────────────────────────────────────────────────
# BGM picker — kept as-is from old router
# ─────────────────────────────────────────────────────────────────────


@router.get("/audio/bgm-list")
async def list_bgm_tracks():
    """Return BGM catalog for the iOS BGM picker."""
    if not AUDIO_MANIFEST.exists():
        return {"bgms": []}
    try:
        data = json.loads(AUDIO_MANIFEST.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read audio manifest: %s", e)
        return {"bgms": []}

    bgms = []
    for sound in data.get("sounds", []):
        if sound.get("category") != "bgm":
            continue
        bgms.append({
            "id": sound["id"],
            "description": sound.get("description", ""),
            "preview_url": f"/api/games/audio/bgm-preview/{sound['id']}",
        })
    return {"bgms": bgms}


@router.get("/audio/bgm-preview/{bgm_id}")
async def get_bgm_preview(bgm_id: str):
    """Stream the m4a preview clip for a single BGM track."""
    if "/" in bgm_id or "\\" in bgm_id or ".." in bgm_id:
        raise HTTPException(status_code=400, detail="invalid bgm id")
    preview = BGM_PREVIEW_DIR / f"{bgm_id}.m4a"
    if not preview.exists():
        raise HTTPException(status_code=404, detail="preview not found")
    return FileResponse(
        path=str(preview),
        media_type="audio/mp4",
        filename=f"{bgm_id}.m4a",
    )


# ─────────────────────────────────────────────────────────────────────
# Inspirations — tap-to-fill suggestion cards for the new GameCreator chat tab
# ─────────────────────────────────────────────────────────────────────


@router.get("/inspirations")
async def list_inspirations():
    """Return inspiration cards for the iOS GameCreator's `idea` toggle.

    Honors `Accept-Language`. Auth is intentionally optional — these are
    public discovery content. Card list is currently hard-coded in
    `robot.games.inspirations`; promote to DB / admin CMS later without
    changing the response envelope.
    """
    from robot.games.inspirations import for_language
    cards = for_language(get_locale())
    return {"inspirations": cards}


# ─────────────────────────────────────────────────────────────────────
# Text-to-image — placeholder API for future implementation
# ─────────────────────────────────────────────────────────────────────


class _TextToImageRequest(BaseModel):
    prompt: str


@router.post("/text-to-image", status_code=501)
async def text_to_image_stub(
    body: _TextToImageRequest,
    current_user: User = Depends(get_current_user),
):
    """Placeholder for future text-to-image generation in the GameCreator's
    `image` tab. Always returns 501 Not Implemented for now — iOS catches this
    and shows a "coming soon" toast. Auth is required to avoid spammy abuse
    once we wire up a real generator.

    The Pydantic model enforces `prompt` field exists (422 otherwise) — that
    way clients implementing against this stub don't drift on the contract.
    """
    raise HTTPException(status_code=501, detail="not_implemented")


# ─────────────────────────────────────────────────────────────────────
# Files / download
# ─────────────────────────────────────────────────────────────────────


async def _check_files_access(
    db: AsyncSession, game_id: str, user_id: str | None
) -> AIGame:
    """校验访问。private 仅 owner 可访问；public_*/builtin 任何人可访问。

    若 ai_games 表里没有这一行 → 一律 404，**不再**对已登录用户走 fs
    fallback。

    Why: 老的「登录用户 fs fallback」让任意登录用户能下载所有未迁移到
    ai_games 表的私有 zip（三方 review 共识 P0：visibility=private 在
    fs fallback 路径被静默放过）。生产环境 games/ 下有大量 ai_* 目录，
    其中存在未入库行的概率非低。
    How to apply: builtin / public / 已迁移的 private 都在 ai_games 表里
    有 row（alembic 数据迁移 + service.upsert_game_row），row 缺失即可视
    为「该 game 不存在」。如果未来发现仍有真实老 ai_* 漏迁移导致用户报
    错，再在端点上做 `_ensure_ai_game_row` 懒回灌（默认 visibility=
    private），而不是放行 fs fallback。
    """
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.visibility == "private" and row.owner_id != user_id:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    return row


def _normalize_cover_image(raw_bytes: bytes) -> bytes:
    """把上传的图按原始比例缩放（**不裁切**）成 PNG，最大宽度 1280。

    早期版本 center-crop 成 16:9 600×337，但 iPhone 16 Pro Max landscape 实际
    比例 ≈ 932:430 ≈ 2.17:1，crop 后左右被裁掉关键内容（标题、按钮）。
    现在改成 resize-only：保留原比例、原构图，只是把分辨率压到 1280 宽。
    iOS 端 GameCoverView 用 .aspectRatio + scaledToFill 自适应，cover 与
    实际游戏画面 1:1 还原（不再有"裁剪后只看见中间一块"的问题）。

    安全：先读 image header 拿尺寸，超过 _COVER_MAX_PIXELS 直接拒绝（避免
    decode bomb 占内存）。Image.open 是 lazy 的，不到 .convert() 不实际
    decode 像素数据，所以 size 校验在 convert 之前发生。
    """
    img = Image.open(io.BytesIO(raw_bytes))
    src_w, src_h = img.size
    if src_w * src_h > _COVER_MAX_PIXELS:
        raise ValueError(
            f"cover image too many pixels ({src_w}×{src_h} = "
            f"{src_w * src_h} > {_COVER_MAX_PIXELS} cap)"
        )
    img = img.convert("RGB")
    if src_w > _COVER_MAX_WIDTH:
        new_w = _COVER_MAX_WIDTH
        new_h = int(src_h * (_COVER_MAX_WIDTH / src_w))
        img = img.resize((new_w, new_h), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _cover_url_for(game_id: str) -> str | None:
    """如果该 game_id 的 cover.png 存在则返回 URL，否则 nil。
    给 _enrich_for_user / 上传响应 / 缓存路径用。"""
    if (GAMES_DIR / game_id / "cover.png").exists():
        return f"/api/games/{game_id}/cover"
    return None


def _cover_set_at_for(game_id: str) -> int | None:
    """cover.png 的 mtime（unix timestamp）。仅给上传响应这种**直接 fs**
    场景用 — list 端点的 _enrich_for_user 从 cached _row_to_dict 读 mtime
    避免每个 request 都做 file IO。"""
    return game_service._read_cover_set_at(game_id)


@router.get("/{game_id}/download")
async def download_game(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """下载游戏 zip。private 仅 owner 可下；builtin 无目录 → 404。"""
    user_id = current_user.id if current_user else None
    row = await _check_files_access(db, game_id, user_id)
    if row.kind == "builtin":
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))

    zip_path = GAMES_DIR / f"{game_id}.zip"
    game_dir = GAMES_DIR / game_id

    # Repack zip if it's missing OR stale (game_dir/meta.json newer than zip).
    # Without the staleness check, batch migrations that bump meta.modified_at
    # on disk but don't touch zip cause iOS to download an old zip whose
    # internal meta.modified_at < server's content_modified_at → iOS sees
    # "stale" → re-downloads same old zip → infinite re-download loop.
    needs_repack = False
    if not zip_path.exists():
        needs_repack = True
    elif game_dir.is_dir():
        meta_path = game_dir / "meta.json"
        if meta_path.exists():
            try:
                if meta_path.stat().st_mtime > zip_path.stat().st_mtime:
                    needs_repack = True
            except OSError:
                pass

    if needs_repack:
        if game_dir.is_dir():
            # zip 打包是 CPU + 磁盘 IO 密集，同步跑会阻塞 FastAPI event loop
            # —— 大游戏目录可能 100-500ms。to_thread 让它在线程池里跑。
            #
            # Per-game lock 防止两个并发请求同时 make_archive 互相覆盖产生
            # 半截 zip。double-check 锁内重新判断 staleness，第二个 waiter
            # 拿到锁时第一个已经 repack，直接走 FileResponse。
            async with _zip_repackage_locks[game_id]:
                still_stale = not zip_path.exists()
                if zip_path.exists():
                    try:
                        meta_path = game_dir / "meta.json"
                        if meta_path.exists() and meta_path.stat().st_mtime > zip_path.stat().st_mtime:
                            still_stale = True
                    except OSError:
                        pass
                if still_stale:
                    await asyncio.to_thread(
                        shutil.make_archive,
                        str(zip_path.with_suffix("")), "zip", str(game_dir),
                    )
                    logger.info("[Games] re-packaged zip on demand: %s", zip_path.name)
        else:
            raise HTTPException(status_code=404, detail=_("GAME_ZIP_NOT_FOUND"))

    # 体积守门
    size = zip_path.stat().st_size
    if size > _ZIP_REJECT_BYTES:
        logger.error("[Games] reject oversized zip %s: %d bytes", game_id, size)
        raise HTTPException(status_code=413, detail=_("GAME_OPERATION_ERROR", e="zip too large"))
    if size > _ZIP_WARN_BYTES:
        logger.warning("[Games] oversized zip %s: %d bytes", game_id, size)

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"{game_id}.zip",
    )


@router.get("/{game_id}/files")
async def get_game_files(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """获取游戏文件内容（用于热重载恢复）。private 仅 owner；builtin 返回空。"""
    user_id = current_user.id if current_user else None
    row = await _check_files_access(db, game_id, user_id)
    if row.kind == "builtin":
        return {"game_id": game_id, "files": {}, "kind": "builtin"}

    game_dir = GAMES_DIR / game_id
    if not game_dir.is_dir():
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))

    # 大游戏目录可能含几十个 .gd / .tscn / .json 文件，每个 read_text 都是
    # 同步 IO；直接在 async handler 里循环读会阻塞 event loop 100~300ms，
    # 高并发热重载时其他请求被排队。整个目录扫 + 逐文件读全打到 to_thread
    # 后台线程，handler 等待期间 event loop 可以处理其他请求。
    files = await asyncio.to_thread(_read_game_text_files, game_dir)

    return {"game_id": game_id, "files": files}


def _read_game_text_files(game_dir: Path) -> dict[str, str]:
    """同步遍历 + 读取游戏目录里的源码 / 配置文本文件。在 ``to_thread`` 里跑。"""
    out: dict[str, str] = {}
    for f in game_dir.rglob("*"):
        if f.is_file() and f.suffix in (".gd", ".tscn", ".godot", ".cfg", ".tres", ".json"):
            rel = str(f.relative_to(game_dir))
            try:
                out[rel] = f.read_text(encoding="utf-8")
            except Exception:
                pass
    return out


# ─────────────────────────────────────────────────────────────────────
# Cover image — 创作期 iOS 自动截图 + 上传，所有玩家共享真实 cover
# ─────────────────────────────────────────────────────────────────────


@router.post("/{game_id}/cover")
async def upload_game_cover(
    game_id: str,
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner-only 上传 cover 图。**resize-only**（保持原比例，max width 1280）
    → PNG，写入 `games/{id}/cover.png`，并把更新后的 cover.png 拷进 zip（与
    modify_game 同款重打 zip 流程）让其他客户端下载游戏时一并拿到。"""
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.kind == "builtin":
        raise HTTPException(
            status_code=403, detail=_("GAME_BUILTIN_VISIBILITY_LOCKED")
        )
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    # content-type 校验
    if not (image.content_type or "").startswith("image/"):
        raise HTTPException(status_code=415, detail=_("GAME_COVER_INVALID_TYPE"))

    raw = await image.read()
    if len(raw) > _COVER_MAX_BYTES:
        raise HTTPException(status_code=413, detail=_("GAME_COVER_TOO_LARGE"))

    # PIL decode + crop + resize
    try:
        png_bytes = _normalize_cover_image(raw)
    except Exception as e:
        logger.warning("[Games] cover decode failed for %s: %s", game_id, e)
        raise HTTPException(status_code=400, detail=_("GAME_COVER_DECODE_FAILED"))

    # 写入 game dir
    game_dir = GAMES_DIR / game_id
    if not game_dir.is_dir():
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    cover_path = game_dir / "cover.png"
    cover_path.write_bytes(png_bytes)

    # 重打 zip — 让 download endpoint 下次发出的 zip 包含新 cover
    # 异步跑避免阻塞 FastAPI event loop（大游戏 zip 100-500ms）
    zip_path = GAMES_DIR / f"{game_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    await asyncio.to_thread(
        shutil.make_archive,
        str(zip_path.with_suffix("")), "zip", str(game_dir),
    )

    # cover_set_at 与 list 端点 _cover_set_at_for 同一来源（文件 mtime），
    # 避免上传响应里给 utcnow、list 给 mtime 的语义不一致。fallback 到 utcnow
    # 仅为防御性兜底（极罕见的 stat 失败场景）。
    cover_set_at = _cover_set_at_for(game_id) or int(game_service._utcnow().timestamp())
    logger.info(
        "[Games] cover uploaded: %s (%d bytes raw → %d bytes png), zip repacked",
        game_id, len(raw), len(png_bytes),
    )
    # cover 变化也是 explore / mine list 的视觉变化，invalidate cache 让卡片刷新
    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=[current_user.id], invalidate_explore=True,
    )
    return {
        "game_id": game_id,
        "cover_url": _cover_url_for(game_id),
        "cover_set_at": cover_set_at,
    }


# ─────────────────────────────────────────────────────────────────────
# Game assets — user-uploaded background images / sprites for the AI agent
# ─────────────────────────────────────────────────────────────────────


def _sanitize_asset_filename(raw_name: str) -> str:
    """Lower-case, strip path components, allow only `[a-z0-9_].<ext>`.

    Returns the safe filename. Caller is responsible for de-duping against
    existing files (see _next_unique_filename).
    """
    # Strip path components (anything before last / or \)
    name = raw_name.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if not name or name in {".", ".."}:
        return ""
    # Only keep ascii alphanumerics + underscore + single dot for ext
    # Replace anything else with underscore so e.g. 中文 becomes uniform _
    safe = _re.sub(r"[^a-z0-9._]+", "_", name)
    # collapse repeated underscores
    safe = _re.sub(r"_{2,}", "_", safe).strip("._")
    if "." not in safe:
        return ""
    stem, _, ext = safe.rpartition(".")
    if not stem:
        return ""
    if ext not in _ASSET_ALLOWED_EXTS:
        return ""
    candidate = f"{stem}.{ext}"
    if not _ASSET_FILENAME_SAFE_RE.match(candidate):
        return ""
    return candidate


def _next_unique_filename(assets_dir: Path, candidate: str, taken: set[str]) -> str:
    """Return a filename that doesn't collide with anything in `assets_dir`
    or `taken`. If `bg.png` exists, returns `bg_2.png`, `bg_3.png`, ..."""
    if candidate not in taken and not (assets_dir / candidate).exists():
        return candidate
    stem, _, ext = candidate.rpartition(".")
    n = 2
    while True:
        attempt = f"{stem}_{n}.{ext}"
        if attempt not in taken and not (assets_dir / attempt).exists():
            return attempt
        n += 1


@router.post("/{game_id}/assets")
async def upload_game_assets(
    game_id: str,
    images: list[UploadFile] = File(..., description="One or more PNG/JPEG/WebP files."),
    filenames: list[str] = Body(default_factory=list, description="Optional filename hints, must align with images list by index."),
    replace: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner-only — upload one or more user-supplied images as game assets
    (backgrounds, sprites). Files land in `games/{id}/assets/<safename>` and
    the game zip is repacked so subsequent downloads include them. The AI
    agent sees these via `assets/` enumeration in its modify_game cwd.

    Behavior:
    - Filenames are sanitized (lower-case, ASCII-only, allowed exts: png/jpg/jpeg/webp).
    - Collisions with existing or duplicate-batch names auto-suffix `_2`, `_3`, ...
    - Up to `_ASSET_MAX_BATCH` (10) files per request, `_ASSET_MAX_BYTES` (8MB) each.
    - Returns the canonical final filenames the iOS client should reference
      when constructing the AI prompt hint ("user uploaded res://assets/<name>").
    """
    if not images:
        raise HTTPException(status_code=400, detail=_("GAME_ASSETS_NONE_PROVIDED"))
    if len(images) > _ASSET_MAX_BATCH:
        raise HTTPException(status_code=400, detail=_("GAME_ASSETS_TOO_MANY"))

    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.kind == "builtin":
        raise HTTPException(
            status_code=403, detail=_("GAME_BUILTIN_VISIBILITY_LOCKED")
        )
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    game_dir = GAMES_DIR / game_id
    if not game_dir.is_dir():
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    assets_dir = game_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    final_names: list[str] = []
    taken_in_batch: set[str] = set()
    for idx, upload in enumerate(images):
        # Per-image content-type guard
        ctype = (upload.content_type or "").lower()
        if not ctype.startswith("image/"):
            raise HTTPException(status_code=415, detail=_("GAME_ASSETS_INVALID_TYPE"))

        raw = await upload.read()
        if len(raw) > _ASSET_MAX_BYTES:
            raise HTTPException(status_code=413, detail=_("GAME_ASSETS_TOO_LARGE"))

        # Decide candidate filename: prefer user-supplied hint at the same
        # index, fall back to the upload's filename, fall back to bg_N.png.
        hint = filenames[idx] if idx < len(filenames) else ""
        if not hint:
            hint = upload.filename or ""
        if not hint:
            # Default pattern when client provided nothing
            ext = "png" if "png" in ctype else ("jpg" if "jpeg" in ctype else "webp")
            hint = f"bg_{idx + 1}.{ext}"

        safe = _sanitize_asset_filename(hint)
        if not safe:
            # If sanitization stripped to empty, fall back to bg_<n>.<ext>
            ext = "png" if "png" in ctype else ("jpg" if "jpeg" in ctype else "webp")
            safe = _sanitize_asset_filename(f"bg_{idx + 1}.{ext}")
            if not safe:
                # Should never happen but guard anyway.
                raise HTTPException(status_code=400, detail=_("GAME_ASSETS_INVALID_FILENAME"))

        # Decode-validate via PIL (rejects malformed images that snuck past
        # content-type sniff). Use verify() — leaves bytes untouched on disk.
        try:
            with Image.open(io.BytesIO(raw)) as im:
                im.verify()
        except Exception:
            raise HTTPException(status_code=400, detail=_("GAME_ASSETS_DECODE_FAILED"))

        if replace:
            # 用 view tab 图片亮度调整后回写场景：直接用 sanitized 名覆盖。
            final_name = safe
        else:
            final_name = _next_unique_filename(assets_dir, safe, taken_in_batch)
        taken_in_batch.add(final_name)
        (assets_dir / final_name).write_bytes(raw)
        final_names.append(final_name)

    # Repack zip so download includes new assets (same convention as cover)
    zip_path = GAMES_DIR / f"{game_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    await asyncio.to_thread(
        shutil.make_archive,
        str(zip_path.with_suffix("")), "zip", str(game_dir),
    )

    # Bump updated_at so iOS auto-sync (refreshGameLists) detects staleness
    row.updated_at = game_service._utcnow()
    await db.commit()

    # Invalidate caches — game list cards may need to refresh
    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=[current_user.id], invalidate_explore=True,
    )

    logger.info(
        "[Games] %d assets uploaded to %s: %s",
        len(final_names), game_id, final_names,
    )
    return {"filenames": final_names}


# ─────────────────────────────────────────────────────────────────────
# View tab — list & serve game assets / list AI-used audio
# ─────────────────────────────────────────────────────────────────────


@router.get("/{game_id}/assets")
async def list_game_assets(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List image assets currently in `games/{id}/assets/` — owner-only.
    iOS view tab image panel uses this to show the grid of editable
    backgrounds + sprites. Returns filenames + sizes (for the client to
    decide layout / show "X MB" if needed)."""
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    assets_dir = GAMES_DIR / game_id / "assets"
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    files: list[dict] = []
    if assets_dir.is_dir():
        for p in sorted(assets_dir.iterdir()):
            if not p.is_file() or p.suffix.lower() not in image_exts:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            files.append({
                "filename": p.name,
                "size_bytes": size,
                "url": f"/api/games/{game_id}/assets/{p.name}",
            })
    return {"assets": files}


@router.get("/{game_id}/assets/{filename}")
async def get_game_asset(
    game_id: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Serve a single asset image binary — owner-only.
    Used by iOS view tab image panel to fetch full-resolution thumbnails."""
    # Sanitize filename — reject path traversal / odd chars
    if "/" in filename or "\\" in filename or ".." in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    fp = GAMES_DIR / game_id / "assets" / filename
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    ext = fp.suffix.lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return FileResponse(path=str(fp), media_type=media_type, filename=filename)


@router.get("/{game_id}/used_audio")
async def list_used_audio(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the BGM tracks AI actually included in this game (everything in
    `games/{id}/audio/*.ogg`). Cross-references the master BGM manifest to
    enrich each entry with category + description so iOS view tab sound panel
    can display human-readable labels.

    Owner-only — these tracks are part of the game's editable internals."""
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    audio_dir = GAMES_DIR / game_id / "audio"
    used: list[dict] = []
    if not audio_dir.is_dir():
        return {"audio": used}

    # Load master manifest once for cross-reference
    manifest_lookup: dict[str, dict] = {}
    if AUDIO_MANIFEST.exists():
        try:
            data = json.loads(AUDIO_MANIFEST.read_text(encoding="utf-8"))
            for sound in data.get("sounds", []):
                if sound.get("category") != "bgm":
                    continue
                # Map by both id (as filename stem) and explicit filename if present
                manifest_lookup[sound["id"]] = sound
        except Exception as e:
            logger.warning("[Games] used_audio: failed to parse manifest: %s", e)

    for p in sorted(audio_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".ogg":
            continue
        track_id = p.stem  # e.g., "8bit_loop"
        meta = manifest_lookup.get(track_id, {})
        # 优先用 BGM 主库的 m4a 预听（iOS AVPlayer 100% 能放）；落库以外的
        # SFX（event / action 等）用 fallback 直接 stream 游戏目录下的 ogg
        # 文件，AVPlayer 大部分能解码。
        if track_id in manifest_lookup:
            preview = f"/api/games/audio/bgm-preview/{track_id}"
        else:
            preview = f"/api/games/{game_id}/audio/{p.name}"
        used.append({
            "id": track_id,
            "filename": p.name,
            "description": meta.get("description", ""),
            "preview_url": preview,
        })
    return {"audio": used}


@router.get("/{game_id}/audio/{filename}")
async def get_game_audio_file(
    game_id: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stream a game's local audio file (ogg). Owner-only —— 这些都是 game
    内部资源，不公开。view tab sound panel 列出 SFX/BGM 时如果主库没 m4a
    预听就 fallback 到这里直接放原 ogg。"""
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    # 使用路径规范化校验文件名，防 URL-encoded %2e%2e 和符号链接穿越
    audio_dir = (GAMES_DIR / game_id / "audio").resolve()
    audio_path = (audio_dir / filename).resolve()
    if not str(audio_path).startswith(str(audio_dir) + "/"):
        raise HTTPException(status_code=400, detail="invalid filename")
    if not audio_path.exists() or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="audio file not found")
    return FileResponse(
        path=str(audio_path),
        media_type="audio/ogg",
        filename=filename,
    )


@router.post("/{game_id}/audio/upload")
async def upload_game_audio_file(
    game_id: str,
    audio: UploadFile = File(..., description="Audio file (ogg/mp3/m4a/wav/aac/aiff)"),
    filename: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner-only —— 用户上传自定义 BGM 文件到游戏自己的 audio/ 目录。
    与 audio/swap 区别：那个是从主库拷贝，这个是客户端 multipart 上传 raw 文件。
    用法：iOS 在 game_dir_ready 后把本地 custom_bgm/ 选中的文件传上来，AI 写
    AudioManager.play_bgm("<basename without ext>")，运行时加载这个上传的文件。"""
    target_name = (filename or audio.filename or "").strip()
    if not target_name:
        raise HTTPException(status_code=400, detail="no filename")
    # 在 final target_name 上做 path traversal 检查 —— 不论 filename 是 query
    # 来的还是从 multipart audio.filename 兜底，都过同一个安全网。
    if "/" in target_name or "\\" in target_name or ".." in target_name:
        raise HTTPException(status_code=400, detail="invalid filename")

    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    raw = await audio.read()
    if len(raw) > 8 * 1024 * 1024:  # 8MB
        raise HTTPException(status_code=413, detail="audio file too large")
    ext = Path(target_name).suffix.lower()
    if ext not in {".ogg", ".mp3", ".m4a", ".wav", ".aac", ".aiff"}:
        raise HTTPException(status_code=415, detail="unsupported audio format")

    audio_dir = GAMES_DIR / game_id / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    target = audio_dir / target_name
    target.write_bytes(raw)
    logger.info("[Games] custom audio uploaded: game=%s file=%s (%d bytes)",
                game_id, target_name, len(raw))

    # Repack zip — 走线程池避免阻塞 event loop
    zip_path = GAMES_DIR / f"{game_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    await asyncio.to_thread(
        shutil.make_archive,
        str(zip_path.with_suffix("")), "zip", str(GAMES_DIR / game_id),
    )

    return {"status": "ok", "filename": target_name}


@router.post("/{game_id}/audio/swap")
async def swap_game_audio(
    game_id: str,
    old_filename: str,
    new_track_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner-only —— 用主库的某个 track 替换游戏自己 audio/ 目录里的某个文件。
    **不动主库**，只覆盖该游戏自己 audio/<old_filename> 的字节。GDScript 里
    `load("res://audio/<old_filename>")` 等引用全部不用改，AI 也不用介入。

    Query 参数：
      - old_filename: 游戏 audio/ 目录下要被替换的文件名（如 score_combo.ogg）
      - new_track_id: 主库 manifest 里的 track id（如 bgm_8bit_chill）
    """
    if "/" in old_filename or "\\" in old_filename or ".." in old_filename:
        raise HTTPException(status_code=400, detail="invalid old_filename")
    if "/" in new_track_id or "\\" in new_track_id or ".." in new_track_id:
        raise HTTPException(status_code=400, detail="invalid new_track_id")

    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    audio_dir = GAMES_DIR / game_id / "audio"
    if not audio_dir.is_dir():
        raise HTTPException(status_code=404, detail="game audio dir not found")
    dst_path = audio_dir / old_filename

    # iOS 端 `BGMSelection.none = "_none"` 是"无背景音乐"哨兵；view tab BGM
    # 选曲面板里"BGM_NONE"行也用同一个 id 提交 → 这里把 `_none` 当作"删除该
    # 音频文件"处理。skeleton audio_manager.gd._load_stream 找不到文件时
    # silently no-op，运行时不会崩。
    if new_track_id == "_none":
        if dst_path.exists():
            dst_path.unlink()
            logger.info("[Games] audio swap: game=%s removed %s (none)", game_id, old_filename)
        else:
            logger.info("[Games] audio swap: game=%s %s already absent (none)", game_id, old_filename)
    else:
        if not AUDIO_MANIFEST.exists():
            raise HTTPException(status_code=500, detail="audio manifest missing")
        manifest = json.loads(AUDIO_MANIFEST.read_text(encoding="utf-8"))
        new_sound = next(
            (s for s in manifest.get("sounds", []) if s.get("id") == new_track_id),
            None,
        )
        if new_sound is None:
            raise HTTPException(status_code=404, detail=f"track {new_track_id!r} not in library")
        src_path = AUDIO_LIBRARY_DIR / new_sound["file"]
        if not src_path.exists():
            raise HTTPException(status_code=500, detail=f"library file missing: {src_path.name}")

        # 复制覆盖（原文件名不变 → GDScript 不用改）
        shutil.copyfile(src_path, dst_path)
        logger.info(
            "[Games] audio swap: game=%s %s ← library/%s",
            game_id, old_filename, new_sound["file"],
        )

    # 重打 zip 让下次 download 拿到新内容 — 走线程池避免阻塞 event loop
    zip_path = GAMES_DIR / f"{game_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    await asyncio.to_thread(
        shutil.make_archive,
        str(zip_path.with_suffix("")), "zip", str(GAMES_DIR / game_id),
    )

    # 刷新 modified_at 让 iOS 端 cover/内容自动同步路径感知到变更
    try:
        meta_path = GAMES_DIR / game_id / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["modified_at"] = int(time.time())
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("[Games] audio swap: meta refresh failed: %s", e)

    return {"status": "ok", "old_filename": old_filename, "new_track_id": new_track_id}


@router.get("/{game_id}/cover")
async def get_game_cover(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """公开下载 cover.png（走 visibility 校验：private 仅 owner 可看）。"""
    user_id = current_user.id if current_user else None
    # builtin 不存 cover.png（直接 404）；private 仅 owner
    row = await _check_files_access(db, game_id, user_id)
    # ai_games 行不存在（老 ai_* 没回灌 / 已删除）→ 视为 404，不要让 cover
    # 在没 visibility 校验的情况下被裸下载（_check_files_access 返 None 时
    # 没做 visibility 检查；这里补 404 闭合）。
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    cover_path = GAMES_DIR / game_id / "cover.png"
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    # ETag 用 mtime，让 iOS / 浏览器复用缓存；cover 变化时 mtime 变会自动 bust
    mtime = int(cover_path.stat().st_mtime)
    etag = f'W/"cover-{game_id}-{mtime}"'
    return FileResponse(
        path=str(cover_path),
        media_type="image/png",
        filename=f"{game_id}_cover.png",
        headers={
            "Cache-Control": "public, max-age=300",
            "ETag": etag,
        },
    )


# ─────────────────────────────────────────────────────────────────────
# Like / Favorite / Share
# ─────────────────────────────────────────────────────────────────────


@router.get("/{game_id}/stats")
async def get_game_stats_endpoint(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """读取一个游戏的 share / like 计数。Auth 可选，登录时附带 liked_by_me。"""
    row = await _ensure_ai_game_row(db, game_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    share_count = row.share_count
    like_count = row.like_count

    payload = {"share_count": share_count, "like_count": like_count}
    if current_user is not None:
        liked = (
            await db.execute(
                select(GameLike).where(
                    GameLike.user_id == current_user.id,
                    GameLike.game_id == game_id,
                )
            )
        ).scalar_one_or_none()
        payload["liked_by_me"] = liked is not None
    return payload


@router.post("/{game_id}/share")
async def record_share_endpoint(game_id: str, db: AsyncSession = Depends(get_db)):
    """匿名 +1 分享计数（用户在 iOS 端确认成功分享后调用）。双写 game_stats。"""
    row = await _ensure_ai_game_row(db, game_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))

    new_count = await game_service.record_share(db, game_id)
    await db.commit()
    await game_service.invalidate_game_caches(game_id, invalidate_explore=True)
    return {"share_count": new_count}


@router.post("/{game_id}/like")
async def toggle_like_endpoint(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """toggle 当前用户对该游戏的点赞。双写 ai_games + game_stats。

    若 ai_games 行不存在但 fs 上有，自动懒回灌（兼容老 ai_* 未迁移）。"""
    row = await _ensure_ai_game_row(db, game_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))

    liked, like_count = await game_service.toggle_like(
        db, current_user.id, game_id
    )
    await db.commit()
    affected = [current_user.id]
    if row.owner_id and row.owner_id != current_user.id:
        affected.append(row.owner_id)
    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=affected, invalidate_explore=True,
    )
    return {"liked_by_me": liked, "like_count": like_count}


@router.post("/{game_id}/favorite")
async def toggle_favorite_endpoint(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """toggle 收藏。允许 owner 收藏自己的游戏 — 走 ai_games.owner_favorited
    列；非 owner 走 user_game_view view_type='favorited'。Service 层
    add_favorite / remove_favorite 内部分流。"""
    row = await _ensure_ai_game_row(db, game_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))

    is_owner = (row.owner_id == current_user.id)
    # 是否当前已 favorited（取决于 owner / non-owner 路径）
    if is_owner:
        is_favorited = row.owner_favorited
    else:
        existing = (
            await db.execute(
                select(UserGameView).where(
                    UserGameView.user_id == current_user.id,
                    UserGameView.game_id == game_id,
                    UserGameView.view_type == "favorited",
                )
            )
        ).scalar_one_or_none()
        is_favorited = existing is not None

    if is_favorited:
        _changed, fav_count = await game_service.remove_favorite(
            db, current_user.id, game_id
        )
        favorited = False
    else:
        _changed, fav_count = await game_service.add_favorite(
            db, current_user.id, game_id
        )
        favorited = True
    await db.commit()

    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=[current_user.id], invalidate_explore=True,
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


_TITLE_MAX_LEN = 40


@router.patch("/{game_id}/title")
async def update_game_title(
    game_id: str,
    body: TitleBody = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Owner-only 改名。用户在 publish sheet 输入任一语言；后端补另一种
    语言写进 `title_i18n`，保证列表展示在 en / zh-Hans 两种 app 语言下
    都有像样的标题。
    """
    new_title = (body.title or "").strip()
    if not new_title:
        raise HTTPException(status_code=400, detail=_("GAME_TITLE_EMPTY"))
    if len(new_title) > _TITLE_MAX_LEN:
        raise HTTPException(status_code=400, detail=_("GAME_TITLE_TOO_LONG"))

    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.kind == "builtin":
        raise HTTPException(
            status_code=403, detail=_("GAME_BUILTIN_VISIBILITY_LOCKED")
        )
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    from robot.agent.title_translator import translate_title
    title_i18n = await translate_title(new_title)

    try:
        await game_service.update_title(
            db, game_id, title_i18n=title_i18n, primary_title=new_title
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    await db.commit()
    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=[current_user.id], invalidate_explore=True,
    )
    return {
        "game_id": game_id,
        "title": new_title,
        "title_i18n": title_i18n,
    }


@router.patch("/{game_id}/visibility")
async def update_visibility(
    game_id: str,
    body: VisibilityBody = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.visibility not in _VALID_VISIBILITY:
        raise HTTPException(status_code=400, detail=_("GAME_VISIBILITY_INVALID"))

    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
    if row.kind == "builtin":
        raise HTTPException(
            status_code=403, detail=_("GAME_BUILTIN_VISIBILITY_LOCKED")
        )
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    try:
        await game_service.set_visibility(db, game_id, body.visibility)
    except ValueError:
        raise HTTPException(status_code=400, detail=_("GAME_VISIBILITY_INVALID"))
    await db.commit()
    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=[current_user.id], invalidate_explore=True,
    )
    return {"game_id": game_id, "visibility": body.visibility}


# ─────────────────────────────────────────────────────────────────────
# Remix
# ─────────────────────────────────────────────────────────────────────


@router.post("/{game_id}/remix")
async def remix_game_endpoint(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """clone game Godot 工程 + 元数据，原作 remix_count++，新副本 visibility=private。"""
    logger.info(
        "[RemixDiag] endpoint start source_game_id=%s requester=%s",
        game_id, current_user.id,
    )
    try:
        new_row = await game_service.clone_game(
            db, source_game_id=game_id, new_owner_id=current_user.id,
        )
    except game_service.RemixError as e:
        logger.warning(
            "[RemixDiag] endpoint rejected source_game_id=%s requester=%s code=%s status=%s",
            game_id, current_user.id, e.code, e.http_status,
        )
        raise HTTPException(status_code=e.http_status, detail=_(e.code))

    await db.commit()
    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=[current_user.id], invalidate_explore=True,
    )
    await game_service.invalidate_game_caches(
        new_row.game_id, affected_user_ids=[current_user.id],
        invalidate_explore=False,  # 新行默认 private 不影响 explore
    )

    logger.info(
        "[RemixDiag] endpoint success source_game_id=%s requester=%s new_game_id=%s "
        "original_game_id=%s visibility=%s owner_id=%s input_mode=%s",
        game_id, current_user.id, new_row.game_id, new_row.original_game_id,
        new_row.visibility, new_row.owner_id, new_row.input_mode,
    )
    return {
        "game_id": new_row.game_id,
        "original_game_id": new_row.original_game_id,
        "owner_id": new_row.owner_id,
        "visibility": new_row.visibility,
        "input_mode": new_row.input_mode,
    }


# ─────────────────────────────────────────────────────────────────────
# Delete
# ─────────────────────────────────────────────────────────────────────


@router.delete("/{game_id}")
async def delete_game(
    game_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """owner-only 删除。FK CASCADE 清掉 user_game_view / explore_game_view / game_likes。"""
    row = (
        await db.execute(select(AIGame).where(AIGame.game_id == game_id))
    ).scalar_one_or_none()
    if row is None:
        # 兼容期：老 ai_* 没回灌行，按 fs 兜底删
        if not _game_dir_exists(game_id):
            raise HTTPException(status_code=404, detail=_("GAME_NOT_FOUND"))
        # 安全起见，未回灌行不允许通过此路径删除（避免误删别人的）
        raise HTTPException(status_code=409, detail=_("GAME_OPERATION_ERROR", e="legacy row not migrated"))
    if row.kind == "builtin":
        raise HTTPException(status_code=403, detail=_("GAME_BUILTIN_NOT_DELETABLE"))
    if row.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail=_("GAME_NOT_OWNED"))

    # 大目录 rmtree 也是阻塞 IO，丢线程池。同时清掉 sibling
    # `<game_id>.claude/`（per-game session 存储），否则会 leak 用户的
    # Claude Code 会话 jsonl 在磁盘上。
    from robot.games._session_paths import wipe_game_artifacts
    await asyncio.to_thread(wipe_game_artifacts, GAMES_DIR / game_id)

    # 老 game_stats / game_likes / user_game_view 显式清（这三张表的 game_id
    # 都是裸 String，不是 FK，没 CASCADE）。explore_game_view 的 game_id 是
    # FK ondelete=CASCADE，被 db.delete(row) 自动连带清掉，不用显式 delete。
    #
    # user_game_view 必须清的原因：作者删自己游戏时，其它用户对它的 favorite
    # 关系（view_type='favorited'）会变成 orphan —— 虽然 mine endpoint JOIN
    # 后用户看不到，但 favorite_count 等统计会偏。同样作者自己的 'created' 行
    # 也得清干净。
    await db.execute(delete(GameStats).where(GameStats.game_id == game_id))
    await db.execute(delete(GameLike).where(GameLike.game_id == game_id))
    affected_users = (await db.execute(
        select(UserGameView.user_id).where(UserGameView.game_id == game_id).distinct()
    )).scalars().all()
    await db.execute(delete(UserGameView).where(UserGameView.game_id == game_id))

    await db.delete(row)
    await db.commit()
    # 所有 favorite 过该游戏的用户的 mine cache 都得清，否则他们的 mine 还
    # 显示这条 orphan（直到 60s TTL 过期或他们主动写操作触发其它 invalidate）
    invalidate_users = list(set(list(affected_users) + [current_user.id]))
    await game_service.invalidate_game_caches(
        game_id, affected_user_ids=invalidate_users, invalidate_explore=True,
    )

    logger.info("[Games] deleted game: %s (owner=%s)", game_id, current_user.id)
    return {"status": "deleted", "game_id": game_id}
