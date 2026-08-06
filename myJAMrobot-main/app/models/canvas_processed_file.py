from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive as _utcnow_naive


class CanvasProcessedFile(Base):
    """Cache persistente de variantes processadas do Canvas.

    Não substitui canvas_files. A tabela canvas_files continua representando o
    Canvas bruto por faixa; esta tabela guarda derivados seguros, como o Canvas
    muxado com preview oficial.
    """

    __tablename__ = "canvas_processed_files"

    cache_key: Mapped[str] = mapped_column(String, primary_key=True)
    spotify_track_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    canvas_fingerprint: Mapped[str] = mapped_column(String, nullable=False, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    process_kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    process_version: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
