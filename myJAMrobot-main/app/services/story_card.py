"""Render do card vertical (9:16) do /story via Playwright/Chromium.

Dois modos a partir do MESMO template:
- "full": card estático opaco (fundo = capa borrada) — usado no fallback,
  quando não há vídeo de Canvas. Sai em JPEG 1080x1920.
- "overlay": só o painel de info com fundo transparente — sobreposto ao
  vídeo do Canvas via ffmpeg. Sai em PNG 1080x1920 (com alpha).

Sem emojis em nada (política do projeto). Retorna None quando o Playwright
não está disponível ou o render falha — o chamador cai pro próximo fallback.
"""
from __future__ import annotations

import base64
import html
import io
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

CARD_W = 1080
CARD_H = 1920
MAX_SOURCE_PIXELS = 25_000_000
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "story_card.html"

# Capa fallback (gradiente) quando não há bytes de capa — mesmo espírito do
# Gradiente local para o card nunca sair com src quebrado.
FALLBACK_COVER = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 240 240'>"
    "<defs><linearGradient id='g' x1='0' x2='1' y1='0' y2='1'>"
    "<stop offset='0%' stop-color='%235B9CFF'/>"
    "<stop offset='55%' stop-color='%23B58CFE'/>"
    "<stop offset='100%' stop-color='%233FE0A6'/>"
    "</linearGradient></defs>"
    "<rect width='240' height='240' fill='url(%23g)'/>"
    "<circle cx='120' cy='120' r='52' fill='rgba(255,255,255,.18)'/>"
    "</svg>"
)


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _cover_data_uri(raw: bytes | None, *, max_dim: int = 2048) -> str | None:
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width <= 0 or img.height <= 0 or img.width * img.height > MAX_SOURCE_PIXELS:
                return None
            img = img.convert("RGB")
            largest = max(img.size)
            if largest > max_dim:
                scale = max_dim / largest
                img = img.resize(
                    (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                    Image.LANCZOS,
                )
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("STORY_COVER_ENCODE_FAILED", exc_info=True)
        return None


def _logo_data_uri(raw: bytes | None, *, max_dim: int = 128) -> str | None:
    """Logo do bot em PNG, preservando alpha (o CSS arredonda em círculo)."""
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width <= 0 or img.height <= 0 or img.width * img.height > MAX_SOURCE_PIXELS:
                return None
            img = img.convert("RGBA")
            largest = max(img.size)
            if largest > max_dim:
                scale = max_dim / largest
                img = img.resize(
                    (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                    Image.LANCZOS,
                )
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.debug("STORY_LOGO_ENCODE_FAILED", exc_info=True)
        return None


def build_story_html(
    *,
    mode: str,
    cover_uri: str | None,
    listening: str,
    title: str,
    artist: str,
    bot_name: str,
    bot_logo_uri: str | None,
) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    bot_logo_img = f'<img src="{_esc(bot_logo_uri)}" alt="" />' if bot_logo_uri else ""
    values = {
        "mode": mode,
        "cover": _esc(cover_uri or FALLBACK_COVER),
        "listening": _esc(listening),
        "title": _esc(title),
        "artist": _esc(artist),
        "sep": " – ",
        "bot_logo_img": bot_logo_img,
        "bot_name": _esc(bot_name),
    }
    for key, value in values.items():
        template = template.replace("{{ " + key + " }}", str(value))
    return template


async def _render(html_content: str, *, transparent: bool) -> bytes | None:
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except Exception:
        logger.warning("STORY_CARD_RENDER_UNAVAILABLE reason=playwright_import_failed", exc_info=True)
        return None

    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page(
                viewport={"width": CARD_W, "height": CARD_H},
                device_scale_factor=1,
            )
            await page.set_content(html_content, wait_until="networkidle", timeout=20000)
            try:
                await page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                logger.debug("STORY_CARD_FONTS_READY_FAILED", exc_info=True)
            if transparent:
                return await page.screenshot(type="png", omit_background=True, timeout=20000)
            return await page.screenshot(type="jpeg", quality=92, timeout=20000)
    except Exception:
        logger.exception("STORY_CARD_RENDER_FAILED mode_transparent=%s", transparent)
        return None
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.warning("STORY_CARD_BROWSER_CLOSE_FAILED", exc_info=True)


async def render_story_full(
    *,
    cover_bytes: bytes | None,
    listening: str,
    title: str,
    artist: str,
    bot_name: str,
    bot_logo_bytes: bytes | None,
) -> bytes | None:
    """Card estático 1080x1920 (JPEG) — fallback sem vídeo."""
    html_content = build_story_html(
        mode="full",
        cover_uri=_cover_data_uri(cover_bytes),
        listening=listening,
        title=title,
        artist=artist,
        bot_name=bot_name,
        bot_logo_uri=_logo_data_uri(bot_logo_bytes),
    )
    return await _render(html_content, transparent=False)


async def render_story_overlay(
    *,
    cover_bytes: bytes | None,
    listening: str,
    title: str,
    artist: str,
    bot_name: str,
    bot_logo_bytes: bytes | None,
) -> bytes | None:
    """Painel de info 1080x1920 (PNG transparente) — overlay sobre o vídeo."""
    html_content = build_story_html(
        mode="overlay",
        cover_uri=_cover_data_uri(cover_bytes),
        listening=listening,
        title=title,
        artist=artist,
        bot_name=bot_name,
        bot_logo_uri=_logo_data_uri(bot_logo_bytes),
    )
    return await _render(html_content, transparent=True)
