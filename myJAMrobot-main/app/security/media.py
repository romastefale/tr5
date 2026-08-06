from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

_HOSTS_BY_KIND: dict[str, tuple[str, ...]] = {
    "cover": (
        "i.scdn.co",
        "cdn-images.dzcdn.net",
        "lastfm.freetls.fastly.net",
        "lastfm-img2.akamaized.net",
    ),
    "canvas": ("canvaz.scdn.co",),
    "preview": ("p.scdn.co",),
}
_SEARCH_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(slots=True, frozen=True)
class DownloadedMedia:
    data: bytes
    content_type: str
    final_url: str


def sanitize_search_term(value: str, *, maximum: int = 120) -> str:
    clean = _SEARCH_CONTROL_RE.sub(" ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:maximum].strip()


def _allowed_host(host: str, kind: str) -> bool:
    allowed = _HOSTS_BY_KIND.get(kind, ())
    return any(host == candidate for candidate in allowed)


def validate_media_url(value: str | None, *, kind: str) -> str | None:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or port not in {None, 443} or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if not _allowed_host(host, kind):
        return None
    return raw


async def download_media(
    value: str | None,
    *,
    kind: str,
    max_bytes: int,
    accepted_types: tuple[str, ...],
    timeout_seconds: float = 10.0,
    max_redirects: int = 3,
) -> DownloadedMedia | None:
    current = validate_media_url(value, kind=kind)
    if not current:
        return None
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        next_url = validate_media_url(urljoin(current, location or ""), kind=kind)
                        if not next_url:
                            return None
                        current = next_url
                        continue
                    if response.status_code != 200:
                        return None
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in accepted_types:
                        return None
                    try:
                        declared = int(response.headers.get("content-length") or 0)
                    except ValueError:
                        declared = 0
                    if declared > max_bytes:
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            return None
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        return None
                    return DownloadedMedia(data=data, content_type=content_type, final_url=current)
            except (httpx.HTTPError, ValueError):
                return None
    return None
