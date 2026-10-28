#!/usr/bin/env python3
"""Backfill `input_mode` into game meta.json from the authoritative DB.

Background (历史 P0):
    Fast path 的 `compiler.py` 写 meta.json 时漏了 `input_mode`，而
    `finalize.py` 把真实值（archetype 一律 `body_pose`）写进了
    `ai_games.input_mode`。结果同一个游戏 DB 与磁盘 meta 分叉：DB =
    body_pose，meta.json = 缺失 / `touch`（被老 modify 路径按默认值回写）。
    端上玩游戏时读 meta 判定输入方式 → 体感游戏摄像头开不起来。

    compiler 已修复（新游戏不再分叉）。本脚本修复存量游戏：以 DB
    `ai_games.input_mode` 为权威，回灌每个 `games/<id>/meta.json` 的
    `input_mode`，并 bump `modified_at`（= `content_modified_at`，让 iOS /
    macOS 感知变更重新下载），重打 zip 让 `/download` 立即一致。

幂等：meta 已与 DB 一致的游戏跳过。

用法：
    conda activate robot
    python scripts/backfill_game_input_mode.py --dry-run   # 只报告，不写
    python scripts/backfill_game_input_mode.py             # 实际修复
    python scripts/backfill_game_input_mode.py --only-fastpath  # 仅 provenance=fastpath
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
GAMES_DIR = ROOT_DIR / "games"
_VALID_MODES = {"touch", "body_pose"}

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


async def _load_db_input_modes() -> dict[str, str]:
    """game_id -> ai_games.input_mode（仅保留合法值）。"""
    from sqlalchemy import select

    from robot.db.database import async_session
    from robot.db.models import AIGame

    out: dict[str, str] = {}
    async with async_session() as session:
        rows = (await session.execute(select(AIGame.game_id, AIGame.input_mode))).all()
    for game_id, input_mode in rows:
        if input_mode in _VALID_MODES:
            out[game_id] = input_mode
    return out


def _rezip(game_dir: Path) -> None:
    """重打 <game_id>.zip（atomic：tmp → os.replace），让 zip.mtime 新于 meta，
    /download 不会再触发 on-demand repack，内容立即一致。"""
    import os

    parent = game_dir.parent
    zip_path = parent / f"{game_dir.name}.zip"
    tmp_base = parent / f".{game_dir.name}.inputmode.tmp"
    shutil.make_archive(str(tmp_base), "zip", str(game_dir))
    os.replace(str(tmp_base) + ".zip", str(zip_path))


def _process_one(
    game_dir: Path, db_mode: str, *, dry_run: bool, only_fastpath: bool
) -> tuple[str, str] | None:
    """返回 (game_id, "old->new") 表示修复了；None 表示跳过。"""
    meta_path = game_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ! {game_dir.name}: meta.json 解析失败，跳过 ({e})")
        return None

    if only_fastpath and meta.get("provenance") != "fastpath":
        return None

    current = meta.get("input_mode")
    if current == db_mode:
        return None  # 已一致，幂等跳过

    if dry_run:
        return (game_dir.name, f"{current!r} -> {db_mode!r}")

    meta["input_mode"] = db_mode
    meta["modified_at"] = int(time.time())
    meta.setdefault("created_at", meta["modified_at"])
    # atomic 写 meta.json
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    import os

    os.replace(tmp, meta_path)
    # 重打 zip 让 /download 立即一致
    try:
        _rezip(game_dir)
    except Exception as e:
        print(f"  ! {game_dir.name}: rezip 失败（meta 已修复，下次 download 会自动重打）: {e}")
    return (game_dir.name, f"{current!r} -> {db_mode!r}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只报告不写盘")
    parser.add_argument("--only-fastpath", action="store_true",
                        help="仅修复 provenance=fastpath 的游戏")
    args = parser.parse_args()

    db_modes = await _load_db_input_modes()
    # 干净关闭连接池，避免解释器退出时 aiomysql __del__ 在已关闭的 event loop
    # 上 call_soon 抛 "Event loop is closed" 噪音 traceback。
    from robot.db.database import engine
    await engine.dispose()
    print(f"DB ai_games 合法 input_mode 行：{len(db_modes)}")

    if not GAMES_DIR.is_dir():
        print(f"games 目录不存在：{GAMES_DIR}")
        return 1

    fixed: list[tuple[str, str]] = []
    no_db = 0
    scanned = 0
    for game_dir in sorted(GAMES_DIR.iterdir()):
        if not game_dir.is_dir():
            continue
        if game_dir.name.startswith((".", "_")):
            continue  # _template / _fastpath_references 等非真实游戏
        if not (game_dir / "meta.json").exists():
            continue
        scanned += 1
        db_mode = db_modes.get(game_dir.name)
        if db_mode is None:
            no_db += 1
            continue
        res = _process_one(
            game_dir, db_mode, dry_run=args.dry_run, only_fastpath=args.only_fastpath,
        )
        if res:
            fixed.append(res)

    verb = "将修复" if args.dry_run else "已修复"
    print(f"\n扫描游戏目录：{scanned}（无 DB 行：{no_db}）")
    print(f"{verb} {len(fixed)} 个：")
    for gid, change in fixed:
        print(f"  - {gid}: {change}")
    if args.dry_run and fixed:
        print("\n（dry-run，未写盘。去掉 --dry-run 实际执行。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
