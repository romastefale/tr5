from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models.lyrics_snippet_cache import LyricsSnippetCache
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SNIPPET_WORD_RE = re.compile(r"[^\W_]+(?:[’'-][^\W_]+)*", re.UNICODE)
_MAX_SNIPPET_WORDS = 10


def _bounded_snippet(value: str | None) -> str | None:
    text = _WS_RE.sub(" ", str(value or "")).strip()
    words = list(_SNIPPET_WORD_RE.finditer(text))
    if not words:
        return None
    if len(words) > _MAX_SNIPPET_WORDS:
        text = text[: words[_MAX_SNIPPET_WORDS - 1].end()]
    return text.strip(" ,;:—–-") or None


def normalize_lyrics_key_part(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", text)).strip()


def build_lyrics_cache_key(artist: str | None, title: str | None) -> str | None:
    artist_norm = normalize_lyrics_key_part(artist)
    title_norm = normalize_lyrics_key_part(title)
    if not artist_norm or not title_norm:
        return None
    digest = hashlib.sha256(f"{artist_norm}\0{title_norm}".encode()).hexdigest()[:40]
    return f"lyr:{digest}"


@dataclass(slots=True, frozen=True)
class LyricsCacheHit:
    snippet: str | None
    source: str | None
    negative: bool = False


class LyricsSnippetCacheService:
    async def get(self, artist: str, title: str) -> LyricsCacheHit | None:
        key = build_lyrics_cache_key(artist, title)
        if not key:
            return None
        try:
            with SessionLocal() as db:
                row = db.get(LyricsSnippetCache, key)
                if not row:
                    return None
                if row.expires_at <= utcnow_naive():
                    db.delete(row)
                    db.commit()
                    return None
                return LyricsCacheHit(row.snippet, row.source, negative=not bool(row.snippet))
        except Exception:
            logger.warning("LYRICS_CACHE_READ_FAILED key=%s", key)
            return None

    async def put(
        self,
        *,
        artist: str,
        title: str,
        snippet: str | None,
        source: str | None,
        ttl_seconds: int,
    ) -> None:
        key = build_lyrics_cache_key(artist, title)
        if not key:
            return
        now = utcnow_naive()
        values = {
            "artist_norm": normalize_lyrics_key_part(artist),
            "title_norm": normalize_lyrics_key_part(title),
            "artist": str(artist)[:300],
            "title": str(title)[:300],
            "snippet": _bounded_snippet(snippet),
            "source": (source or "negative").strip()[:64],
            "expires_at": now + timedelta(seconds=max(1, int(ttl_seconds))),
        }
        try:
            with SessionLocal() as db:
                try:
                    row = db.get(LyricsSnippetCache, key)
                    if row:
                        for name, value in values.items():
                            setattr(row, name, value)
                        row.updated_at = now
                    else:
                        db.add(LyricsSnippetCache(cache_key=key, created_at=now, updated_at=now, **values))
                    db.commit()
                except IntegrityError:
                    db.rollback()
        except Exception:
            logger.warning("LYRICS_CACHE_WRITE_FAILED key=%s", key)


lyrics_snippet_cache_service = LyricsSnippetCacheService()
