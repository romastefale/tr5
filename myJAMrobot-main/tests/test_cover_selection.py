from __future__ import annotations

from app.services.spotify import _largest_image


def test_largest_declared_cover_is_selected_without_upscaling() -> None:
    selected = _largest_image(
        [
            {"url": "small", "width": 64, "height": 64},
            {"url": "large", "width": 640, "height": 640},
            {"url": "medium", "width": 300, "height": 300},
        ]
    )
    assert selected == ("large", 640, 640)
