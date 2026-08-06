from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive


class LyricsSnippetCache(Base):
    """Only the final short excerpt is persisted; full lyrics are never stored."""

    __tablename__ = "lyrics_snippet_cache"

    cache_key: Mapped[str] = mapped_column(String, primary_key=True)
    artist_norm: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title_norm: Mapped[str] = mapped_column(String, nullable=False, index=True)
    artist: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_naive)
