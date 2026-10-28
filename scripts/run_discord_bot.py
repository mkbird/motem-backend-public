#!/usr/bin/env python
"""Run the Discord game-creation bot.

    conda run -n robot python scripts/run_discord_bot.py

Reads ROBOT_DISCORD_BOT_* / ROBOT_BOT_OWNER_USER_ID / ROBOT_SHARE_PUBLIC_BASE_URL
from .env. See robot/bot/README.md.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO)

from robot.bot.discord_bot import run  # noqa: E402

if __name__ == "__main__":
    run()
