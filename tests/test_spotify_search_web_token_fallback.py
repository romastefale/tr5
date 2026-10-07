"""Busca Spotify (Last.fm -> Spotify ID) com fallback para o token do web player.

Cobre: /tcanvas depende de search_track() para transformar "lfm:<hash>" em ID do
Spotify. O token app-only pode ser recusado (403); nesse caso a busca tenta o
token do web player (sp_dc) uma vez, e erro de API nunca vira "miss" em cache.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import spotify as spotify_module
from app.services import spotify_canvas as canvas_module
from app.services.spotify import SpotifyService


class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


_HIT = _Resp(
    200,
    {
        "tracks": {
            "items": [
                {
                    "id": "TRACKID123",
                    "external_urls": {"spotify": "https://open.spotify.com/track/TRACKID123"},
                    "album": {"images": [{"url": "https://img/640.jpg"}]},
                }
            ]
        }
    },
)


def _service(responses: dict[str, _Resp], *, app_token: str | None, web_token: str | None):
    """Serviço com cliente HTTP falso: a resposta depende do token usado."""
    service = SpotifyService()
    calls: list[str] = []

    async def fake_get(url, params=None, headers=None):
        bearer = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        calls.append(bearer)
        return responses[bearer]

    service._http = SimpleNamespace(get=fake_get)  # type: ignore[attr-defined]
    service._get_client_credentials_token = AsyncMock(return_value=app_token)  # type: ignore[method-assign]
    service._get_web_player_token = AsyncMock(return_value=web_token)  # type: ignore[method-assign]
    return service, calls


def _run(coro):
    return asyncio.run(coro)


def test_search_usa_token_app_quando_aceito():
    service, calls = _service({"app": _HIT}, app_token="app", web_token="web")
    record = _run(service.search_track("Artist", "Title"))
    assert record and record["id"] == "TRACKID123"
    assert calls == ["app"]
    service._get_web_player_token.assert_not_awaited()


def test_search_403_com_token_app_tenta_web_player_e_resolve():
    service, calls = _service({"app": _Resp(403), "web": _HIT}, app_token="app", web_token="web")
    record = _run(service.search_track("Artist", "Title"))
    assert record and record["id"] == "TRACKID123"
    assert calls == ["app", "web"]
    # Resultado bom fica em cache: segunda busca não chama a API.
    again = _run(service.search_track("Artist", "Title"))
    assert again == record and calls == ["app", "web"]


def test_search_403_nos_dois_tokens_devolve_none_sem_cachear_miss():
    service, calls = _service({"app": _Resp(403), "web": _Resp(403)}, app_token="app", web_token="web")
    assert _run(service.search_track("Artist", "Title")) is None
    assert calls == ["app", "web"]
    assert service._track_search_cache == {}


def test_search_403_sem_web_token_devolve_none_sem_cachear_miss():
    service, calls = _service({"app": _Resp(403)}, app_token="app", web_token=None)
    assert _run(service.search_track("Artist", "Title")) is None
    assert calls == ["app"]
    assert service._track_search_cache == {}


def test_search_sem_token_app_usa_web_player_direto():
    service, calls = _service({"web": _HIT}, app_token=None, web_token="web")
    record = _run(service.search_track("Artist", "Title"))
    assert record and record["id"] == "TRACKID123"
    assert calls == ["web"]


def test_search_resposta_200_vazia_e_miss_legitimo_e_cacheado():
    empty = _Resp(200, {"tracks": {"items": []}})
    service, calls = _service({"app": empty}, app_token="app", web_token="web")
    assert _run(service.search_track("Artist", "Title")) is None
    assert ("artist", "title") in service._track_search_cache
    assert calls == ["app"]


def test_canvas_web_token_exige_cookie_e_respeita_backoff():
    svc = canvas_module.SpotifyCanvasService()
    svc._get_access_token = AsyncMock(return_value="web")  # type: ignore[method-assign]

    original = canvas_module.SPOTIFY_CANVAS_SP_DC
    try:
        canvas_module.SPOTIFY_CANVAS_SP_DC = ""
        assert _run(svc.get_web_access_token()) is None
        svc._get_access_token.assert_not_awaited()

        canvas_module.SPOTIFY_CANVAS_SP_DC = "cookie"
        svc._token_blocked_until = 10**12  # backoff ativo
        assert _run(svc.get_web_access_token()) is None
        svc._get_access_token.assert_not_awaited()

        svc._token_blocked_until = 0.0
        assert _run(svc.get_web_access_token()) == "web"
        svc._get_access_token.assert_awaited_once()
    finally:
        canvas_module.SPOTIFY_CANVAS_SP_DC = original


def test_spotify_module_expoe_helpers():
    assert hasattr(spotify_module.SpotifyService, "_get_web_player_token")
    assert hasattr(spotify_module.SpotifyService, "_search_tracks_request")
