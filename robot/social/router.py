"""社交功能 API —— 关注 / 粉丝 / 用户 profile 查询。

iOS 端 ProfileView (other-view) / FollowListView 调用入口。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user
from robot.auth.models import (
    FollowListResponse,
    FollowToggleResponse,
    OtherProfileResponse,
    UserMiniResponse,
)
from robot.db.database import get_db
from robot.db.models import AIGame, GameLike, User, UserFollow, UserGameView
from robot.games.router import _build_owner_info, _enrich_for_user, _user_flag_sets
from robot.games import service as game_service
from robot.i18n import _

router = APIRouter(prefix="/api/users", tags=["social"])


async def _resolve_user(db: AsyncSession, id_or_handle: str) -> User:
    """按 id 或 username 找用户。两个都试，找不到 404。"""
    q = select(User).where(or_(User.id == id_or_handle, User.username == id_or_handle))
    user = (await db.execute(q)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=_("USER_NOT_FOUND"))
    return user


async def _follow_counts(db: AsyncSession, user_id: str) -> tuple[int, int]:
    """(followers_count, following_count)"""
    f1 = select(func.count(UserFollow.id)).where(UserFollow.followee_id == user_id)
    f2 = select(func.count(UserFollow.id)).where(UserFollow.follower_id == user_id)
    return (
        (await db.execute(f1)).scalar() or 0,
        (await db.execute(f2)).scalar() or 0,
    )


async def _is_following(db: AsyncSession, follower_id: str, followee_id: str) -> bool:
    if follower_id == followee_id:
        return False
    q = select(UserFollow.id).where(
        UserFollow.follower_id == follower_id,
        UserFollow.followee_id == followee_id,
    )
    return (await db.execute(q)).first() is not None


@router.get("/{id_or_handle}", response_model=OtherProfileResponse)
async def get_user_profile(
    id_or_handle: str,
    me: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    target = await _resolve_user(db, id_or_handle)
    followers_count, following_count = await _follow_counts(db, target.id)
    is_following = await _is_following(db, me.id, target.id)
    follows_me = await _is_following(db, target.id, me.id)
    return OtherProfileResponse(
        id=target.id,
        username=target.username,
        display_name=target.display_name,
        avatar_url=target.avatar_url,
        bio=target.bio,
        followers_count=followers_count,
        following_count=following_count,
        is_following=is_following,
        follows_me=follows_me,
    )


@router.get("/{id_or_handle}/games")
async def list_user_profile_games(
    id_or_handle: str,
    tab: str = Query("created", pattern="^(created|liked|favorited)$"),
    me: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Profile 三个子 tab 的游戏列表。

    - 自己看自己：created / liked / favorited 都可见，含 private。
    - 看别人：只返回 created / liked 中公开游戏；favorited 属于个人书签，返回空。
    返回结构复用 `/api/games/*` 的 `{games: [...]}`，iOS 可直接用同一张 GameCard。
    """
    target = await _resolve_user(db, id_or_handle)
    is_self = target.id == me.id

    if tab == "favorited" and not is_self:
        return {"games": []}

    public_visibilities = ("public_remixable", "public_view_only")
    if tab == "created":
        stmt = select(AIGame).where(AIGame.owner_id == target.id)
        if not is_self:
            stmt = stmt.where(AIGame.visibility.in_(public_visibilities))
        stmt = stmt.order_by(AIGame.created_at.desc())
        rows = (await db.execute(stmt.limit(200))).scalars().all()
        raw_list = [game_service._row_to_dict(r) for r in rows]
        for raw in raw_list:
            raw["view_type"] = "created" if raw.get("owner_id") == me.id else None
    elif tab == "liked":
        stmt = (
            select(AIGame)
            .join(GameLike, GameLike.game_id == AIGame.game_id)
            .where(GameLike.user_id == target.id)
        )
        if not is_self:
            stmt = stmt.where(AIGame.visibility.in_(public_visibilities))
        stmt = stmt.order_by(GameLike.created_at.desc())
        rows = (await db.execute(stmt.limit(200))).scalars().all()
        raw_list = [game_service._row_to_dict(r) for r in rows]
    else:
        favorited_rows = (
            await db.execute(
                select(AIGame, UserGameView.added_at)
                .join(UserGameView, UserGameView.game_id == AIGame.game_id)
                .where(
                    UserGameView.user_id == target.id,
                    UserGameView.view_type == "favorited",
                )
                .order_by(UserGameView.added_at.desc())
                .limit(200)
            )
        ).all()
        owned_favorited_rows = (
            await db.execute(
                select(AIGame)
                .where(AIGame.owner_id == target.id, AIGame.owner_favorited.is_(True))
                .order_by(AIGame.updated_at.desc())
                .limit(200)
            )
        ).scalars().all()

        raw_list = []
        seen: set[str] = set()
        for row, _added_at in favorited_rows:
            raw = game_service._row_to_dict(row)
            raw["view_type"] = "favorited"
            raw_list.append(raw)
            seen.add(row.game_id)
        for row in owned_favorited_rows:
            if row.game_id in seen:
                continue
            raw = game_service._row_to_dict(row)
            raw["view_type"] = "created"
            raw_list.append(raw)

    game_ids = [r["game_id"] for r in raw_list]
    liked, favorited, created = await _user_flag_sets(db, me.id, game_ids)
    owner_info = await _build_owner_info(db, raw_list)
    return {
        "games": [
            _enrich_for_user(
                r,
                current_user_id=me.id,
                liked_set=liked,
                favorited_set=favorited,
                created_set=created,
                owner_info=owner_info,
            )
            for r in raw_list
        ]
    }


async def _list_relations(
    db: AsyncSession,
    me: User,
    target_id: str,
    *,
    role: str,                  # "followers" or "following"
    cursor: str | None,
    limit: int,
) -> FollowListResponse:
    """共享的分页查询。cursor 是 UserFollow.id（lexicographic uuid 用作 keyset）。"""
    if role == "followers":
        # 找 followee_id == target_id 的所有 follow，关联 follower 用户行
        q = (
            select(UserFollow.id, User)
            .join(User, User.id == UserFollow.follower_id)
            .where(UserFollow.followee_id == target_id)
        )
    else:
        q = (
            select(UserFollow.id, User)
            .join(User, User.id == UserFollow.followee_id)
            .where(UserFollow.follower_id == target_id)
        )
    if cursor:
        q = q.where(UserFollow.id > cursor)
    q = q.order_by(UserFollow.id.asc()).limit(limit + 1)
    rows = (await db.execute(q)).all()

    next_cursor: str | None = None
    if len(rows) > limit:
        next_cursor = rows[limit - 1][0]   # last id of returned page
        rows = rows[:limit]

    items: list[UserMiniResponse] = []
    for follow_id, user in rows:
        is_following = await _is_following(db, me.id, user.id)
        items.append(UserMiniResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            is_following=is_following,
        ))
    return FollowListResponse(items=items, next_cursor=next_cursor)


@router.get("/{id_or_handle}/followers", response_model=FollowListResponse)
async def list_followers(
    id_or_handle: str,
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    me: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    target = await _resolve_user(db, id_or_handle)
    return await _list_relations(db, me, target.id, role="followers", cursor=cursor, limit=limit)


@router.get("/{id_or_handle}/following", response_model=FollowListResponse)
async def list_following(
    id_or_handle: str,
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    me: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    target = await _resolve_user(db, id_or_handle)
    return await _list_relations(db, me, target.id, role="following", cursor=cursor, limit=limit)


@router.post("/{id_or_handle}/follow", response_model=FollowToggleResponse)
async def follow_user(
    id_or_handle: str,
    me: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    target = await _resolve_user(db, id_or_handle)
    if target.id == me.id:
        raise HTTPException(status_code=400, detail=_("CANNOT_FOLLOW_SELF"))
    # 幂等：已关注就不再插入
    existing = await db.execute(
        select(UserFollow.id).where(
            UserFollow.follower_id == me.id, UserFollow.followee_id == target.id
        )
    )
    if existing.first() is None:
        db.add(UserFollow(follower_id=me.id, followee_id=target.id))
        await db.commit()
    followers_count, _following = await _follow_counts(db, target.id)
    return FollowToggleResponse(is_following=True, followers_count=followers_count)


@router.delete("/{id_or_handle}/follow", response_model=FollowToggleResponse)
async def unfollow_user(
    id_or_handle: str,
    me: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    target = await _resolve_user(db, id_or_handle)
    row = await db.execute(
        select(UserFollow).where(
            UserFollow.follower_id == me.id, UserFollow.followee_id == target.id
        )
    )
    f = row.scalar_one_or_none()
    if f is not None:
        await db.delete(f)
        await db.commit()
    followers_count, _following = await _follow_counts(db, target.id)
    return FollowToggleResponse(is_following=False, followers_count=followers_count)
