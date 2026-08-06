from __future__ import annotations

from app.security.media import sanitize_search_term, validate_media_url


def test_media_allowlists() -> None:
    assert validate_media_url("https://cdn-images.dzcdn.net/images/cover/a.jpg", kind="cover")
    assert validate_media_url("https://i.scdn.co/image/abc", kind="cover")
    assert validate_media_url("https://canvaz.scdn.co/upload/abc.mp4", kind="canvas")
    assert validate_media_url("https://p.scdn.co/mp3-preview/abc", kind="preview")


def test_media_rejects_ssrf_shapes() -> None:
    assert validate_media_url("http://cdn-images.dzcdn.net/a.jpg", kind="cover") is None
    assert validate_media_url("https://127.0.0.1/a.jpg", kind="cover") is None
    assert validate_media_url("https://cdn-images.dzcdn.net.evil.test/a.jpg", kind="cover") is None
    assert validate_media_url("https://user:pass@i.scdn.co/a.jpg", kind="cover") is None
    assert validate_media_url("https://i.scdn.co/image/a\nb", kind="cover") is None
    assert validate_media_url("https://i.scdn.co:8443/image/a", kind="cover") is None


def test_search_input_is_bounded_and_single_line() -> None:
    value = sanitize_search_term("song\n\x00artist" + "x" * 500)
    assert "\n" not in value and "\x00" not in value
    assert len(value) <= 120
