"""Patch existing games' main.gd to apply 2× center scaling on face_position
fallback inside `get_body_target()` (and `_resolve_cursor()` for slicer).

Reason: skeleton's `get_body_target()` helper shipped without the 2× scaling
that 火球掌控 (ai_1777535277_165fa2) used inline; without scaling, face
naturally spans only ~0.35-0.65 → can't reach screen edges. Fix is in
template + new strategies, but existing on-disk games still have the bug
baked into their zip — this script retro-patches them.

Idempotent: skips files already containing the patched pattern. Re-zips
every patched game so iOS sees updated content next time it pulls.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


# 待替换的两种模式（一个是 get_body_target 的 face fallback，一个是
# slicer_v1 的 _resolve_cursor face fallback）。匹配尽量保守，前后留足
# 上下文避免误改其它脚本里相似但不同的代码。
_PATTERNS = [
    # Pattern A — skeleton get_body_target 的 face fallback
    (
        "\t\telif bridge.face_detected:\n"
        "\t\t\ttx = bridge.face_position.x * screen_size.x\n"
        "\t\t\tty = bridge.face_position.y * screen_size.y\n",
        "\t\telif bridge.face_detected:\n"
        "\t\t\t# 2× center scale (retro-patched) — face naturally spans 0.35-0.65\n"
        "\t\t\tvar fx: float = (bridge.face_position.x - 0.5) * 2.0 + 0.5\n"
        "\t\t\tvar fy: float = (bridge.face_position.y - 0.5) * 2.0 + 0.5\n"
        "\t\t\ttx = fx * screen_size.x\n"
        "\t\t\tty = fy * screen_size.y\n",
    ),
    # Pattern B — slicer _resolve_cursor 的 face fallback
    (
        "\t\tif bridge.face_detected:\n"
        "\t\t\treturn Vector2(bridge.face_position.x * screen_size.x, bridge.face_position.y * screen_size.y)\n",
        "\t\tif bridge.face_detected:\n"
        "\t\t\tvar fx: float = (bridge.face_position.x - 0.5) * 2.0 + 0.5\n"
        "\t\t\tvar fy: float = (bridge.face_position.y - 0.5) * 2.0 + 0.5\n"
        "\t\t\treturn Vector2(fx * screen_size.x, fy * screen_size.y)\n",
    ),
]


def _patched_marker_present(text: str) -> bool:
    """简易幂等性检测：已经包含 `(bridge.face_position.x - 0.5) * 2.0 + 0.5`
    说明这次或上次已经打过，跳过避免重复改。"""
    return "(bridge.face_position.x - 0.5) * 2.0" in text


def patch_main_gd(main_gd: Path) -> bool:
    """打 patch；返回 True 表示有修改。"""
    text = main_gd.read_text(encoding="utf-8")
    if _patched_marker_present(text):
        return False
    if "func get_body_target" not in text and "func _resolve_cursor" not in text:
        return False  # 不是 skeleton 模式游戏，跳过

    new_text = text
    for old, new in _PATTERNS:
        if old in new_text:
            new_text = new_text.replace(old, new)
    if new_text == text:
        return False  # 模式没匹配上
    main_gd.write_text(new_text, encoding="utf-8")
    return True


def rezip_game(game_dir: Path) -> int:
    """重打 game zip。返回 zip 大小（字节）。"""
    zip_base = game_dir.parent / game_dir.name
    zip_path = zip_base.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_base), "zip", str(game_dir))
    return zip_path.stat().st_size


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: patch_face_scale.py <games_dir> [<games_dir2> ...]")
        print("  example: patch_face_scale.py games /path/to/main-backend/games")
        return 1

    n_scanned = 0
    n_patched = 0
    n_rezipped = 0
    for root in args:
        root_p = Path(root)
        if not root_p.is_dir():
            print(f"  skip (not a dir): {root_p}")
            continue
        for game_dir in sorted(root_p.glob("ai_*")):
            if not game_dir.is_dir():
                continue
            main_gd = game_dir / "scenes" / "main.gd"
            if not main_gd.is_file():
                continue
            n_scanned += 1
            if patch_main_gd(main_gd):
                size = rezip_game(game_dir)
                n_patched += 1
                n_rezipped += 1
                print(f"  ✓ {game_dir.name}  patched + re-zipped ({size} B)")
            else:
                print(f"  · {game_dir.name}  skip (already patched / not skeleton-mode)")

    print(f"\nscanned {n_scanned}, patched {n_patched}, re-zipped {n_rezipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
