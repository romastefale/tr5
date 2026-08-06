"""Helpers de datetime do projeto.

Histórico (Sprint 1): `datetime.utcnow()` foi deprecado no Python 3.12+. A
substituição idiomática é `datetime.now(timezone.utc)` (timezone-aware), mas
o schema do SQLAlchemy usa colunas `DateTime` SEM tz (naive). Pra evitar
quebrar comparações e migrations, mantemos o naive UTC explicitamente.

S2: antes essa função estava duplicada em 3 models + spotify.py (4 cópias).
Agora é a fonte única; importar daqui em todo lugar.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """UTC naive (sem tzinfo). Equivalente ao antigo `datetime.utcnow()`,
    mas sem deprecation warning no Python 3.12+."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
