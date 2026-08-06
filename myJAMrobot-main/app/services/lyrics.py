from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.services.lyrics_cache import lyrics_snippet_cache_service

logger = logging.getLogger(__name__)
LRCLIB_API_URL = "https://lrclib.net/api"
LYRICS_OVH_API_URL = "https://api.lyrics.ovh/v1"
LRCLIB_USER_AGENT = "myJAMrobot/11.0 (short-excerpts)"
LYRICS_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_LYRICS_CHARS = 16_000
MAX_EXCERPT_WORDS = 10
POSITIVE_TTL_SECONDS = 24 * 3600
NEGATIVE_TTL_SECONDS = 90

_LRC_TIMESTAMP_RE = re.compile(r"\[[0-9:.]+]")
_SECTION_RE = re.compile(r"^\s*\[(?:chorus|refrain|refrão|hook)(?:[^]]*)]\s*$", re.IGNORECASE)
_ANY_SECTION_RE = re.compile(r"^\s*\[[^]]+]\s*$")
_WORD_RE = re.compile(r"[^\W_]+(?:[’'-][^\W_]+)*", re.UNICODE)
_VERSION_RE = re.compile(
    r"\s*[-–—]\s*(?:remaster(?:ed)?|live|radio edit|single version|mono|stereo|remix|acoustic|version).*$",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"\s*[([][^)\]]*[)\]]")
_FEAT_RE = re.compile(r"\s*(?:[([]\s*)?(?:feat\.?|ft\.?|featuring|with)\b.*$", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class LyricExcerpt:
    text: str
    source: str
    source_url: str
    selection_kind: str = "excerpt"

    @property
    def label(self) -> str:
        return "Trecho de refrão" if self.selection_kind == "chorus" else "Trecho curto"


def _clean_title(value: str) -> str:
    text = _VERSION_RE.sub("", str(value or "").strip())
    text = _FEAT_RE.sub("", _BRACKET_RE.sub("", text))
    return re.sub(r"\s+", " ", text).strip()


def _clean_artist(value: str) -> str:
    text = _FEAT_RE.sub("", str(value or "").strip())
    for separator in (",", " & ", " x ", " + "):
        if separator in text:
            text = text.split(separator, 1)[0]
    return re.sub(r"\s+", " ", text).strip()


def _line_key(value: str) -> str:
    return " ".join(match.group(0).casefold() for match in _WORD_RE.finditer(value))


def _limit_words(value: str, maximum: int = MAX_EXCERPT_WORDS) -> str | None:
    text = re.sub(r"[ \t]+", " ", str(value or "")).strip()
    matches = list(_WORD_RE.finditer(text))
    if not matches:
        return None
    if len(matches) > maximum:
        text = text[: matches[maximum - 1].end()]
    return text.strip(" \n\t,;:—–-") or None


def bound_excerpt_text(value: str) -> str | None:
    """Apply the public-response ceiling at any output boundary."""
    return _limit_words(value, MAX_EXCERPT_WORDS)


def select_lyric_excerpt(lyrics: str) -> tuple[str | None, str]:
    """Return a bounded excerpt and whether chorus evidence was found."""
    if not lyrics:
        return None, "excerpt"
    lines = [line.strip() for line in lyrics.replace("\r\n", "\n").replace("\r", "\n").split("\n")]

    # Explicit section labels are the strongest signal.
    for index, line in enumerate(lines):
        if not _SECTION_RE.fullmatch(line):
            continue
        selected: list[str] = []
        for candidate in lines[index + 1 :]:
            if _ANY_SECTION_RE.fullmatch(candidate):
                break
            if candidate:
                selected.append(candidate)
            elif selected:
                break
        excerpt = _limit_words("\n".join(selected))
        if excerpt:
            return excerpt, "chorus"

    stanzas: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line and not _ANY_SECTION_RE.fullmatch(line):
            current.append(line)
        elif current:
            stanzas.append(current)
            current = []
    if current:
        stanzas.append(current)
    if not stanzas:
        return None, "excerpt"

    stanza_keys = ["\n".join(filter(None, (_line_key(line) for line in stanza))) for stanza in stanzas]
    stanza_counts = Counter(key for key in stanza_keys if key)
    repeated = {key for key, count in stanza_counts.items() if count >= 2}
    if repeated:
        best_key = max(repeated, key=lambda key: (stanza_counts[key], len(key)))
        return _limit_words("\n".join(stanzas[stanza_keys.index(best_key)])), "chorus"

    line_counts = Counter(_line_key(line) for stanza in stanzas for line in stanza if _line_key(line))
    repeated_lines = {key for key, count in line_counts.items() if count >= 2}
    if repeated_lines:
        hook = max(repeated_lines, key=lambda key: (line_counts[key], len(key)))
        for stanza in stanzas:
            if hook in {_line_key(line) for line in stanza}:
                return _limit_words("\n".join(stanza)), "chorus"

    # Provider data sometimes omits section labels and repetitions. The first
    # stanza is the deterministic low-confidence fallback, still only 10 words.
    return _limit_words("\n".join(stanzas[0])), "excerpt"


def extract_chorus_excerpt(lyrics: str) -> str | None:
    """Backward-compatible text-only view of :func:`select_lyric_excerpt`."""
    return select_lyric_excerpt(lyrics)[0]


extract_snippet = extract_chorus_excerpt


def _lyrics_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict) or payload.get("instrumental") is True:
        return None
    plain = payload.get("plainLyrics")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()[:MAX_LYRICS_CHARS]
    synced = payload.get("syncedLyrics")
    if isinstance(synced, str) and synced.strip():
        clean = "\n".join(_LRC_TIMESTAMP_RE.sub("", line).strip() for line in synced.splitlines())
        return clean.strip()[:MAX_LYRICS_CHARS] or None
    return None


class LyricsService:
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._slots = asyncio.Semaphore(4)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=LYRICS_TIMEOUT_SECONDS, follow_redirects=False)
        return self._http

    async def shutdown(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _json_get(self, url: str, *, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        try:
            async with self._slots:
                async with self._client().stream("GET", url, params=params, headers=headers) as response:
                    if response.status_code != 200:
                        return response.status_code, None
                    total = 0
                    chunks: list[bytes] = []
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            logger.warning("LYRICS_RESPONSE_REJECTED reason=oversize")
                            return 413, None
                        chunks.append(chunk)
            return 200, httpx.Response(200, content=b"".join(chunks)).json()
        except Exception:
            return 0, None

    async def _from_lrclib(self, artist: str, title: str) -> str | None:
        headers = {"User-Agent": LRCLIB_USER_AGENT}
        status, payload = await self._json_get(
            f"{LRCLIB_API_URL}/get",
            params={"artist_name": artist, "track_name": title},
            headers=headers,
        )
        if status == 200:
            lyrics = _lyrics_from_payload(payload)
            if lyrics:
                return lyrics
        status, payload = await self._json_get(
            f"{LRCLIB_API_URL}/search",
            params={"artist_name": artist, "track_name": title},
            headers=headers,
        )
        if status == 200 and isinstance(payload, list):
            for item in payload[:8]:
                lyrics = _lyrics_from_payload(item)
                if lyrics:
                    return lyrics
        return None

    async def _from_lyrics_ovh(self, artist: str, title: str) -> str | None:
        status, payload = await self._json_get(
            f"{LYRICS_OVH_API_URL}/{quote(artist, safe='')}/{quote(title, safe='')}"
        )
        if status != 200 or not isinstance(payload, dict):
            return None
        lyrics = payload.get("lyrics")
        return lyrics.strip()[:MAX_LYRICS_CHARS] if isinstance(lyrics, str) and lyrics.strip() else None

    async def get_excerpt(self, artist: str, title: str) -> LyricExcerpt | None:
        artist = str(artist or "").strip()[:300]
        title = str(title or "").strip()[:300]
        if not artist or not title:
            return None
        cached = await lyrics_snippet_cache_service.get(artist, title)
        if cached is not None:
            if not cached.snippet:
                return None
            safe_snippet = bound_excerpt_text(cached.snippet)
            if not safe_snippet:
                return None
            source_parts = str(cached.source or "").split(":", 1)
            source = source_parts[0] if source_parts[0] in {"lrclib", "lyrics.ovh"} else "lrclib"
            selection_kind = source_parts[1] if len(source_parts) == 2 and source_parts[1] == "chorus" else "excerpt"
            return LyricExcerpt(safe_snippet, source, self._source_url(source), selection_kind)

        clean_artist, clean_title = _clean_artist(artist), _clean_title(title)
        candidates: list[tuple[str, str]] = []
        for pair in ((clean_artist, clean_title), (artist, title)):
            if pair[0] and pair[1] and pair not in candidates:
                candidates.append(pair)

        excerpt: str | None = None
        source: str | None = None
        selection_kind = "excerpt"
        for candidate_artist, candidate_title in candidates:
            lyrics = await self._from_lrclib(candidate_artist, candidate_title)
            if lyrics:
                excerpt, selection_kind = select_lyric_excerpt(lyrics)
                if excerpt:
                    source = "lrclib"
            if not excerpt:
                lyrics = await self._from_lyrics_ovh(candidate_artist, candidate_title)
                if lyrics:
                    excerpt, selection_kind = select_lyric_excerpt(lyrics)
                    if excerpt:
                        source = "lyrics.ovh"
            if excerpt:
                break
        await lyrics_snippet_cache_service.put(
            artist=artist,
            title=title,
            snippet=excerpt,
            source=f"{source}:{selection_kind}" if source else None,
            ttl_seconds=POSITIVE_TTL_SECONDS if excerpt else NEGATIVE_TTL_SECONDS,
        )
        if not excerpt or not source:
            return None
        return LyricExcerpt(excerpt, source, self._source_url(source), selection_kind)

    async def get_snippet(self, artist: str, title: str) -> str | None:
        result = await self.get_excerpt(artist, title)
        return result.text if result else None

    @staticmethod
    def _source_url(source: str) -> str:
        return "https://lrclib.net" if source == "lrclib" else "https://lyrics.ovh"


lyrics_service = LyricsService()
