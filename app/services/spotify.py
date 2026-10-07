from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from app.services.spotify_links import extract_spotify_track_id, first_spotify_url, is_allowed_spotify_short_url

import httpx

from app.config.settings import (
    HTTP_TIMEOUT_SECONDS,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_SCOPES,
    TELEGRAM_BOT_TOKEN,
)
from app.db.database import SessionLocal
from app.models.spotify_token import SpotifyToken
from app.services.ops_control import release_legacy_after_login
from app.utils.datetime import utcnow_naive as _utcnow_naive

logger = logging.getLogger(__name__)


_TRACK_SEARCH_CACHE_MAX = 4096
_TRACK_SEARCH_TTL_HIT = timedelta(hours=24)
_TRACK_SEARCH_TTL_MISS = timedelta(hours=2)

# OAuth state HMAC: assina (user_id, expiry) com TELEGRAM_BOT_TOKEN como
# segredo. Impede que um atacante forje um link de callback usando o
# user_id de outra pessoa (account-hijack via state guessing).
_STATE_TTL_SECONDS = 600
_STATE_SIG_LEN = 16


def _state_secret() -> bytes | None:
    """Segredo HMAC pro state do OAuth. Sem TELEGRAM_BOT_TOKEN configurado,
    retorna None — `build_auth_url`/`resolve_user_id_from_state` recusam
    operar pra evitar segredo previsível/forjável."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    return TELEGRAM_BOT_TOKEN.encode()


def _sanitize_token_error(data: Any) -> dict[str, Any]:
    """Extrai só campos seguros (error/error_description) do payload
    de erro do Spotify. Evita vazar access_token/refresh_token em log
    caso a API devolva resposta híbrida."""
    if not isinstance(data, dict):
        return {"raw_type": type(data).__name__}
    return {
        k: data.get(k)
        for k in ("error", "error_description", "message", "status")
        if k in data
    }


class SpotifyService:
    def __init__(self) -> None:
        self._client_access_token: str | None = None
        self._client_token_expiration: datetime | None = None
        # Cache (artist_lower, title_lower) -> (url_or_None, expires_at).
        # Negative results são cacheados com TTL menor (faixas raras /
        # ambíguas evitam bater na Search API toda execução).
        # value = (record_or_None, expires_at). record = {"url": str, "cover": str|None}
        self._track_search_cache: dict[
            tuple[str, str], tuple[dict[str, str | None] | None, datetime]
        ] = {}
        # Sprint 4 (S4.1): pool httpx compartilhado. Antes cada chamada
        # criava um AsyncClient novo (TCP+TLS handshake do zero ~200ms p/
        # accounts.spotify.com e api.spotify.com). Agora keepalive global:
        # /tnow + search_track ficam muito mais rápidos em cadeia. Lazy
        # init pra não tocar o loop antes do startup.
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
        return self._http

    async def shutdown(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                logger.exception("Spotify httpx pool close failed")
            self._http = None
        logger.info("Spotify service stopped.")

    def _client_token_valid(self) -> bool:
        return bool(
            self._client_access_token
            and self._client_token_expiration
            and self._client_token_expiration > _utcnow_naive() + timedelta(seconds=60)
        )

    def build_auth_url(self, user_id: int) -> str:
        secret = _state_secret()
        if secret is None:
            logger.error("Spotify OAuth: TELEGRAM_BOT_TOKEN ausente — login bloqueado")
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN não configurado — Spotify login indisponível."
            )
        expiry = int(time.time()) + _STATE_TTL_SECONDS
        payload = f"{user_id}.{expiry}"
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:_STATE_SIG_LEN]
        state = f"{payload}.{sig}"
        return (
            "https://accounts.spotify.com/authorize"
            f"?client_id={SPOTIFY_CLIENT_ID}"
            "&response_type=code"
            f"&redirect_uri={SPOTIFY_REDIRECT_URI}"
            f"&scope={quote(SPOTIFY_SCOPES)}"
            f"&state={state}"
        )

    def resolve_user_id_from_state(self, state: str) -> int | None:
        if not state:
            return None
        secret = _state_secret()
        if secret is None:
            logger.error("Spotify OAuth: TELEGRAM_BOT_TOKEN ausente — state recusado")
            return None
        try:
            user_str, expiry_str, sig = state.split(".", 2)
            expiry = int(expiry_str)
        except (ValueError, AttributeError):
            return None
        if expiry < int(time.time()):
            logger.warning("Spotify OAuth state expired")
            return None
        expected = hmac.new(
            secret,
            f"{user_str}.{expiry_str}".encode(),
            hashlib.sha256,
        ).hexdigest()[:_STATE_SIG_LEN]
        if not hmac.compare_digest(expected, sig):
            logger.warning("Spotify OAuth state signature mismatch")
            return None
        try:
            return int(user_str)
        except ValueError:
            return None

    async def exchange_code_for_token(self, code: str, user_id: int) -> bool | None:
        """Troca `code` por tokens e salva. Retorna:

        * `True`  — substituiu um login anterior do mesmo user_id
        * `False` — primeira conexão
        * `None`  — Spotify devolveu resposta inválida (nada foi gravado)
        """
        auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()

        client = self._client()
        response = await client.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": SPOTIFY_REDIRECT_URI,
            },
            headers={
                "Authorization": f"Basic {b64_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        data = response.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in")

        if not access_token or not expires_in:
            logger.error("Invalid Spotify token response: %s", _sanitize_token_error(data))
            return None

        expiration = _utcnow_naive() + timedelta(seconds=int(expires_in))
        replaced = False
        with SessionLocal() as db:
            existing = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if existing:
                replaced = True
                existing.access_token = access_token
                existing.expiration = expiration
                existing.updated_at = _utcnow_naive()
                if refresh_token:
                    existing.refresh_token = refresh_token
            else:
                db.add(
                    SpotifyToken(
                        user_id=user_id,
                        access_token=access_token,
                        refresh_token=refresh_token or "",
                        expiration=expiration,
                        created_at=_utcnow_naive(),
                        updated_at=_utcnow_naive(),
                    )
                )
            db.commit()
        release_legacy_after_login(user_id, source="spotify")
        return replaced

    async def _refresh_token(self, user_id: int) -> SpotifyToken | None:
        with SessionLocal() as db:
            token = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if not token or not token.refresh_token:
                return None

            auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()

            client = self._client()
            response = await client.post(
                "https://accounts.spotify.com/api/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                },
                headers={
                    "Authorization": f"Basic {b64_auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

            data = response.json()
            access_token = data.get("access_token")
            expires_in = data.get("expires_in")
            if not access_token or not expires_in:
                logger.error("Spotify refresh failed: %s", _sanitize_token_error(data))
                return None

            token.access_token = access_token
            token.expiration = _utcnow_naive() + timedelta(seconds=int(expires_in))
            token.updated_at = _utcnow_naive()
            db.commit()
            db.refresh(token)
            return token

    async def get_current_or_last_played(self, user_id: int) -> dict[str, Any] | None:
        with SessionLocal() as db:
            token = db.query(SpotifyToken).filter_by(user_id=user_id).first()

        if not token:
            return None

        async def fetch_current(access_token: str) -> httpx.Response:
            client = self._client()
            return await client.get(
                "https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        async def fetch_recent(access_token: str) -> httpx.Response:
            client = self._client()
            return await client.get(
                "https://api.spotify.com/v1/me/player/recently-played?limit=1",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        response = await fetch_current(token.access_token)
        if response.status_code == 401:
            refreshed = await self._refresh_token(user_id)
            if refreshed:
                response = await fetch_current(refreshed.access_token)

        if response.status_code == 200:
            data = response.json()
            item = data.get("item")
            if item:
                mapped = self._map_track(item, source="spotify_current", played_at=None)
                if mapped is not None:
                    # Spotify pode responder 200 com is_playing=false quando o
                    # usuário pausou. Propagamos a flag e preservamos o timestamp
                    # da mudança de estado para consumidores como /tnow exibirem
                    # a última música recente com cor de recência.
                    mapped["is_playing"] = bool(data.get("is_playing", True))
                    timestamp_ms = data.get("timestamp")
                    mapped["player_timestamp_ms"] = timestamp_ms
                    if not mapped["is_playing"] and timestamp_ms:
                        try:
                            mapped["played_at"] = datetime.fromtimestamp(
                                int(timestamp_ms) / 1000,
                                tz=timezone.utc,
                            ).isoformat().replace("+00:00", "Z")
                        except Exception:
                            logger.debug("Spotify current timestamp parse failed: %s", timestamp_ms, exc_info=True)
                return mapped

        recent = await fetch_recent(token.access_token)
        if recent.status_code == 401:
            refreshed = await self._refresh_token(user_id)
            if refreshed:
                recent = await fetch_recent(refreshed.access_token)

        if recent.status_code != 200:
            logger.error("Spotify recent error: %s", recent.text)
            return None

        items = recent.json().get("items") or []
        if not items:
            return None

        return self._map_track(
            items[0].get("track") or {},
            source="spotify_last",
            played_at=items[0].get("played_at"),
        )

    async def _get_client_credentials_token(self) -> str | None:
        if self._client_token_valid():
            return self._client_access_token
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            logger.error("Spotify client credentials are not configured.")
            return None

        auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()

        client = self._client()
        response = await client.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {b64_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        if response.status_code != 200:
            logger.error(
                "Spotify client credentials token failed: status=%s",
                response.status_code,
            )
            return None

        data = response.json()
        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not access_token or not expires_in:
            logger.error(
                "Invalid Spotify client credentials response: %s",
                _sanitize_token_error(data),
            )
            return None

        self._client_access_token = str(access_token)
        self._client_token_expiration = _utcnow_naive() + timedelta(seconds=int(expires_in))
        return self._client_access_token

    async def get_track_by_id(self, track_id: str, market: str | None = "BR") -> dict[str, Any] | None:
        clean_track_id = (track_id or "").strip()
        if not clean_track_id:
            return None

        access_token = await self._get_client_credentials_token()
        if not access_token:
            return None

        params = {"market": market} if market else None
        client = self._client()
        response = await client.get(
            f"https://api.spotify.com/v1/tracks/{clean_track_id}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if response.status_code != 200:
            logger.error("Spotify track lookup failed: status=%s body=%s", response.status_code, response.text)
            return None

        return self._map_track(response.json(), source="spotify_link", played_at=None)

    async def resolve_track_from_shared_link(self, raw_link: str | None) -> dict[str, Any] | None:
        """Resolve uma faixa a partir de link/URI compartilhado do Spotify.

        Não busca hosts arbitrários: URLs curtas só são seguidas quando o
        domínio inicial está na allow-list do Spotify. O resultado final só é
        aceito quando resolve para open.spotify.com/track/{id}.
        """
        raw = (raw_link or "").strip()
        if not raw:
            return None

        track_id = extract_spotify_track_id(raw)
        if track_id:
            return await self.get_track_by_id(track_id, market="BR")

        if not is_allowed_spotify_short_url(raw):
            return None

        url = first_spotify_url(raw)
        if not url or len(url) > 2048:
            return None

        try:
            client = self._client()
            response = await client.get(url, follow_redirects=True)
            final_url = str(response.url)
        except Exception:
            logger.warning("Spotify shared link resolve failed", exc_info=True)
            return None

        track_id = extract_spotify_track_id(final_url)
        if not track_id:
            logger.warning("Spotify shared link did not resolve to track URL: %s", final_url[:160])
            return None
        return await self.get_track_by_id(track_id, market="BR")

    def _map_track(self, item: dict[str, Any], source: str, played_at: str | None) -> dict[str, Any] | None:
        if not item:
            return None
        album = item.get("album") or {}
        artists = item.get("artists") or []
        artist = artists[0].get("name") if artists else ""
        images = album.get("images") or []
        return {
            "source": source,
            "played_at": played_at,
            "track_name": item.get("name") or "",
            "artist": artist,
            "album": album.get("name") or "",
            "album_name": album.get("name") or "",
            "track_id": item.get("id"),
            "spotify_url": (item.get("external_urls") or {}).get("spotify"),
            "album_url": (album.get("external_urls") or {}).get("spotify"),
            "album_image_url": images[0].get("url") if images else None,
            "preview_url": item.get("preview_url"),
            "duration_ms": item.get("duration_ms"),
        }

    async def _get_web_player_token(self) -> str | None:
        """Token do web player autenticado pelo cookie sp_dc, ou None.

        Fonte única: o serviço de Canvas (TOTP + cookie + backoff de 403).
        Import tardio só para manter spotify.py sem dependência no import.
        """
        try:
            from app.services.spotify_canvas import spotify_canvas_service

            return await spotify_canvas_service.get_web_access_token()
        except Exception:
            logger.exception("Spotify web-player token lookup failed")
            return None

    async def _search_tracks_request(self, client: httpx.AsyncClient, token: str, query: str):
        return await client.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": "track", "limit": 1, "market": "BR"},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def search_track(self, artist: str, title: str) -> dict[str, str | None] | None:
        """Resolve artist+title -> {url, cover} via Spotify Search API.

        Usa Client Credentials (app-only auth), portanto NÃO requer que o
        usuário esteja logado no Spotify. Retorna um dict
        `{"url": "https://open.spotify.com/track/{id}", "cover": "...640px..."}`
        ou None quando não há match / API indisponível. URL e capa vêm
        no MESMO payload (sem chamadas extras). Resultados cacheados em
        memória com TTL — o ganho de capa é "de graça" depois do link.
        """
        a = (artist or "").strip()
        t = (title or "").strip()
        if not a or not t:
            return None
        key = (a.lower(), t.lower())
        now = _utcnow_naive()
        cached = self._track_search_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

        # Credencial preferida: app-only (Client Credentials). Se ela não existir
        # ou o Spotify recusar a busca (401/403), tenta UMA vez com o token do
        # web player (cookie sp_dc), o mesmo que o serviço de Canvas já usa.
        token = await self._get_client_credentials_token()
        used_web_token = False
        if not token:
            token = await self._get_web_player_token()
            used_web_token = bool(token)
        if not token:
            return None

        # Operadores `track:` e `artist:` com aspas restringem o match aos
        # campos exatos, reduzindo falso-positivos com títulos genéricos.
        query = f'track:"{t}" artist:"{a}"'
        record: dict[str, str | None] | None = None
        try:
            client = self._client()
            resp = await self._search_tracks_request(client, token, query)
            if resp.status_code in (401, 403) and not used_web_token:
                first_status = resp.status_code
                web_token = await self._get_web_player_token()
                if web_token:
                    resp = await self._search_tracks_request(client, web_token, query)
                    logger.info(
                        "Spotify search retried with web-player token | first_status=%s | status=%s",
                        first_status, resp.status_code,
                    )
            if resp.status_code == 200:
                items = ((resp.json().get("tracks") or {}).get("items") or [])
                if items:
                    item = items[0]
                    url = (item.get("external_urls") or {}).get("spotify")
                    images = (item.get("album") or {}).get("images") or []
                    # images[0] = maior resolução (640px) por convenção da API.
                    cover = images[0].get("url") if images else None
                    if url:
                        record = {"id": item.get("id"), "url": url, "cover": cover}
            else:
                logger.warning(
                    "Spotify search non-200 | status=%s | artist=%s | title=%s",
                    resp.status_code, a, t,
                )
                # Falha de API/autorização NÃO é "faixa inexistente": não cacheia
                # como miss (senão um 403 trava a faixa por _TRACK_SEARCH_TTL_MISS).
                return None
        except Exception:
            logger.exception(
                "Spotify search request failed | artist=%s | title=%s", a, t
            )
            # Não cacheia erros de rede para tentar de novo logo.
            return None

        ttl = _TRACK_SEARCH_TTL_HIT if record else _TRACK_SEARCH_TTL_MISS
        self._track_search_cache[key] = (record, now + ttl)
        # Bound do cache: se exceder o limite, descarta os 25% mais antigos.
        if len(self._track_search_cache) > _TRACK_SEARCH_CACHE_MAX:
            oldest = sorted(self._track_search_cache.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[: len(oldest) // 4]:
                self._track_search_cache.pop(k, None)
        return record

    async def clear_user_session(self, user_id: int) -> bool:
        with SessionLocal() as db:
            token = db.query(SpotifyToken).filter_by(user_id=user_id).first()
            if token:
                db.delete(token)
                db.commit()
        return True


spotify_service = SpotifyService()
