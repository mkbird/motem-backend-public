#!/usr/bin/env python3
"""Migrate legacy ai_* games to current skeleton.

Two idempotent changes per game's scenes/main.gd:

1. AudioManager indirection — appends three self-contained helpers
   (`_audio_play`, `_audio_play_bgm`, `_audio_stop_bgm`) at end of file,
   then rewrites every `AudioManager.play(...)` / `play_bgm(...)` /
   `stop_bgm(...)` call site to the helper form. Helpers do their own
   `/root/AudioManager` lookup so they work regardless of `_ready` shape.
   Fixes the iPhone white-screen P0 (iOS embedded Godot does not register
   autoload names as parse-time globals).

2. gameplay_active gating — wires `bridge.gameplay_active = true` in
   `_enter_playing` and `... = false` in `_enter_game_over` so the macOS
   Motem Studio MacFaceDetector strictly gates the front camera on the
   PLAYING state. Old skeletons lacking this state-machine pair fall back
   to the 3-second legacy window in MacFaceDetector — also fine.

Each touched game gets its zip rebuilt; the previous zip is preserved as
`<game_id>.zip.legacy.bak`. Already-migrated games are no-ops.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

GAMES_DIR = Path(__file__).resolve().parent.parent / "games"

AUDIO_HELPERS = """

# /root/AudioManager indirection — iOS embedded Godot does NOT register
# autoload names as global identifiers, so a bare `AudioManager.play("x")`
# parse-errors at script load (white screen, no sound). All audio goes
# through these helpers, which look the singleton up by node path at the
# call site (cheap; cached by Godot's scene tree).
func _audio_play(sfx_id: String) -> void:
\tvar m: Node = get_node_or_null("/root/AudioManager")
\tif m != null and m.has_method("play"):
\t\tm.call("play", sfx_id)

func _audio_play_bgm(track_id: String) -> void:
\tvar m: Node = get_node_or_null("/root/AudioManager")
\tif m != null and m.has_method("play_bgm"):
\t\tm.call("play_bgm", track_id)

func _audio_stop_bgm() -> void:
\tvar m: Node = get_node_or_null("/root/AudioManager")
\tif m != null and m.has_method("stop_bgm"):
\t\tm.call("stop_bgm")
"""

ENTER_PLAYING_GATE = "\tif bridge != null:\n\t\tbridge.gameplay_active = true\n"
ENTER_GAME_OVER_GATE = "\tif bridge != null:\n\t\tbridge.gameplay_active = false\n"


def migrate_main_gd(path: Path) -> tuple[bool, list[str]]:
    """Idempotent. Returns (changed, notes)."""
    src = path.read_text()
    out = src
    notes: list[str] = []

    needs_audio_helpers = (
        "func _audio_play(" not in out
        and re.search(r"\bAudioManager\.(play|play_bgm|stop_bgm)\b", out) is not None
    )
    if needs_audio_helpers:
        # Append helpers at end of file. They're self-contained — no
        # dependencies on `_audio_mgr` var or any `_ready` shape.
        if not out.endswith("\n"):
            out += "\n"
        out += AUDIO_HELPERS.lstrip("\n")
        notes.append("appended audio helpers")

    # Rewrite call sites — only safe to do AFTER helpers are present (either
    # appended above this turn or already present from a prior run).
    if "func _audio_play(" in out or needs_audio_helpers:
        rewrites = 0
        for pat, repl in [
            (r"\bAudioManager\.play_bgm\(", "_audio_play_bgm("),
            (r"\bAudioManager\.stop_bgm\(\s*\)", "_audio_stop_bgm()"),
            (r"\bAudioManager\.play\(", "_audio_play("),
        ]:
            new, n = re.subn(pat, repl, out)
            if n:
                out = new
                rewrites += n
        if rewrites:
            notes.append(f"rewrote {rewrites} AudioManager call(s)")

    # gameplay_active gating — only meaningful for new-skeleton games that
    # have the START/PLAYING/GAME_OVER state machine. Silently skip if not.
    if "bridge.gameplay_active = true" not in out:
        m = re.search(r"^func _enter_playing\(\)[^\n]*\n", out, flags=re.MULTILINE)
        if m:
            body_m = re.search(r"^\tstate = GameState\.PLAYING[^\n]*\n", out[m.end():], flags=re.MULTILINE)
            if body_m:
                anchor = m.end() + body_m.end()
                out = out[:anchor] + ENTER_PLAYING_GATE + out[anchor:]
                notes.append("wired gameplay_active = true")

    if "bridge.gameplay_active = false" not in out:
        m = re.search(r"^func _enter_game_over\(\)[^\n]*\n", out, flags=re.MULTILINE)
        if m:
            body_m = re.search(r"^\tstate = GameState\.GAME_OVER[^\n]*\n", out[m.end():], flags=re.MULTILINE)
            if body_m:
                anchor = m.end() + body_m.end()
                out = out[:anchor] + ENTER_GAME_OVER_GATE + out[anchor:]
                notes.append("wired gameplay_active = false")

    if out == src:
        return False, notes
    path.write_text(out)
    return True, notes


def rezip(game_dir: Path) -> None:
    parent = game_dir.parent
    zip_path = parent / f"{game_dir.name}.zip"
    backup = parent / f"{game_dir.name}.zip.legacy.bak"
    if zip_path.exists() and not backup.exists():
        shutil.move(str(zip_path), str(backup))
    tmp = parent / f".{game_dir.name}.zip.tmp"
    if tmp.exists():
        tmp.unlink()
    subprocess.run(
        ["zip", "-r", "-q", str(tmp), ".", "-x", "*.DS_Store", "__pycache__/*", ".git/*"],
        cwd=game_dir, check=True,
    )
    os.replace(tmp, zip_path)


def main() -> int:
    if not GAMES_DIR.is_dir():
        print(f"games dir not found: {GAMES_DIR}", file=sys.stderr)
        return 1

    targets = sorted(p for p in GAMES_DIR.glob("ai_*") if p.is_dir())
    if not targets:
        print("no ai_* games found")
        return 0

    changed_count = 0
    for game_dir in targets:
        main_gd = game_dir / "scenes" / "main.gd"
        if not main_gd.is_file():
            print(f"[skip] {game_dir.name}: no scenes/main.gd")
            continue
        changed, notes = migrate_main_gd(main_gd)
        tag = "MIGRATED" if changed else "clean"
        note_str = "; ".join(notes) if notes else "already current"
        print(f"[{tag:>8}] {game_dir.name}: {note_str}")
        if changed:
            rezip(game_dir)
            changed_count += 1

    print(f"\nDone. {changed_count}/{len(targets)} games migrated and rezipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
