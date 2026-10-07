from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
import weakref

import httpx

from app.config.settings import (
    SPOTIFY_CANVAS_ENABLED,
    SPOTIFY_CANVAS_SP_DC,
    SPOTIFY_CANVAS_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# Token anônimo do web player dura ~1h; usamos 50min p/ margem de segurança.
CANVAS_TOKEN_TTL_SECONDS = 50 * 60
# Canvas URL pra um track muda raramente; 24h de cache reduz drasticamente
# o tráfego pro canvaz-cache mas ainda permite refresh diário.
CANVAS_URL_CACHE_TTL_SECONDS = 24 * 3600
# Cache curto pra "miss confiável" (canvasdownloader retornou página
# "Canvas not found"). Como o proxy dá MUITO false negative, não pode
# cachear 24h — em 1h tentamos de novo (incluindo via TOTP/sp_dc).
CANVAS_URL_NEGATIVE_TTL_SECONDS = 1 * 3600
# Quando o endpoint de token do Spotify devolve 403 (IP bloqueado), não
# vale a pena retentar a cada request — ficamos em backoff por 10min.
CANVAS_TOKEN_BACKOFF_SECONDS = 10 * 60
# Canvas é vertical 720x1280 H.264, raramente passa de 2MB. 8MB já é teto
# bem folgado — qualquer coisa maior é provavelmente bug e a gente aborta.
CANVAS_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024
CANVAS_DOWNLOAD_TIMEOUT_SECONDS = 10.0

# Endpoint único do web player (cookie + anon usam o mesmo). Espera os
# params TOTP-signed (reason=init, productType=web-player COM HÍFEN,
# totp, totpServer, totpVer). Validado ao vivo em 2026: a versão SEM
# TOTP retorna 403 de datacenter; com TOTP correto retorna 200.
CANVAS_TOKEN_URL = "https://open.spotify.com/api/token"
CANVAS_API_URL = "https://spclient.wg.spotify.com/canvaz-cache/v0/canvases"
CANVAS_URL_RE = re.compile(rb"https://canvaz\.scdn\.co/[^\x00\s\"'<>]+")

# Fonte dinâmica do secret TOTP. O Spotify rotaciona o segredo ~a cada
# poucos meses; o repo `thereallo/totp-secrets` (usado pelo glomatico/votify)
# mantém o dicionário versionado atualizado. A gente faz fetch + cacheia
# por 24h. Se a fonte cair, usa o fallback hardcoded (última versão
# conhecida estável, validada ao vivo).
TOTP_SECRETS_URL = (
    "https://git.gay/thereallo/totp-secrets/raw/branch/main/secrets/secretDict.json"
)
TOTP_SECRETS_TTL_SECONDS = 24 * 3600
TOTP_SECRETS_FALLBACK: dict[str, list[int]] = {
    # Versão 61 — verificada ao vivo no Replit em 2026 contra o endpoint
    # /api/token: derivada via XOR (i%33)+9, gerou token 200 OK.
    "61": [
        44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76,
        94, 102, 43, 69, 49, 120, 118, 80, 64, 78,
    ],
}
TOTP_PERIOD = 30
TOTP_DIGITS = 6

# Proxy terceirizado: canvasdownloader.com já tem IP residencial / parceria
# com o Spotify pra atravessar o bloqueio de datacenter. Devolve HTML com
# <video src="https://canvaz.scdn.co/...">. A gente regexa o src e baixa
# direto do CDN oficial (mesmo SSRF guard de antes).
#
# Trade-offs aceitos (estudados):
# - É um terceiro: se ele cair, /tcanvas cai pra /playing (fallback igual hoje)
# - Cloudflare na frente: User-Agent realista + ~50 req/dia esperado = bem abaixo
#   de qualquer limite razoável. Sem captcha visível em 2026
# - Privacidade: a gente expõe pra ele só o track_id público do Spotify
#   (mesmo dado que o user vê na URL). Sem user_id, sem token nosso
# - Cache de 24h por track_id (já existente) reduz drasticamente a frequência
CANVASDOWNLOADER_URL = "https://www.canvasdownloader.com/canvas"
CANVASDOWNLOADER_TIMEOUT_SECONDS = 8.0
CANVASDOWNLOADER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8",
    "Referer": "https://www.canvasdownloader.com/",
}
# Marker que o canvasdownloader.com cospe quando ele NÃO acha o Canvas.
# Validado ao vivo: várias músicas populares (Blinding Lights etc) caem nessa
# página mesmo tendo Canvas no app — false negative. Por isso o cache pra
# "miss" do proxy é curto (1h) e a gente tenta camadas seguintes.
CANVASDOWNLOADER_NOT_FOUND_MARKER = "Canvas not found"
TOKEN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
CANVAS_HEADERS = {
    "Accept": "application/protobuf",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Language": "en",
    "User-Agent": "Spotify/8.5.49 iOS/Version 13.3.1 (Build 17D50)",
    "Accept-Encoding": "gzip, deflate, br",
}


def _encode_varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _encode_length_delimited(field_number: int, payload: bytes) -> bytes:
    tag = (field_number << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(payload)) + payload


def _encode_canvas_request(track_id: str) -> bytes:
    track_uri = f"spotify:track:{track_id}".encode()
    inner = _encode_length_delimited(1, track_uri)
    outer = _encode_length_delimited(1, inner)
    return outer


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("Incomplete varint")


def _iter_length_delimited_fields(data: bytes):
    offset = 0
    while offset < len(data):
        try:
            tag, offset = _decode_varint(data, offset)
        except ValueError:
            return
        field_number = tag >> 3
        wire_type = tag & 7
        if wire_type == 2:
            try:
                length, offset = _decode_varint(data, offset)
            except ValueError:
                return
            payload = data[offset : offset + length]
            offset += length
            yield field_number, payload
        elif wire_type == 0:
            try:
                _, offset = _decode_varint(data, offset)
            except ValueError:
                return
        elif wire_type == 1:
            offset += 8
        elif wire_type == 5:
            offset += 4
        else:
            return


def _find_canvas_url_from_protobuf(data: bytes) -> str | None:
    for _, payload in _iter_length_delimited_fields(data):
        if payload.startswith(b"http") and b"canvaz.scdn.co" in payload:
            try:
                return payload.decode()
            except UnicodeDecodeError:
                continue
        nested = _find_canvas_url_from_protobuf(payload)
        if nested:
            return nested
    return None


def _find_canvas_url(data: bytes) -> str | None:
    protobuf_url = _find_canvas_url_from_protobuf(data)
    if protobuf_url:
        return protobuf_url
    match = CANVAS_URL_RE.search(data)
    if not match:
        return None
    try:
        return match.group(0).decode()
    except UnicodeDecodeError:
        return None


class SpotifyCanvasService:
    def __init__(self) -> None:
        # Cache do token anônimo (compartilhado entre requests).
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        # Backoff: se o endpoint de token retorna 403 (IP bloqueado), evita
        # martelar — ficamos em "modo desistir" por CANVAS_TOKEN_BACKOFF_SECONDS.
        self._token_blocked_until: float = 0.0
        # Cache de URL por track_id; armazena `None` como cache negativo pra
        # não martelar o canvaz-cache em músicas que sabidamente não têm Canvas.
        self._url_cache: dict[str, tuple[str | None, float]] = {}
        # Locks POR TRACK (não global): permite que lookups de tracks diferentes
        # rodem em paralelo. Mesma track ainda coalesce (2 users pedindo a
        # mesma faixa = 1 fetch, o 2º espera o 1º terminar e pega do cache).
        # WeakValueDictionary: o lock vive enquanto algum coroutine o segura
        # (o caller faz `async with` mantendo referência forte na sua frame);
        # quando ninguém mais usa, o GC remove a entrada. Sem cleanup manual,
        # sem bound — coalescência fica preservada SEMPRE (nunca cria 2 locks
        # diferentes pra mesma track simultânea).
        self._url_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # Semáforo global de concorrência (B): no máximo N lookups Canvas
        # fazendo trabalho HTTP simultâneo. Protege o proxy canvasdownloader
        # (Cloudflare na frente) e o endpoint canvaz-cache do Spotify de
        # picos. N=3 é o sweet spot: paralelismo OK pra grupo pequeno, mas
        # o 4º pedido em diante espera ~2-4s (latência do anterior) — exatamente
        # o "demorar um pouquinho" desejado. Cache hits NÃO consomem o semáforo.
        self._canvas_concurrency = asyncio.Semaphore(3)
        # Cache do secretDict.json (versão + ciphertext). Atualiza a cada 24h.
        self._totp_version: str | None = None
        self._totp_secret: bytes | None = None
        self._totp_expires_at: float = 0.0
        self._totp_lock = asyncio.Lock()

    async def get_canvas_url(self, track_id: str) -> str | None:
        clean_track_id = (track_id or "").strip()
        if not SPOTIFY_CANVAS_ENABLED:
            logger.info("Spotify Canvas skipped: disabled")
            return None
        if not clean_track_id:
            logger.info("Spotify Canvas skipped: empty track_id")
            return None
        # Defesa: track_id "lfm:<hash>" é hash interno Last.fm, NUNCA
        # resolve no canvaz-cache nem no proxy. O chamador (tcanvas.py)
        # deveria ter resolvido pra Spotify ID base62 antes — se chegou
        # aqui é porque a resolução falhou. Skip rápido pra não poluir
        # log com 403 do proxy.
        if clean_track_id.startswith("lfm:"):
            logger.info(
                "Spotify Canvas skipped: track_id not spotify (got=%s)",
                clean_track_id,
            )
            return None

        # Fast path: cache hit sem lock.
        now = time.time()
        cached = self._url_cache.get(clean_track_id)
        if cached is not None and now < cached[1]:
            return cached[0]

        # Slow path: re-check sob lock POR TRACK pra coalescer fetches
        # duplicados da mesma faixa, sem bloquear tracks diferentes.
        async with self._get_url_lock(clean_track_id):
            now = time.time()
            cached = self._url_cache.get(clean_track_id)
            if cached is not None and now < cached[1]:
                return cached[0]
            # Semáforo global limita N HTTP-bound canvas lookups simultâneos.
            # Adquirido AQUI (depois do cache re-check) pra não bloquear
            # cache hits ou requests coalescidos pela mesma track.
            async with self._canvas_concurrency:
                try:
                    canvas_url: str | None = None
                    # Marca se a resposta negativa veio de uma fonte confiável
                    # (Spotify oficial via token válido) — aí cacheia por 24h.
                    # Senão, cacheia só 1h pra dar chance de tentar de novo logo.
                    negative_is_authoritative = False

                    # CAMADA 1 (PRIMÁRIA quando cookie disponível): Spotify direto
                    # via sp_dc. Hit rate ~99% pra tracks que têm canvas. Se o cookie
                    # não está setado, pula direto pra camada 2 (sem round-trip
                    # inútil — token anônimo retorna vazio garantido).
                    if (
                        SPOTIFY_CANVAS_SP_DC
                        and time.time() >= self._token_blocked_until
                    ):
                        token = await self._get_access_token()
                        if token:
                            canvas_url = await self._fetch_canvas_url(clean_track_id, token)
                            if canvas_url:
                                logger.info(
                                    "Spotify Canvas via TOKEN_DIRECT: track_id=%s",
                                    clean_track_id,
                                )
                            else:
                                # Cookie + token válido + canvas vazio = Spotify
                                # oficial confirmou que essa track não tem canvas.
                                # Negativo autoritativo (cache 24h) — mas ainda
                                # tentamos o proxy abaixo por garantia (custa pouco
                                # e ocasionalmente acha algo que o oficial perdeu).
                                negative_is_authoritative = True

                    # CAMADA 2 (FALLBACK): canvasdownloader.com.
                    # Roda quando o cookie não está setado OU quando o cookie
                    # está mas o canvaz-cache devolveu vazio. ~50% hit rate,
                    # zero credencial. Misses ficam com cache curto (1h) pra
                    # retry; hits viram cache de 24h.
                    if canvas_url is None:
                        canvas_url, _proxy_definitive = await self._fetch_via_canvasdownloader(
                            clean_track_id
                        )
                        if canvas_url:
                            logger.info(
                                "Spotify Canvas via PROXY: track_id=%s", clean_track_id
                            )
                            # Proxy achou algo que o oficial não tinha — invalida o
                            # marker de negativo autoritativo (não cacheia o "não"
                            # do Spotify oficial junto com um "sim" do proxy).
                            negative_is_authoritative = False

                    # Decide TTL do cache:
                    # - Positivo: 24h (Canvas URLs são estáveis)
                    # - Negativo confiável (Spotify direto disse não): 24h
                    # - Negativo não-confiável (só o proxy/falha): 1h pra retry
                    ttl = (
                        CANVAS_URL_CACHE_TTL_SECONDS
                        if canvas_url or negative_is_authoritative
                        else CANVAS_URL_NEGATIVE_TTL_SECONDS
                    )
                    if not canvas_url:
                        logger.info(
                            "Spotify Canvas NOT FOUND: track_id=%s (cache_ttl=%ss authoritative=%s)",
                            clean_track_id,
                            ttl,
                            negative_is_authoritative,
                        )
                    self._url_cache[clean_track_id] = (canvas_url, time.time() + ttl)
                    return canvas_url
                except Exception:
                    logger.exception("Spotify Canvas lookup failed: track_id=%s", clean_track_id)
                    return None

    def _get_url_lock(self, track_id: str) -> asyncio.Lock:
        """Retorna lock dedicado pra essa track. Cria lazy.

        WeakValueDictionary: enquanto algum caller segura o lock via
        `async with`, ele permanece vivo (referência forte na frame). Quando
        ninguém mais usa, o GC remove a entrada automaticamente. Garante:
        - Coalescência estrita: 2 requests simultâneas da MESMA track sempre
          pegam o MESMO objeto lock (a 2ª aumenta refcount e mantém vivo).
        - Sem leak: tracks idle são coletadas naturalmente.
        - Sem bound artificial.
        """
        lock = self._url_locks.get(track_id)
        if lock is None:
            lock = asyncio.Lock()
            self._url_locks[track_id] = lock
        return lock

    async def _fetch_via_canvasdownloader(
        self, track_id: str
    ) -> tuple[str | None, bool]:
        """Resolve Canvas URL via canvasdownloader.com (proxy terceirizado).

        Retorna (url, definitivo_negativo). O segundo bool indica se o proxy
        explicitamente disse "Canvas not found" (página com marker). Mesmo
        assim NÃO é autoritativo — o proxy dá MUITO false negative em
        músicas populares (Blinding Lights, Call Me Maybe testadas). Só
        ajuda a distinguir "erro de rede" de "proxy respondeu não".

        Segurança:
        - Só passa o track_id público (mesmo dado da URL do Spotify)
        - URL extraída é revalidada no download_canvas_bytes (SSRF guard)
        - Timeout fail-fast 8s
        """
        track_url = f"https://open.spotify.com/track/{track_id}"
        try:
            async with httpx.AsyncClient(
                timeout=CANVASDOWNLOADER_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers=CANVASDOWNLOADER_HEADERS,
            ) as client:
                response = await client.get(
                    CANVASDOWNLOADER_URL, params={"link": track_url}
                )
        except Exception:
            logger.warning(
                "Canvas proxy request error: track_id=%s", track_id, exc_info=True
            )
            return None, False
        if response.status_code != 200:
            logger.warning(
                "Canvas proxy non-200: track_id=%s status=%s",
                track_id,
                response.status_code,
            )
            return None, False
        match = CANVAS_URL_RE.search(response.content)
        if match:
            try:
                return match.group(0).decode(), False
            except UnicodeDecodeError:
                logger.warning("Canvas proxy: URL não-utf8 track_id=%s", track_id)
                return None, False
        # Detecta a página "Canvas not found" do proxy.
        is_not_found = CANVASDOWNLOADER_NOT_FOUND_MARKER in response.text
        logger.info(
            "Canvas proxy MISS: track_id=%s proxy_says_not_found=%s",
            track_id,
            is_not_found,
        )
        return None, is_not_found

    async def download_canvas_bytes(self, url: str) -> bytes | None:
        """Baixa o vídeo Canvas pra memória, com teto de tamanho.

        Só aceita URLs do domínio oficial `canvaz.scdn.co` (SSRF guard).
        Retorna `None` em qualquer falha — chamador deve cair pro fallback.
        """
        if not url or not url.startswith("https://canvaz.scdn.co/"):
            logger.warning("Canvas download rejected: bad url=%s", url[:120] if url else None)
            return None
        try:
            async with httpx.AsyncClient(
                timeout=CANVAS_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        logger.warning(
                            "Canvas download failed: status=%s url=%s", response.status_code, url
                        )
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > CANVAS_DOWNLOAD_MAX_BYTES:
                            logger.warning(
                                "Canvas download aborted: oversize (>%s bytes) url=%s",
                                CANVAS_DOWNLOAD_MAX_BYTES,
                                url,
                            )
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except Exception:
            logger.exception("Canvas download error url=%s", url)
            return None

    async def _get_access_token(self) -> str | None:
        # Fast path: token em cache e ainda válido.
        now = time.time()
        if self._token and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            # Re-check sob lock.
            now = time.time()
            if self._token and now < self._token_expires_at:
                return self._token
            token = await self._fetch_access_token()
            if token:
                self._token = token
                self._token_expires_at = now + CANVAS_TOKEN_TTL_SECONDS
            return token

    async def _fetch_access_token(self) -> str | None:
        """Pega token via /api/token TOTP-assinado (com cookie se houver).

        Método único (descoberto em maio/2026 inspecionando glomatico/votify):
        o endpoint correto é `/api/token` com params `reason=init`,
        `productType=web-player` (HÍFEN, não underscore), `totp`, `totpServer`
        e `totpVer` derivados do `secretDict.json` versionado.

        Se SPOTIFY_CANVAS_SP_DC estiver setado, anexa como cookie — o token
        retornado vira `isAnonymous=false` e consegue ler o canvaz-cache.
        Sem cookie, retorna token anônimo (autentica mas canvas vem vazio).

        Em 403 com "URL Blocked" (datacenter sem cookie reconhecido), ativa
        backoff de 10min pra não martelar.
        """
        secret_info = await self._get_totp_secret()
        if not secret_info:
            logger.warning("Spotify Canvas: sem secret TOTP — sem token possível")
            return None
        version, secret_bytes = secret_info
        totp_code = self._generate_totp(secret_bytes)
        params = {
            "reason": "init",
            "productType": "web-player",
            "totp": totp_code,
            "totpServer": totp_code,
            "totpVer": version,
        }
        headers = dict(TOKEN_HEADERS)
        source = "anon"
        if SPOTIFY_CANVAS_SP_DC:
            headers["Cookie"] = f"sp_dc={SPOTIFY_CANVAS_SP_DC}"
            source = "cookie"
        try:
            async with httpx.AsyncClient(
                timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                response = await client.get(
                    CANVAS_TOKEN_URL, params=params, headers=headers
                )
        except Exception:
            logger.exception("Spotify Canvas token request error source=%s", source)
            return None
        if response.status_code == 403 and "URL Blocked" in response.text:
            self._token_blocked_until = time.time() + CANVAS_TOKEN_BACKOFF_SECONDS
            logger.warning(
                "Spotify Canvas token BLOCKED (datacenter IP) source=%s — "
                "backoff %ss. Configure SPOTIFY_CANVAS_SP_DC pra atravessar.",
                source,
                CANVAS_TOKEN_BACKOFF_SECONDS,
            )
            return None
        return self._extract_token(response, source=f"{source}/v{version}")

    async def _get_totp_secret(self) -> tuple[str, bytes] | None:
        """Retorna (version, derived_secret_bytes) com cache de 24h.

        Fast path: cache em memória. Slow path sob lock: faz fetch do
        secretDict.json (votify-style), pega max(version), aplica a função
        de derivação XOR e cacheia. Se o fetch falhar, usa o fallback
        hardcoded — assim a feature nunca quebra por causa de uma fonte
        externa offline.
        """
        now = time.time()
        if self._totp_secret and self._totp_version and now < self._totp_expires_at:
            return self._totp_version, self._totp_secret
        async with self._totp_lock:
            now = time.time()
            if self._totp_secret and self._totp_version and now < self._totp_expires_at:
                return self._totp_version, self._totp_secret
            secrets_dict: dict[str, list[int]] | None = None
            try:
                async with httpx.AsyncClient(
                    timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True
                ) as client:
                    response = await client.get(TOTP_SECRETS_URL)
                if response.status_code == 200:
                    parsed = response.json()
                    if isinstance(parsed, dict) and parsed:
                        secrets_dict = parsed
                        logger.info(
                            "Spotify Canvas TOTP secrets fetched: versions=%s",
                            sorted(parsed.keys(), key=lambda k: int(k)),
                        )
                else:
                    logger.warning(
                        "Spotify Canvas TOTP secrets non-200 status=%s — usando fallback",
                        response.status_code,
                    )
            except Exception:
                logger.warning(
                    "Spotify Canvas TOTP secrets fetch falhou — usando fallback",
                    exc_info=True,
                )
            if not secrets_dict:
                secrets_dict = TOTP_SECRETS_FALLBACK
            try:
                version = max(secrets_dict.keys(), key=lambda k: int(k))
                ciphertext = secrets_dict[version]
                derived = self._derive_totp_secret(ciphertext)
            except (ValueError, TypeError):
                logger.exception("Spotify Canvas TOTP secret invalid")
                return None
            self._totp_version = version
            self._totp_secret = derived
            self._totp_expires_at = now + TOTP_SECRETS_TTL_SECONDS
            return version, derived

    @staticmethod
    def _derive_totp_secret(ciphertext) -> bytes:
        """Algoritmo do glomatico/votify: XOR byte-a-byte com ((i%33)+9),
        concatena os ints decimais como string e encode pra ASCII."""
        return "".join(
            str(int(b) ^ ((i % 33) + 9)) for i, b in enumerate(ciphertext)
        ).encode("ascii")

    @staticmethod
    def _generate_totp(secret: bytes) -> str:
        """TOTP HMAC-SHA1 padrão RFC 6238, period=30, digits=6."""
        counter = int(time.time()) // TOTP_PERIOD
        counter_bytes = counter.to_bytes(8, "big")
        h = hmac.new(secret, counter_bytes, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        binary = (
            (h[offset] & 0x7F) << 24
            | (h[offset + 1] & 0xFF) << 16
            | (h[offset + 2] & 0xFF) << 8
            | (h[offset + 3] & 0xFF)
        )
        return str(binary % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)

    def _extract_token(self, response: httpx.Response, source: str) -> str | None:
        if response.status_code == 403 and "URL Blocked" in response.text:
            # Caso clássico de IP de datacenter bloqueado pelo upstream do Spotify.
            logger.warning(
                "Spotify Canvas token BLOCKED_BY_UPSTREAM (datacenter IP block) source=%s — "
                "configure SPOTIFY_CANVAS_SP_DC com o cookie sp_dc de uma conta logada",
                source,
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "Spotify Canvas token failed: source=%s status=%s body=%s",
                source,
                response.status_code,
                response.text[:200],
            )
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning(
                "Spotify Canvas token failed: non-json response source=%s body=%s",
                source,
                response.text[:200],
            )
            return None
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            logger.warning(
                "Spotify Canvas token failed: token key missing source=%s keys=%s",
                source,
                sorted(data.keys()),
            )
            return None
        is_anonymous = bool(data.get("isAnonymous"))
        logger.info("Spotify Canvas token OK source=%s isAnonymous=%s", source, is_anonymous)
        return str(token)

    async def _fetch_canvas_url(self, track_id: str, access_token: str) -> str | None:
        payload = _encode_canvas_request(track_id)
        headers = dict(CANVAS_HEADERS)
        headers["Authorization"] = f"Bearer {access_token}"
        async with httpx.AsyncClient(timeout=SPOTIFY_CANVAS_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.post(CANVAS_API_URL, content=payload, headers=headers)
        if response.status_code != 200:
            logger.warning("Spotify Canvas API failed: track_id=%s status=%s body=%s", track_id, response.status_code, response.text[:200])
            return None
        canvas_url = _find_canvas_url(response.content)
        if not canvas_url:
            logger.info("Spotify Canvas API response parsed without URL: track_id=%s bytes=%s", track_id, len(response.content))
        return canvas_url


spotify_canvas_service = SpotifyCanvasService()

async def fetch_canvas_video_bytes(
    track_id: str,
    artist: str | None = None,
    track_name: str | None = None,
) -> bytes | None:
    """Resolve track_id Last.fm -> Spotify ID se preciso, busca Canvas URL,
    baixa bytes. Centraliza o fluxo usado por /tcanvas e /tly.

    Retorna `bytes` do mp4 se tudo deu certo, ou `None` em qualquer falha
    (sem Canvas, download falhou, resolução Last.fm->Spotify falhou).
    Loga cada etapa pra debugging em produção.
    """
    canvas_track_id = (track_id or "").strip()
    if not canvas_track_id:
        return None
    if canvas_track_id.startswith("lfm:"):
        artist_clean = (artist or "").strip()
        track_clean = (track_name or "").strip()
        if not artist_clean or not track_clean:
            logger.info(
                "Canvas helper: lfm sem artist/track, skip | track_id=%s",
                canvas_track_id,
            )
            return None
        # Import local pra evitar ciclo (spotify importa coisas do db).
        from app.services.spotify import spotify_service
        try:
            match = await spotify_service.search_track(artist_clean, track_clean)
        except Exception:
            logger.exception(
                "Canvas helper: search_track error | artist=%s | track=%s",
                artist_clean, track_clean,
            )
            return None
        if not match or not match.get("id"):
            logger.info(
                "Canvas helper: search miss | artist=%s | track=%s",
                artist_clean, track_clean,
            )
            return None
        resolved = match["id"]
        logger.info(
            "Canvas helper resolved | lfm=%s -> spotify=%s | artist=%s | track=%s",
            canvas_track_id, resolved, artist_clean, track_clean,
        )
        canvas_track_id = resolved

    canvas_url = await spotify_canvas_service.get_canvas_url(canvas_track_id)
    if not canvas_url:
        return None
    return await spotify_canvas_service.download_canvas_bytes(canvas_url)

