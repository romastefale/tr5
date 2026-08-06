from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive as _utcnow_naive


class CanvasFile(Base):
    """Cache persistente do Spotify Canvas por faixa (reuso de file_id).

    O Telegram guarda cada arquivo enviado e devolve um `file_id` que pode ser
    reenviado pra qualquer chat (pelo mesmo bot) sem re-upload — padrão oficial
    (Bot FAQ / grammY docs). Assim /canvas para de rebaixar do CDN e
    re-subir o mesmo vídeo a cada chamada.

    - `track_id`: Spotify track_id base62 (NUNCA o "lfm:<hash>" interno).
    - `file_id`: usado pra REENVIAR o vídeo (pode mudar com o tempo — se o
      Telegram rejeitar com "wrong file_id", o caller esquece e re-sobe).
    - `file_unique_id`: estável; só pra dedup/diagnóstico (não reenvia/baixa).
    """

    __tablename__ = "canvas_files"

    track_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
