"""把 Unicode emoji 现场渲染成 PNG，供 Customizer 直出的 emoji 字符使用。

复用 scripts/render_catalog_sprites.py 的核心逻辑，但提供更稳定的 Python
模块接口（不是 CLI），让 Compiler.prepare_assets 直接调用。

环境约束：当前只支持 macOS（Apple Color Emoji.ttc）；Linux 部署时需要
切到 Noto Color Emoji 或类似 SBIX/CBDT 彩色字体。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


# Apple Color Emoji 的 SBIX bitmap 在 size=160 时画质最稳，96/40 也合法
_EMOJI_FONT_PATH = "/System/Library/Fonts/Apple Color Emoji.ttc"
_EMOJI_RENDER_SIZE = 160
_DEFAULT_OUTPUT_SIZE = 96
_FALLBACK_EMOJI = "⚪"  # 真渲染不出来时的最后兜底（白圆圈）


# 字体一次性加载——线程安全
_font_lock = threading.Lock()
_cached_font: ImageFont.FreeTypeFont | None = None


def _get_font() -> ImageFont.FreeTypeFont:
    global _cached_font
    if _cached_font is None:
        with _font_lock:
            if _cached_font is None:
                _cached_font = ImageFont.truetype(_EMOJI_FONT_PATH, _EMOJI_RENDER_SIZE)
    return _cached_font


def render_emoji_to_png(
    emoji: str,
    out_path: Path,
    *,
    size: int = _DEFAULT_OUTPUT_SIZE,
) -> bool:
    """渲染单个 emoji 为透明 PNG 写入 out_path。

    Returns True on success；rendering 出错时 fallback 渲 ⚪ 并 log warn，返回 False。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _do_render(emoji, out_path, size)
        return True
    except Exception as e:
        logger.warning("[Fastpath/Emoji] render %r failed: %s; using ⚪ fallback", emoji, e)
        try:
            _do_render(_FALLBACK_EMOJI, out_path, size)
        except Exception as e2:
            logger.error("[Fastpath/Emoji] fallback ⚪ also failed: %s", e2)
            # 最后兜底：纯灰 PNG
            Image.new("RGBA", (size, size), (200, 200, 200, 255)).save(out_path, "PNG")
        return False


def _do_render(emoji: str, out_path: Path, size: int) -> None:
    font = _get_font()
    canvas = Image.new("RGBA", (_EMOJI_RENDER_SIZE, _EMOJI_RENDER_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((0, 0), emoji, font=font, embedded_color=True)

    # 裁透明边再居中放大正方，避免 emoji 偏左上
    bbox = canvas.getbbox()
    if bbox:
        cropped = canvas.crop(bbox)
        side = max(cropped.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        offset = ((side - cropped.size[0]) // 2, (side - cropped.size[1]) // 2)
        square.paste(cropped, offset, cropped)
    else:
        square = canvas

    square.resize((size, size), Image.LANCZOS).save(out_path, "PNG", optimize=True)


def font_available() -> bool:
    """部署环境检测：emoji 字体存在否。"""
    return Path(_EMOJI_FONT_PATH).is_file()
