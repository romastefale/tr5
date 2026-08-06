from __future__ import annotations

import re

from app.services.lyrics import (
    MAX_EXCERPT_WORDS,
    bound_excerpt_text,
    extract_chorus_excerpt,
    select_lyric_excerpt,
)

WORD_RE = re.compile(r"[^\W_]+(?:[’'-][^\W_]+)*", re.UNICODE)


def test_explicit_chorus_is_selected_and_limited() -> None:
    lyrics = """[Verse]\nNot this opening line at all\n\n[Chorus]\nWe sing this bright refrain together every single night again\nSecond chorus line\n\n[Verse 2]\nDifferent words"""
    excerpt = extract_chorus_excerpt(lyrics)
    assert excerpt is not None
    assert excerpt.startswith("We sing this bright refrain")
    assert len(WORD_RE.findall(excerpt)) <= MAX_EXCERPT_WORDS
    assert select_lyric_excerpt(lyrics)[1] == "chorus"


def test_repeated_stanza_wins_without_labels() -> None:
    lyrics = """Opening verse words\n\nHold on to the light\nWe are coming home tonight\n\nAnother verse here\n\nHold on to the light\nWe are coming home tonight"""
    excerpt = extract_chorus_excerpt(lyrics)
    assert excerpt is not None
    assert excerpt.startswith("Hold on")
    assert len(WORD_RE.findall(excerpt)) <= 10
    assert select_lyric_excerpt(lyrics)[1] == "chorus"


def test_no_full_lyrics_can_escape() -> None:
    excerpt = extract_chorus_excerpt(" ".join(f"word{index}" for index in range(500)))
    assert excerpt is not None
    assert len(WORD_RE.findall(excerpt)) == 10
    assert select_lyric_excerpt(" ".join(f"word{index}" for index in range(500)))[1] == "excerpt"


def test_output_boundary_relimits_legacy_cached_text() -> None:
    excerpt = bound_excerpt_text(" ".join(f"cached{index}" for index in range(40)))
    assert excerpt is not None
    assert len(WORD_RE.findall(excerpt)) == 10
