from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rich_message_2026_api_is_pinned_and_used() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    presentation = (ROOT / "app" / "bot" / "presentation.py").read_text(encoding="utf-8")
    assert "aiogram==3.30.0" in requirements
    assert "InputRichMessage" in presentation
    assert "send_rich_message" in presentation
    assert "rich_message=rich" in presentation


def test_webhook_and_background_work_are_bounded() -> None:
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    work = (ROOT / "app" / "security" / "work_limits.py").read_text(encoding="utf-8")
    spotify = (ROOT / "app" / "services" / "spotify.py").read_text(encoding="utf-8")
    lastfm = (ROOT / "app" / "services" / "lastfm.py").read_text(encoding="utf-8")
    radio_search = (ROOT / "app" / "services" / "track_search.py").read_text(encoding="utf-8")
    assert "MAX_WEBHOOK_BYTES" in main
    assert "async for chunk in request.stream()" in main
    assert "await request.body()" not in main
    assert "hmac.compare_digest" in main
    assert "BackgroundTaskPool" in work
    assert "self.maximum" in work
    assert "_MAX_JSON_BYTES" in spotify and ".stream(" in spotify
    assert "_MAX_JSON_BYTES" in lastfm and ".stream(" in lastfm
    assert "_SEARCH_SLOTS" in radio_search and ".stream(" in radio_search


def test_story_is_exact_9_by_16_hd() -> None:
    source = (ROOT / "app" / "services" / "story_video.py").read_text(encoding="utf-8")
    audio = (ROOT / "app" / "services" / "canvas_audio.py").read_text(encoding="utf-8")
    assert "STORY_W = 1080" in source
    assert "STORY_H = 1920" in source
    assert "if not (CANVAS_CACHE_ENABLED and CANVAS_CACHE_CHANNEL_ID)" not in audio
    assert "audio_canvas_bytes if want_bytes or not file_id else None" in audio


def test_oauth_and_full_lyrics_storage_are_absent() -> None:
    spotify = (ROOT / "app" / "services" / "spotify.py").read_text(encoding="utf-8")
    lyrics = (ROOT / "app" / "services" / "lyrics.py").read_text(encoding="utf-8")
    assert "authorization_code" not in spotify
    assert "refresh_token" not in spotify
    assert "SpotifyToken" not in spotify
    assert "MAX_EXCERPT_WORDS = 10" in lyrics
    assert "full_lyrics" not in lyrics


def test_database_boot_never_drops_historical_tables() -> None:
    database = (ROOT / "app" / "db" / "database.py").read_text(encoding="utf-8")
    settings = (ROOT / "app" / "config" / "settings.py").read_text(encoding="utf-8")
    assert "DROP TABLE" not in database.upper()
    assert "drop_all" not in database
    assert "chmod(0o600)" in database
    assert "refusing an implicit database fallback" in settings
