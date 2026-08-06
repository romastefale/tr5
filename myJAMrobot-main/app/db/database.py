from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config.settings import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def run_migrations(bind=engine) -> None:
    """Create only current support tables; never drop or rewrite legacy data."""
    dialect = bind.dialect.name
    bigint = "BIGINT" if dialect == "postgresql" else "INTEGER"
    timestamp = "TIMESTAMP" if dialect == "postgresql" else "DATETIME"
    with bind.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS operational_state (
                    key VARCHAR PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_by_user_id {bigint},
                    updated_at {timestamp} NOT NULL
                )
                """
            )
        )


def init_db() -> None:
    # Importing these models registers only tables used by the nine-command
    # runtime. Tables from older releases remain untouched in existing DBs.
    from app.models.canvas_file import CanvasFile  # noqa: F401
    from app.models.canvas_processed_file import CanvasProcessedFile  # noqa: F401
    from app.models.cover_file import CoverFile  # noqa: F401
    from app.models.lastfm_profile import LastfmProfile  # noqa: F401
    from app.models.lyrics_snippet_cache import LyricsSnippetCache  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    if engine.dialect.name == "sqlite" and engine.url.database not in {None, "", ":memory:"}:
        try:
            Path(str(engine.url.database)).chmod(0o600)
        except OSError:
            logger.warning("DATABASE_PERMISSION_HARDENING_FAILED")
    logger.info("DATABASE_READY current_tables=%s", len(Base.metadata.tables))
