"""Composição do vídeo do /story: vídeo do Canvas (fundo) + card (overlay).

Usa ffmpeg (disponível no ambiente) pra escalar/cropar o Canvas pra 1080x1920
e sobrepor o PNG transparente do card por cima. Tudo em arquivos temporários,
com timeout — qualquer falha retorna None e o chamador cai pro card estático.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

STORY_W = 1080
STORY_H = 1920
# Canvas é curto (~3-8s) e veryfast; 40s é teto bem folgado pra cold start.
_FFMPEG_TIMEOUT_SECONDS = 40.0


async def compose_story_video(canvas_bytes: bytes | None, overlay_png: bytes | None) -> bytes | None:
    if not canvas_bytes or not overlay_png:
        return None

    tmp_dir = tempfile.mkdtemp(prefix="story_")
    inp = os.path.join(tmp_dir, "canvas.mp4")
    overlay = os.path.join(tmp_dir, "overlay.png")
    out = os.path.join(tmp_dir, "out.mp4")
    try:
        with open(inp, "wb") as fh:
            fh.write(canvas_bytes)
        with open(overlay, "wb") as fh:
            fh.write(overlay_png)

        # Escala o Canvas preenchendo 1080x1920 (sem barras), corta o excesso,
        # normaliza SAR e sobrepõe o card por cima (0,0).
        filter_complex = (
            f"[0:v]scale={STORY_W}:{STORY_H}:force_original_aspect_ratio=increase,"
            f"crop={STORY_W}:{STORY_H},setsar=1[bg];"
            f"[bg][1:v]overlay=0:0:format=auto[vout]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", inp,
            "-i", overlay,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            out,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("STORY_FFMPEG_TIMEOUT")
            return None
        if proc.returncode != 0:
            tail = (stderr or b"")[-600:].decode("utf-8", "replace")
            logger.warning("STORY_FFMPEG_FAILED rc=%s err=%s", proc.returncode, tail)
            return None
        with open(out, "rb") as fh:
            return fh.read()
    except Exception:
        logger.exception("STORY_FFMPEG_ERROR")
        return None
    finally:
        for path in (inp, overlay, out):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
