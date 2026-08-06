from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive as _utcnow_naive


class CoverFile(Base):
    """Cache persistente de capas por file_id do Telegram.

    O cache é uma otimização: o código sempre pode voltar para a URL original
    quando o canal técnico, banco ou file_id falhar. `file_id` é usado para
    reenvio pelo mesmo bot; `file_unique_id` fica só para diagnóstico/dedup.
    """

    __tablename__ = "cover_files"

    cache_key: Mapped[str] = mapped_column(String, primary_key=True)
    spotify_track_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
