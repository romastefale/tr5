from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "", *, legacy: Iterable[str] = ()) -> str:
    for candidate in (name, *legacy):
        value = os.getenv(candidate)
        if value is not None:
            return value
    return default


def _secret(name: str, *, legacy: Iterable[str] = ()) -> str:
    value = _env(name, legacy=legacy).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value


def _int(name: str, default: int, *, legacy: Iterable[str] = ()) -> int:
    raw = _env(name, legacy=legacy).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("CONFIG_VALUE_IGNORED name=%s expected=int", name)
        return default


def _float(name: str, default: float, *, legacy: Iterable[str] = ()) -> float:
    raw = _env(name, legacy=legacy).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("CONFIG_VALUE_IGNORED name=%s expected=float", name)
        return default


def _bool(name: str, default: bool, *, legacy: Iterable[str] = ()) -> bool:
    raw = _env(name, legacy=legacy).strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    logger.warning("CONFIG_VALUE_IGNORED name=%s expected=bool", name)
    return default


def _owner_ids() -> frozenset[int]:
    raw = _env(
        "MYJAM_OWNER_IDS",
        legacy=("CODE_OWNER_IDS", "OWNER_IDS", "OWNER_ID", "TR4_CODE_OWNER_IDS", "TR3_OWNER_IDS"),
    )
    values: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            owner_id = int(item)
            if owner_id <= 0:
                raise ValueError
            values.add(owner_id)
        except ValueError:
            logger.warning("CONFIG_VALUE_IGNORED name=MYJAM_OWNER_IDS expected=positive_int_list")
    return frozenset(values)


def _resolve_data_dir() -> Path:
    requested = _env("MYJAM_DATA_DIR", "/data", legacy=("DATA_DIR", "TR3_DATA_DIR")).strip() or "/data"
    for candidate in (Path(requested), Path("/app/data"), Path("/tmp/myjamrobot-data")):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError:
            logger.warning("DATA_DIR_UNAVAILABLE path=%s", candidate)
    raise RuntimeError("Nenhum diretório de dados gravável está disponível")


TELEGRAM_BOT_TOKEN = _secret(
    "MYJAM_TELEGRAM_BOT_TOKEN",
    legacy=("TELEGRAM_BOT_TOKEN", "TR3_TELEGRAM_BOT_TOKEN"),
)
BASE_URL = _env("MYJAM_BASE_URL", "http://localhost:8000", legacy=("BASE_URL", "TR3_BASE_URL")).strip().rstrip("/")

LASTFM_API_KEY = _secret("MYJAM_LASTFM_API_KEY", legacy=("LASTFM_API_KEY", "TR3_LASTFM_API_KEY"))
LASTFM_API_BASE_URL = "https://ws.audioscrobbler.com/2.0/"

# These app-only credentials enrich metadata and Canvas. They never authenticate
# a Telegram user and are intentionally optional.
SPOTIFY_CLIENT_ID = _secret("MYJAM_SPOTIFY_CLIENT_ID", legacy=("SPOTIFY_CLIENT_ID", "TR3_SPOTIFY_CLIENT_ID"))
SPOTIFY_CLIENT_SECRET = _secret(
    "MYJAM_SPOTIFY_CLIENT_SECRET",
    legacy=("SPOTIFY_CLIENT_SECRET", "TR3_SPOTIFY_CLIENT_SECRET"),
)

HTTP_TIMEOUT_SECONDS = _float("MYJAM_HTTP_TIMEOUT_SECONDS", 8.0, legacy=("HTTP_TIMEOUT_SECONDS", "TR3_HTTP_TIMEOUT_SECONDS"))
SPOTIFY_CANVAS_ENABLED = _bool("MYJAM_SPOTIFY_CANVAS_ENABLED", True, legacy=("SPOTIFY_CANVAS_ENABLED", "TR3_SPOTIFY_CANVAS_ENABLED"))
SPOTIFY_CANVAS_TIMEOUT_SECONDS = _float(
    "MYJAM_SPOTIFY_CANVAS_TIMEOUT_SECONDS",
    8.0,
    legacy=("SPOTIFY_CANVAS_TIMEOUT_SECONDS", "TR3_SPOTIFY_CANVAS_TIMEOUT_SECONDS"),
)

CANVAS_CACHE_ENABLED = _bool("MYJAM_CANVAS_CACHE_ENABLED", True, legacy=("CANVAS_CACHE_ENABLED", "TR3_CANVAS_CACHE_ENABLED"))
CANVAS_CACHE_CHANNEL_ID = _int("MYJAM_CANVAS_CACHE_CHANNEL_ID", 0, legacy=("CANVAS_CACHE_CHANNEL_ID", "TR3_CANVAS_CACHE_CHANNEL_ID"))
COVER_CACHE_ENABLED = _bool("MYJAM_COVER_CACHE_ENABLED", True, legacy=("COVER_CACHE_ENABLED", "TR3_COVER_CACHE_ENABLED"))
COVER_CACHE_CHANNEL_ID = _int(
    "MYJAM_COVER_CACHE_CHANNEL_ID",
    CANVAS_CACHE_CHANNEL_ID,
    legacy=("COVER_CACHE_CHANNEL_ID", "TR3_COVER_CACHE_CHANNEL_ID"),
)
CANVAS_AUDIO_PREVIEW_ENABLED = _bool(
    "MYJAM_CANVAS_AUDIO_PREVIEW_ENABLED",
    True,
    legacy=("CANVAS_AUDIO_PREVIEW_ENABLED", "TR3_CANVAS_AUDIO_PREVIEW_ENABLED"),
)
CANVAS_AUDIO_PROCESS_VERSION = _env(
    "MYJAM_CANVAS_AUDIO_PROCESS_VERSION",
    "preview-v2",
    legacy=("CANVAS_AUDIO_PROCESS_VERSION", "TR3_CANVAS_AUDIO_PROCESS_VERSION"),
).strip() or "preview-v2"

COMMAND_RATE_LIMIT_ENABLED = _bool(
    "MYJAM_COMMAND_RATE_LIMIT_ENABLED",
    True,
    legacy=("COMMAND_RATE_LIMIT_ENABLED", "TR3_COMMAND_RATE_LIMIT_ENABLED"),
)
COMMAND_RATE_LIMIT_WINDOW_SECONDS = _int(
    "MYJAM_COMMAND_RATE_LIMIT_WINDOW_SECONDS",
    60,
    legacy=("COMMAND_RATE_LIMIT_WINDOW_SECONDS", "TR3_COMMAND_RATE_LIMIT_WINDOW_SECONDS"),
)
COMMAND_RATE_LIMIT_STANDARD_PER_WINDOW = _int(
    "MYJAM_COMMAND_RATE_LIMIT_STANDARD_PER_WINDOW",
    12,
    legacy=("COMMAND_RATE_LIMIT_STANDARD_PER_WINDOW", "TR3_COMMAND_RATE_LIMIT_STANDARD_PER_WINDOW"),
)
COMMAND_RATE_LIMIT_EXPENSIVE_PER_WINDOW = _int(
    "MYJAM_COMMAND_RATE_LIMIT_EXPENSIVE_PER_WINDOW",
    4,
    legacy=("COMMAND_RATE_LIMIT_EXPENSIVE_PER_WINDOW", "TR3_COMMAND_RATE_LIMIT_EXPENSIVE_PER_WINDOW"),
)
MAX_CONCURRENT_HEAVY_JOBS = max(1, _int("MYJAM_MAX_CONCURRENT_HEAVY_JOBS", 3))
MAX_LYRICS_BACKGROUND_TASKS = max(1, _int("MYJAM_MAX_LYRICS_BACKGROUND_TASKS", 8))
MAX_WEBHOOK_BYTES = max(65_536, _int("MYJAM_MAX_WEBHOOK_BYTES", 1_048_576))

DATA_DIR = _resolve_data_dir()
_raw_database_url = _env("MYJAM_DATABASE_URL", legacy=("DATABASE_URL", "TR3_DATABASE_URL")).strip()
if _raw_database_url and not _raw_database_url.lower().startswith("sqlite:"):
    raise RuntimeError("MYJAM_DATABASE_URL must use SQLite; refusing an implicit database fallback")
DATABASE_URL = _raw_database_url or f"sqlite:///{DATA_DIR / 'myjamrobot.sqlite3'}"

CODE_OWNER_IDS = _owner_ids()


def is_code_owner(user_id: int | str | None) -> bool:
    try:
        return int(user_id) in CODE_OWNER_IDS
    except (TypeError, ValueError):
        return False


def validate_required_env() -> list[str]:
    try:
        parsed_base = urlsplit(BASE_URL)
        valid_base = bool(
            parsed_base.scheme == "https"
            and parsed_base.hostname
            and not parsed_base.username
            and not parsed_base.password
            and not parsed_base.query
            and not parsed_base.fragment
            and parsed_base.path in {"", "/"}
        )
    except ValueError:
        valid_base = False
    required = (
        ("MYJAM_TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("MYJAM_LASTFM_API_KEY", LASTFM_API_KEY),
        ("MYJAM_BASE_URL", BASE_URL if valid_base else ""),
        ("MYJAM_OWNER_IDS", CODE_OWNER_IDS),
    )
    return [name for name, value in required if not value]


def telegram_webhook_secret() -> str | None:
    if not TELEGRAM_BOT_TOKEN:
        return None
    return hmac.new(
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        b"myjamrobot-webhook-v2",
        hashlib.sha256,
    ).hexdigest()
