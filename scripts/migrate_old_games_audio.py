"""One-shot migration: add sound effects to existing ai_* games.

Iterates `games/ai_*/` directories and runs `modify_game(...)` on each one to
let Claude Code SDK retrofit AudioManager.play(...) calls into existing
gameplay code. The post-process step `_harvest_audio` automatically copies
referenced .ogg files into the game's audio/ directory and injects the
AudioManager autoload if missing.

Run **once** after the audio library is populated. Safe to re-run — Claude
will see the existing AudioManager.play calls and skip re-adding audio that's
already in place.

Usage:
    conda activate robot
    cd motem-backend
    python -m scripts.migrate_old_games_audio
    python -m scripts.migrate_old_games_audio --only ai_1776922700_342680  # single game
    python -m scripts.migrate_old_games_audio --dry-run                    # list what would run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Allow running as `python scripts/migrate_old_games_audio.py` from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from robot.agent.tools import game_tools  # noqa: E402
from robot.agent.tools import _context as ctx  # noqa: E402

LOG_PATH = _REPO_ROOT / "scripts" / "migrate_old_games_audio.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("audio_migration")


_INSTRUCTION = (
    "Please add appropriate sound effects to this game using the AudioManager "
    "autoload. Cover at minimum: UI clicks, scoring events, taking damage / "
    "losing a life, game over, and the primary action (jump / hit / pickup / "
    "shoot — whichever fits this game). Add looping background music with "
    "play_bgm only if the genre suits it (action / arcade / casual). "
    "Use names ONLY from the available sound catalog. Do NOT change the "
    "gameplay, scoring rules, or controls — only add audio calls."
)


async def _migrate_one(game_id: str) -> tuple[str, bool, str]:
    log.info("[migrate] ▶ %s", game_id)
    t0 = time.time()
    try:
        # Set a synthetic context so progress messages don't crash even if
        # called outside a real WebSocket session. The migration script runs
        # offline; ctx defaults to no ws_manager.
        result = await game_tools.modify_game.ainvoke({
            "game_id": game_id,
            "instruction": _INSTRUCTION,
        })
        elapsed = time.time() - t0
        log.info("[migrate] ✓ %s done in %.1fs | %s", game_id, elapsed, result[:120])
        return (game_id, True, result)
    except Exception as e:
        elapsed = time.time() - t0
        log.exception("[migrate] ✗ %s failed in %.1fs: %s", game_id, elapsed, e)
        return (game_id, False, str(e))


async def _run(only: list[str] | None, dry_run: bool) -> None:
    games_dir = game_tools.GAMES_DIR
    if only:
        targets = [g for g in only]
    else:
        targets = sorted(
            d.name for d in games_dir.iterdir()
            if d.is_dir() and d.name.startswith("ai_")
        )

    log.info("[migrate] %d games selected: %s",
             len(targets), ", ".join(targets[:10]) + ("…" if len(targets) > 10 else ""))

    if dry_run:
        log.info("[migrate] dry-run only; exit without invoking modify_game")
        return

    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    for gid in targets:
        gid_, ok, msg = await _migrate_one(gid)
        if ok:
            successes.append(gid_)
        else:
            failures.append((gid_, msg))

    log.info("[migrate] ===== SUMMARY =====")
    log.info("[migrate] success: %d", len(successes))
    for gid in successes:
        log.info("[migrate]   ✓ %s", gid)
    log.info("[migrate] failure: %d", len(failures))
    for gid, msg in failures:
        log.info("[migrate]   ✗ %s — %s", gid, msg[:120])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", action="append", default=[],
                    help="Migrate only this game id (can pass multiple times).")
    ap.add_argument("--dry-run", action="store_true",
                    help="List games that would be migrated; don't run modify.")
    args = ap.parse_args()

    asyncio.run(_run(args.only or None, args.dry_run))


if __name__ == "__main__":
    main()
