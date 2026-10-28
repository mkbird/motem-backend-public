"""add games marketplace tables (ai_games + view tables) + builtin seeds

Revision ID: h8i9j0k1l2m3
Revises: a7b8c9d0e1f2
Create Date: 2026-04-27 12:00:00.000000

把 games 升级为完整市场后端（与 widget marketplace 对照）：
- 新建 ai_games 主表（v1 期间游戏元数据全在 fs 的 meta.json，DB 没有主表，
  现在统一进 DB 同时保留 fs 双写期）
- 新建 user_game_view（per-user "我的"）+ explore_game_view（全局 "探索"）
- 老 game_likes 加 user_id 索引（已 PK 但单字段索引便于"列出 X 用户的所有 like"）
- 数据迁移：
  * 扫描 games/ai_*/meta.json 全部目录，每条插入 ai_games 行 + user_game_view
    (owner, id, 'created')，从对应 game_stats 行回灌 share_count/like_count
  * 种子 5 个内置游戏（builtin_shooter/balance/pinball/meeting/pickpocket），
    `kind='builtin'`、`owner_id=NULL`、`visibility='public_view_only'`、
    `input_mode='body_pose'`，同时写 explore_game_view
- 旧 game_stats / game_likes 表保留（双写期），下个 release 后再 drop

新代码读路径优先 ai_games 列；写路径同时更新 game_stats 列保持兼容。
"""
from datetime import datetime
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# i18n title/description 与 iOS motem/Localizable.xcstrings 中
# 既有 key 文案对齐。input_mode='body_pose' 与 iOS GamesHubView 现有元数据一致。
BUILTIN_SEEDS = [
    {
        "game_id": "builtin_shooter",
        "title": "Space Shooter",
        "title_i18n": {"en": "Space Shooter", "zh-Hans": "太空射手"},
        "description_i18n": {
            "en": "Shoot work enemies, survive waves",
            "zh-Hans": "射击工作敌人，撑过一波又一波",
        },
        "icon": "🚀",
        "input_mode": "body_pose",
    },
    {
        "game_id": "builtin_balance",
        "title": "Balance Master",
        "title_i18n": {"en": "Balance Master", "zh-Hans": "平衡大师"},
        "description_i18n": {
            "en": "Tilt to collect coins, dodge bombs",
            "zh-Hans": "倾斜身体收集金币，躲开炸弹",
        },
        "icon": "⚖️",
        "input_mode": "body_pose",
    },
    {
        "game_id": "builtin_pinball",
        "title": "Pinball",
        "title_i18n": {"en": "Pinball", "zh-Hans": "弹球达人"},
        "description_i18n": {
            "en": "Classic breakout with face-controlled paddle",
            "zh-Hans": "用面部控制挡板的经典打砖块",
        },
        "icon": "🎯",
        "input_mode": "body_pose",
    },
    {
        "game_id": "builtin_meeting",
        "title": "Meeting Survivor",
        "title_i18n": {"en": "Meeting Survivor", "zh-Hans": "会议生存者"},
        "description_i18n": {
            "en": "Bullet-hell survival in endless meetings",
            "zh-Hans": "无尽会议中的弹幕生存战",
        },
        "icon": "💼",
        "input_mode": "body_pose",
    },
    # NB: builtin_pickpocket（H5 小游戏）从原 5 个里移除 —— phonestand 模式
    # 用的 ActiveRobotGame enum 没有 pickPocket case，加进来 explore 列表
    # 用户点了无法 launch。它的老入口在 deprecated 的 PageContainerView
    # 路径里，已不维护。要恢复需要 phonestand 的 H5 容器支持。
]


def upgrade() -> None:
    # ── 1. 主表 ai_games ──
    op.create_table(
        "ai_games",
        sa.Column("game_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("title_i18n", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("description_i18n", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(length=64), nullable=False, server_default="🎮"),
        sa.Column(
            "input_mode", sa.String(length=16),
            nullable=False, server_default="touch",
        ),
        sa.Column(
            "kind", sa.String(length=16),
            nullable=False, server_default="ai",
        ),
        sa.Column(
            "visibility", sa.String(length=24),
            nullable=False, server_default="private",
        ),
        sa.Column("original_game_id", sa.String(length=64), nullable=True),
        sa.Column(
            "template_from", sa.String(length=64),
            nullable=False, server_default="",
        ),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("favorite_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("share_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remix_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], ondelete="CASCADE",
            name="fk_ai_games_owner",
        ),
    )
    op.create_index("ix_ai_games_owner_id", "ai_games", ["owner_id"])
    op.create_index("ix_ai_games_visibility", "ai_games", ["visibility"])
    op.create_index(
        "ix_ai_games_original_game_id", "ai_games", ["original_game_id"]
    )

    # ── 2. 视图表 ──
    op.create_table(
        "user_game_view",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=64), nullable=False),
        sa.Column("view_type", sa.String(length=16), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "game_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_user_game_view_user",
        ),
    )
    op.create_index("ix_user_game_view_game_id", "user_game_view", ["game_id"])

    op.create_table(
        "explore_game_view",
        sa.Column("game_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "sort_score", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"], ["ai_games.game_id"], ondelete="CASCADE",
            name="fk_explore_game_view_game",
        ),
    )

    # game_likes 加 user_id 单字段索引（已经在 PK 内但单独索引便于反向查）
    # 注意：MySQL 在 PK 第一列上自动有索引，user_id 在 PK 第二列也会被覆盖，
    # 这里加显式 idx 主要是为了 "select where user_id = ?" 路径
    op.create_index("ix_game_likes_user_id", "game_likes", ["user_id"])

    # ── 3. 数据迁移 ──
    bind = op.get_bind()
    now = datetime.utcnow()
    now_str = now.replace(microsecond=0).isoformat(sep=" ")

    # 3a. 扫描 games/ai_* 目录回灌 ai_games 行
    import json as _json

    games_dir = Path(__file__).resolve().parent.parent.parent / "games"
    if games_dir.is_dir():
        for d in sorted(games_dir.iterdir()):
            if not d.is_dir() or not d.name.startswith("ai_"):
                continue
            meta_file = d / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = _json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            owner_id = meta.get("owner_id") or None
            title = meta.get("title", d.name)
            title_i18n = meta.get("title_i18n") or {"en": title, "zh-Hans": title}
            description = meta.get("description") or ""
            desc_i18n = meta.get("description_i18n") or {
                "en": description if description else "",
                "zh-Hans": description if description else "",
            }
            icon = meta.get("icon") or "🎮"
            input_mode = meta.get("input_mode") or "touch"
            template_from = meta.get("template_from") or ""
            session_id = meta.get("session_id") or None

            # 老的 share/like 计数从 game_stats 回灌
            stats_row = bind.execute(
                sa.text(
                    "SELECT share_count, like_count FROM game_stats "
                    "WHERE game_id = :gid"
                ),
                {"gid": d.name},
            ).fetchone()
            share_count = stats_row.share_count if stats_row else 0
            like_count = stats_row.like_count if stats_row else 0

            bind.execute(
                sa.text(
                    """
                    INSERT INTO ai_games (
                        game_id, owner_id,
                        title, title_i18n, description, description_i18n,
                        icon, input_mode,
                        kind, visibility, original_game_id, template_from, session_id,
                        like_count, favorite_count, share_count, remix_count,
                        created_at, updated_at
                    ) VALUES (
                        :gid, :owner_id,
                        :title, :title_i18n, :description, :description_i18n,
                        :icon, :input_mode,
                        'ai', 'private', NULL, :template_from, :session_id,
                        :like_count, 0, :share_count, 0,
                        :now, :now
                    )
                    """
                ),
                {
                    "gid": d.name,
                    "owner_id": owner_id,
                    "title": title,
                    "title_i18n": _json.dumps(title_i18n, ensure_ascii=False),
                    "description": description,
                    "description_i18n": _json.dumps(desc_i18n, ensure_ascii=False),
                    "icon": icon,
                    "input_mode": input_mode,
                    "template_from": template_from,
                    "session_id": session_id,
                    "like_count": like_count,
                    "share_count": share_count,
                    "now": now_str,
                },
            )

            # 同步 user_game_view (owner, id, 'created')
            if owner_id:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO user_game_view
                            (user_id, game_id, view_type, added_at)
                        VALUES (:uid, :gid, 'created', :now)
                        """
                    ),
                    {"uid": owner_id, "gid": d.name, "now": now_str},
                )

    # 3b. 种子 5 个内置 → ai_games + explore_game_view
    for seed in BUILTIN_SEEDS:
        bind.execute(
            sa.text(
                """
                INSERT INTO ai_games (
                    game_id, owner_id,
                    title, title_i18n, description, description_i18n,
                    icon, input_mode,
                    kind, visibility, original_game_id, template_from, session_id,
                    like_count, favorite_count, share_count, remix_count,
                    created_at, updated_at
                ) VALUES (
                    :gid, NULL,
                    :title, :title_i18n, :description, :description_i18n,
                    :icon, :input_mode,
                    'builtin', 'public_view_only', NULL, '', NULL,
                    0, 0, 0, 0,
                    :now, :now
                )
                """
            ),
            {
                "gid": seed["game_id"],
                "title": seed["title"],
                "title_i18n": _json.dumps(seed["title_i18n"], ensure_ascii=False),
                "description": seed["description_i18n"]["en"],
                "description_i18n": _json.dumps(
                    seed["description_i18n"], ensure_ascii=False
                ),
                "icon": seed["icon"],
                "input_mode": seed["input_mode"],
                "now": now_str,
            },
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO explore_game_view (game_id, sort_score, added_at)
                VALUES (:gid, 0, :now)
                """
            ),
            {"gid": seed["game_id"], "now": now_str},
        )


def downgrade() -> None:
    # 内置种子先删（避免 ai_games 表 drop 时 FK CASCADE 把 owner=NULL 行连带）
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM ai_games WHERE kind = 'builtin'"))

    op.drop_index("ix_game_likes_user_id", table_name="game_likes")
    op.drop_table("explore_game_view")
    op.drop_index("ix_user_game_view_game_id", table_name="user_game_view")
    op.drop_table("user_game_view")
    op.drop_index("ix_ai_games_original_game_id", table_name="ai_games")
    op.drop_index("ix_ai_games_visibility", table_name="ai_games")
    op.drop_index("ix_ai_games_owner_id", table_name="ai_games")
    op.drop_table("ai_games")
