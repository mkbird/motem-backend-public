"""Public share routes for AI games.

Serves per-game landing HTML with Open Graph / Twitter card metadata so
social-media crawlers (Twitter, Facebook, WhatsApp, Discord, iMessage, etc.)
can expand shared URLs into rich preview cards. Also serves a Pillow-rendered
1200×630 PNG as the og:image.

Not under ``/api/*`` — these endpoints are anonymous-public on purpose.
"""

from __future__ import annotations

import html
import io
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.config import settings
from robot.db.database import get_db
from robot.db.models import GameStats
from robot.i18n import get_locale

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/share", tags=["share"])

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
GAMES_DIR = _BACKEND_ROOT / "games"
FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Regular.otf"


# ─────────────────────────────────────────────────────────────────────
# Meta loader
# ─────────────────────────────────────────────────────────────────────


def _load_meta(game_id: str) -> dict[str, Any]:
    meta_file = GAMES_DIR / game_id / "meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="game_not_found")
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to read meta for %s: %s", game_id, e)
        raise HTTPException(status_code=404, detail="game_meta_invalid")


def _pick_localized(meta: dict[str, Any], field: str, locale: str) -> str:
    """Pick localized value for title / description."""
    i18n = meta.get(f"{field}_i18n")
    if isinstance(i18n, dict):
        for key in (locale, locale.split("-")[0], "en", "zh-Hans"):
            if key and i18n.get(key):
                return i18n[key]
    return meta.get(field) or ""


def _public_base_url(request: Request) -> str:
    """Return public base URL: settings override first, else inferred from request.

    Respects X-Forwarded-Proto / X-Forwarded-Host so ngrok's public HTTPS
    hostname wins over the internal uvicorn host.
    """
    override = (settings.share_public_base_url or "").rstrip("/")
    if override:
        return override
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _onelink_url(onelink_template: str, game_id: str) -> str:
    """Append deep_link_value + game_id to the OneLink template URL."""
    sep = "&" if "?" in onelink_template else "?"
    return f"{onelink_template}{sep}deep_link_value={game_id}&game_id={game_id}"


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


@router.get("/{game_id}", response_class=HTMLResponse)
async def share_landing(
    game_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Public HTML landing with OG / Twitter card meta + CTA. 显示只读计数。"""
    meta = _load_meta(game_id)
    locale = get_locale()
    title = _pick_localized(meta, "title", locale) or "AI Game"
    description = _pick_localized(meta, "description", locale) or ""
    icon = meta.get("icon") or "🎮"
    input_mode = meta.get("input_mode") or "touch"

    # 只读计数 — 此路径不触发 +1（避免 FB / Twitter 爬虫扫到时把计数刷高）
    stats_row = (
        await db.execute(select(GameStats).where(GameStats.game_id == game_id))
    ).scalar_one_or_none()
    share_count = stats_row.share_count if stats_row else 0
    like_count = stats_row.like_count if stats_row else 0

    base_url = _public_base_url(request)
    page_url = f"{base_url}/share/{game_id}"
    og_image = f"{base_url}/share/{game_id}/og-image.png"
    cta_url = _onelink_url(settings.onelink_template_url, game_id)

    # Bilingual UI strings
    is_zh = locale.startswith("zh")
    cta_label = "在 motem 中打开" if is_zh else "Open in motem"
    about_title = "什么是 motem？" if is_zh else "What is motem?"
    about_body = (
        "motem 是一款游戏创作与分享应用：用自然语言创作 AI 小游戏，并在手机上游玩和分享。"
        if is_zh
        else "motem is a game creation and sharing app: create AI mini-games with natural language, then play and share them on your phone."
    )
    mode_label_visual = "视觉控制" if is_zh else "Visual Control"
    mode_label_touch = "触控" if is_zh else "Touch"
    mode_label = mode_label_visual if input_mode == "body_pose" else mode_label_touch
    mode_emoji = "👁" if input_mode == "body_pose" else "👆"

    e_title = html.escape(title)
    e_desc = html.escape(description)

    html_body = f"""<!DOCTYPE html>
<html lang="{'zh-Hans' if is_zh else 'en'}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{e_title} · motem</title>

<!-- Open Graph -->
<meta property="og:type" content="website">
<meta property="og:site_name" content="motem">
<meta property="og:url" content="{page_url}">
<meta property="og:title" content="{e_title}">
<meta property="og:description" content="{e_desc}">
<meta property="og:image" content="{og_image}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">

<!-- Twitter card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e_title}">
<meta name="twitter:description" content="{e_desc}">
<meta name="twitter:image" content="{og_image}">

<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: linear-gradient(180deg, #F7F8FA 0%, #EEF1F5 100%);
    color: #1C1C1E;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    -webkit-font-smoothing: antialiased;
  }}
  .card {{
    background: #FFFFFF;
    border-radius: 20px;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.08);
    max-width: 640px;
    width: 100%;
    padding: 40px 32px;
    text-align: center;
  }}
  .icon {{ font-size: 96px; line-height: 1; margin-bottom: 16px; }}
  .title {{
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin-bottom: 12px;
  }}
  .chips {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    justify-content: center;
    margin-bottom: 20px;
  }}
  .chip {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: 999px;
    background: #F0F2F5;
    color: #4B5563;
    font-size: 13px;
    font-weight: 500;
  }}
  .description {{
    font-size: 16px;
    line-height: 1.6;
    color: #6B7280;
    margin-bottom: 32px;
  }}
  .cta {{
    display: inline-block;
    background: #0A84FF;
    color: #FFFFFF;
    text-decoration: none;
    font-size: 17px;
    font-weight: 600;
    padding: 14px 36px;
    border-radius: 14px;
    transition: transform 0.15s, background 0.15s;
  }}
  .cta:hover {{ background: #0060D1; transform: translateY(-1px); }}
  .about {{
    margin-top: 40px;
    padding-top: 32px;
    border-top: 1px solid #E5E7EB;
    text-align: left;
  }}
  .about h2 {{
    font-size: 15px;
    font-weight: 600;
    color: #1C1C1E;
    margin-bottom: 8px;
  }}
  .about p {{ font-size: 14px; line-height: 1.5; color: #6B7280; }}
  .footer {{
    margin-top: 24px;
    font-size: 12px;
    color: #9CA3AF;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">{html.escape(icon)}</div>
  <h1 class="title">{e_title}</h1>
  <div class="chips">
    <span class="chip">{mode_emoji} {html.escape(mode_label)}</span>
    <span class="chip">⤴ {share_count}</span>
    <span class="chip">♡ {like_count}</span>
  </div>
  <p class="description">{e_desc}</p>
  <a class="cta" href="{html.escape(cta_url)}">{html.escape(cta_label)}</a>
  <div class="about">
    <h2>{html.escape(about_title)}</h2>
    <p>{html.escape(about_body)}</p>
  </div>
  <div class="footer">motem · AI Games</div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html_body)


@router.get("/{game_id}/og-image.png")
async def share_og_image(game_id: str, request: Request) -> Response:
    """1200×630 PNG rendered with Pillow for OG / Twitter card image."""
    meta = _load_meta(game_id)
    locale = get_locale()
    title = _pick_localized(meta, "title", locale) or "AI Game"
    description = _pick_localized(meta, "description", locale) or ""
    icon = meta.get("icon") or "🎮"

    png_bytes = _render_og_image(title=title, description=description, icon=icon)
    return Response(content=png_bytes, media_type="image/png")


# ─────────────────────────────────────────────────────────────────────
# Pillow OG image
# ─────────────────────────────────────────────────────────────────────


def _render_og_image(title: str, description: str, icon: str) -> bytes:
    """Render a 1200×630 light-modern-style share card.

    Layout:
      - Vertical gradient background
      - Small "motem" watermark top-left
      - Emoji icon left-center (180px)
      - Title (60px bold) + description (28px muted, wrapped, max 3 lines) stacked right of icon
      - "AI Games" watermark bottom-right
    """
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), color=(247, 248, 250))
    draw = ImageDraw.Draw(img)

    # Gradient background (top → bottom, subtle)
    for y in range(H):
        t = y / (H - 1)
        r = int(247 + (229 - 247) * t)
        g = int(248 + (233 - 248) * t)
        b = int(250 + (240 - 250) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Fonts — NotoSansSC covers CJK + Latin
    def _font(size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(str(FONT_PATH), size=size)
        except Exception:
            return ImageFont.load_default()

    font_brand = _font(26)
    font_icon = _font(180)
    font_title = _font(64)
    font_desc = _font(28)
    font_tag = _font(22)

    brand_color = (107, 114, 128)
    title_color = (28, 28, 30)
    desc_color = (107, 114, 128)

    # Top-left brand
    draw.text((48, 36), "motem", font=font_brand, fill=brand_color)

    # Bottom-right tag
    tag = "AI Games"
    tag_bbox = draw.textbbox((0, 0), tag, font=font_tag)
    draw.text(
        (W - 48 - (tag_bbox[2] - tag_bbox[0]), H - 48 - (tag_bbox[3] - tag_bbox[1])),
        tag,
        font=font_tag,
        fill=brand_color,
    )

    # Icon (emoji may render as monochrome on systems without color-emoji font —
    # acceptable fallback; visible on macOS / Linux with a color font installed)
    icon_x, icon_y = 80, H // 2 - 90
    draw.text((icon_x, icon_y), icon, font=font_icon, fill=title_color)

    # Title + description — left edge starts right of the icon
    text_x = 320
    text_y_title = 220

    draw.text((text_x, text_y_title), title, font=font_title, fill=title_color)

    # Wrap description
    if description:
        max_width = W - text_x - 80
        lines = _wrap_text(draw, description, font_desc, max_width, max_lines=3)
        y = text_y_title + 100
        for line in lines:
            draw.text((text_x, y), line, font=font_desc, fill=desc_color)
            y += 42

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 3,
) -> list[str]:
    """Greedy wrap respecting word boundaries; per-char wrap for CJK runs."""
    lines: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        w = draw.textlength(candidate, font=font)
        if w <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = ch
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) >= max_lines and current:
        # Ellipsize last line
        last = lines[-1]
        ellipsis = "…"
        while draw.textlength(last + ellipsis, font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last + ellipsis

    return lines
