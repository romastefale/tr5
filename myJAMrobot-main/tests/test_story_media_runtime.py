from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.services.story_card import _cover_data_uri, build_story_html
from app.services.story_video import compose_story_video


def test_story_card_preserves_hd_source_and_escapes_text() -> None:
    source = io.BytesIO()
    Image.new("RGB", (1000, 1000), (20, 40, 60)).save(source, format="JPEG", quality=95)
    uri = _cover_data_uri(source.getvalue())
    assert uri and uri.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as rendered:
        assert rendered.size == (1000, 1000)
    html = build_story_html(
        mode="full",
        cover_uri=uri,
        listening="<nome>",
        title="A & B",
        artist="Artista",
        bot_name="myJAMrobot",
        bot_logo_uri=None,
    )
    assert "&lt;nome&gt;" in html
    assert "A &amp; B" in html


@pytest.mark.asyncio
async def test_story_composition_is_exact_vertical_hd(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg não está instalado")

    canvas_path = tmp_path / "canvas.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x203040:s=270x480:r=12",
            "-t",
            "0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(canvas_path),
        ],
        check=True,
        timeout=20,
    )
    overlay = io.BytesIO()
    Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(overlay, format="PNG")
    result = await compose_story_video(canvas_path.read_bytes(), overlay.getvalue())
    assert result and result[4:8] == b"ftyp"

    output_path = tmp_path / "story.mp4"
    output_path.write_bytes(result)
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1080, 1920)
