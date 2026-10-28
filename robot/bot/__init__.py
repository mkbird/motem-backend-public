"""Bot adapter layer (module ① of the v1 web-games plan).

Wraps the existing game-generation agent so users can create / modify games
from a chat platform (Discord first) and get back a public, login-free
``/play/{game_id}/`` link. The bot is a separate client process that drives the
same agent — it does NOT go through the iOS WebSocket flow.

Layout:
  service.py      platform-agnostic bridge (intent parse, agent call, link
                  build, reply formatting) — fully unit-tested.
  discord_bot.py  Discord client wiring (discord.py, imported lazily).

Run: ``python scripts/run_discord_bot.py`` (needs ROBOT_DISCORD_BOT_* env).
"""
