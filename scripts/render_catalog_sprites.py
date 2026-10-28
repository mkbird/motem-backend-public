"""把 AssetCatalog 里的 emoji 字段渲染成 PNG，作为 fast path sprite 资产。

PIL + Apple Color Emoji（macOS 系统字体）渲染：
- Apple Color Emoji 是 SBIX bitmap font，只能在指定 size 加载（160/96/40）
- 用 size=160 加载 + LANCZOS 缩小到 96x96 → 清晰 sprite
- `embedded_color=True` 走 SBIX 路径输出彩色
- 透明背景 PNG

输出目录：games/_fastpath_catalog/sprites/<id>.png

用法：
    python scripts/render_catalog_sprites.py            # 输出到默认目录
    python scripts/render_catalog_sprites.py /tmp/out   # 自定义
    python scripts/render_catalog_sprites.py --size 128 # 输出 size（默认 96）
    python scripts/render_catalog_sprites.py --force    # 覆盖已存在

非 macOS 系统：需要替换字体路径为 Noto Color Emoji 等支持 SBIX/CBDT 的彩色
emoji 字体。当前实现硬编码 macOS 路径，因为我们的 dev/staging 都跑 macOS。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from robot.games.fastpath.catalog import (
    AssetCatalog,
    BackgroundEntry,
    CatalogEntry,
)


# Apple Color Emoji 的 SBIX bitmap 在 size=160 时质量最稳
_EMOJI_FONT_PATH = "/System/Library/Fonts/Apple Color Emoji.ttc"
_EMOJI_RENDER_SIZE = 160
_OUTPUT_SIZE_DEFAULT = 96

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _BACKEND_ROOT / "games" / "_fastpath_catalog" / "sprites"


def _render_emoji(emoji: str, out_size: int) -> Image.Image:
    """渲染一个 emoji 为指定尺寸的透明 PNG Image。"""
    font = ImageFont.truetype(_EMOJI_FONT_PATH, _EMOJI_RENDER_SIZE)
    canvas = Image.new("RGBA", (_EMOJI_RENDER_SIZE, _EMOJI_RENDER_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((0, 0), emoji, font=font, embedded_color=True)

    # 裁掉透明边再居中放大，避免 emoji 在 canvas 中偏左上
    bbox = canvas.getbbox()
    if bbox:
        cropped = canvas.crop(bbox)
        max_side = max(cropped.size)
        # 居中粘到正方形
        square = Image.new("RGBA", (max_side, max_side), (0, 0, 0, 0))
        offset = ((max_side - cropped.size[0]) // 2, (max_side - cropped.size[1]) // 2)
        square.paste(cropped, offset, cropped)
    else:
        square = canvas

    return square.resize((out_size, out_size), Image.LANCZOS)


def _all_entries(catalog: AssetCatalog) -> list[CatalogEntry]:
    """所有带 emoji 字段的条目（characters / objects / obstacles）。"""
    entries: list[CatalogEntry] = []
    for group in (catalog._characters, catalog._objects, catalog._obstacles):
        for e in group:
            if e.emoji:
                entries.append(e)
    return entries


def main() -> int:
    p = argparse.ArgumentParser(description="Render catalog emoji sprites to PNG.")
    p.add_argument("out", nargs="?", default=str(_DEFAULT_OUT))
    p.add_argument("--size", type=int, default=_OUTPUT_SIZE_DEFAULT)
    p.add_argument("--force", action="store_true", help="overwrite existing PNGs")
    args = p.parse_args()

    if not Path(_EMOJI_FONT_PATH).is_file():
        print(f"emoji font not found: {_EMOJI_FONT_PATH}", file=sys.stderr)
        print("only macOS supported in this implementation; "
              "swap _EMOJI_FONT_PATH to Noto Color Emoji for Linux.",
              file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog = AssetCatalog.default()
    entries = _all_entries(catalog)

    n_rendered = 0
    n_skipped = 0
    for e in entries:
        target = out_dir / f"{e.id}.png"
        if target.exists() and not args.force:
            n_skipped += 1
            continue
        try:
            img = _render_emoji(e.emoji, args.size)
            img.save(target, "PNG", optimize=True)
            print(f"  ✓ {e.id:14s} {e.emoji}  → {target.name} ({target.stat().st_size} B)")
            n_rendered += 1
        except Exception as exc:
            print(f"  ✗ {e.id:14s} {e.emoji}  FAILED: {exc}")

    print(f"\nrendered {n_rendered}, skipped {n_skipped} (use --force to overwrite)")
    print(f"output: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
