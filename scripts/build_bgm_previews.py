"""Pre-build .m4a previews of all BGM tracks for iOS UI playback.

iOS AVPlayer cannot decode .ogg Vorbis natively, so we transcode each
BGM into AAC/.m4a (~20s clip, ~64kbps) once and serve those previews
via the games API. Re-run this script whenever new BGMs are added to
the manifest.
"""

import json
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = BACKEND_ROOT / "robot" / "games" / "assets" / "audio"
LIBRARY_DIR = AUDIO_DIR / "library"
PREVIEW_DIR = AUDIO_DIR / "bgm_previews"
MANIFEST = AUDIO_DIR / "manifest.json"

PREVIEW_SECONDS = 20
PREVIEW_BITRATE = "64k"


def main() -> int:
    if not MANIFEST.exists():
        print(f"manifest not found: {MANIFEST}", file=sys.stderr)
        return 1
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bgms = [s for s in data.get("sounds", []) if s.get("category") == "bgm"]
    print(f"converting {len(bgms)} BGMs → {PREVIEW_DIR}")

    converted = 0
    skipped = 0
    failed = 0
    for sound in bgms:
        name = sound["id"]
        src = LIBRARY_DIR / sound["file"]
        dst = PREVIEW_DIR / f"{name}.m4a"
        if not src.exists():
            print(f"  ! source missing: {src}")
            failed += 1
            continue
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            skipped += 1
            continue
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(src),
            "-t", str(PREVIEW_SECONDS),
            "-vn",
            "-c:a", "aac",
            "-b:a", PREVIEW_BITRATE,
            "-movflags", "+faststart",
            str(dst),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ✗ {name}: ffmpeg failed\n{result.stderr[-300:]}")
            failed += 1
            continue
        converted += 1
        print(f"  ✓ {name}.m4a ({dst.stat().st_size // 1024} KB)")

    print(f"\ndone: {converted} converted, {skipped} cached, {failed} failed")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
