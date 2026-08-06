from __future__ import annotations

import logging
import time

from sqlalchemy import text

from app.db.database import SessionLocal, engine
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)
SILENT_MODE_KEY = "silent_mode_enabled"
_STATE_CACHE_TTL_SECONDS = 1.0
_state_cache: tuple[bool, float] | None = None


def silent_mode_enabled() -> bool:
    global _state_cache
    now = time.monotonic()
    if _state_cache is not None and now < _state_cache[1]:
        return _state_cache[0]
    try:
        with SessionLocal() as db:
            row = db.execute(
                text("SELECT value FROM operational_state WHERE key=:key"),
                {"key": SILENT_MODE_KEY},
            ).scalar_one_or_none()
        result = str(row or "0").strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        logger.warning("OPERATIONAL_STATE_READ_FAILED", exc_info=True)
        # Operational access is an explicit owner-controlled boundary. A
        # database failure must not silently re-enable public commands.
        result = True
    _state_cache = (result, now + _STATE_CACHE_TTL_SECONDS)
    return result


def set_silent_mode(enabled: bool, *, owner_user_id: int | None = None) -> None:
    global _state_cache
    sql = """
        INSERT INTO operational_state (key, value, updated_by_user_id, updated_at)
        VALUES (:key, :value, :owner, :updated_at)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_by_user_id=excluded.updated_by_user_id,
            updated_at=excluded.updated_at
    """
    with SessionLocal() as db:
        db.execute(
            text(sql),
            {
                "key": SILENT_MODE_KEY,
                "value": "1" if enabled else "0",
                "owner": owner_user_id,
                "updated_at": utcnow_naive(),
            },
        )
        db.commit()
    _state_cache = (bool(enabled), time.monotonic() + _STATE_CACHE_TTL_SECONDS)
    logger.info("PUBLIC_COMMANDS_STATE enabled=%s", not enabled)
