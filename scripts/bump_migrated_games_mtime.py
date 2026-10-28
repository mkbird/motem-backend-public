#!/usr/bin/env python3
"""Bump `meta.json.modified_at` for every game whose zip was rebuilt by
`migrate_legacy_games.py` (i.e. has a sibling `<id>.zip.legacy.bak`).

iOS `GameFileManager.localMetaModifiedAt` compares the local copy's value
to the server's `content_modified_at` (derived from `meta.json.modified_at`,
falling back to zip mtime). Bumping this field forces clients to re-download
the freshly-migrated zip on their next list refresh.

Also drops the explore / per-user / meta caches in Redis so the next
`/api/games` response reflects the new `content_modified_at` immediately
instead of waiting on the 60s TTL.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

GAMES_DIR = Path(__file__).resolve().parent.parent / "games"


def bump_meta(game_id: str, now: int) -> bool:
    meta_path = GAMES_DIR / game_id / "meta.json"
    if not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[!! parse fail] {game_id}: {e}")
        return False
    old = meta.get("modified_at")
    if old == now:
        return False
    meta["modified_at"] = now
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"[bumped] {game_id}: {old} -> {now}")
    return True


async def drop_caches(game_ids: list[str]) -> None:
    try:
        from robot.db.redis import redis_client  # type: ignore
    except Exception as e:
        print(f"[redis] skip (import failed: {e})")
        return
    keys: list[str] = ["games:explore:v1"]
    for gid in game_ids:
        keys.append(f"games:meta:{gid}:v1")
    # per-user mine cache: blow away every games:mine:* (small set on dev box)
    try:
        async for k in redis_client.scan_iter(match="games:mine:*", count=100):
            keys.append(k.decode() if isinstance(k, (bytes, bytearray)) else k)
    except Exception as e:
        print(f"[redis] scan games:mine:* failed: {e}")
    if keys:
        try:
            n = await redis_client.delete(*keys)
            print(f"[redis] dropped {n} cache key(s)")
        except Exception as e:
            print(f"[redis] DEL failed: {e}")


def main() -> int:
    backups = sorted(GAMES_DIR.glob("ai_*.zip.legacy.bak"))
    game_ids = [p.name[: -len(".zip.legacy.bak")] for p in backups]
    if not game_ids:
        print("no migrated games (no *.zip.legacy.bak found)")
        return 0

    now = int(time.time())
    bumped: list[str] = []
    for gid in game_ids:
        if bump_meta(gid, now):
            bumped.append(gid)

    print(f"\nBumped {len(bumped)}/{len(game_ids)} meta files to modified_at={now}.")

    if bumped:
        try:
            asyncio.run(drop_caches(bumped))
        except Exception as e:
            print(f"[redis] async run failed: {e}")

    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
