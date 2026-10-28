# Audio License Notice

All audio files in `library/` are sourced from **Kenney.nl** (https://kenney.nl)
and are released into the **Public Domain (CC0 1.0 Universal)**.

CC0: https://creativecommons.org/publicdomain/zero/1.0/

You can copy, modify, distribute, and use the audio for any purpose, including
commercially, without asking permission and without attribution. We voluntarily
credit the source here.

## Source packs

| Our category | Kenney pack | URL |
|--------------|-------------|-----|
| `ui/`        | Interface Sounds | https://kenney.nl/assets/interface-sounds |
| `event/`     | Interface Sounds + Casino Audio | https://kenney.nl/assets/casino-audio |
| `action/`    | Impact Sounds + Sci-Fi Sounds + RPG Audio | https://kenney.nl/assets/impact-sounds |
| `robot/`     | Sci-Fi Sounds | https://kenney.nl/assets/sci-fi-sounds |
| `bgm/`       | Music Loops | https://kenney.nl/assets/music-loops-3 |

## How to populate

1. Visit each pack URL above and download the zip.
2. Listen to the sounds, pick ones that match the IDs in `manifest.json`.
3. Convert to `.ogg` if needed (`ffmpeg -i input.wav -c:a libvorbis -q:a 4 output.ogg`).
4. Drop into `library/<category>/<id>.ogg` so paths line up with manifest.
5. BGM files should be reasonably small loops (< 200 KB) — trim and convert if
   the source is too long.

If a file is missing, generated games that reference it will simply skip the
sound (post-process logs a warning); the rest of the game still works.
