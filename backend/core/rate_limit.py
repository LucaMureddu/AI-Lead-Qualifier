"""
core/rate_limit.py
------------------
Singleton slowapi Limiter — V2.1.

Questo modulo esporta un'unica istanza ``limiter`` condivisa tra main.py
(che la assegna a app.state.limiter per SlowAPIMiddleware) e api/routes.py
(che la usa nei decorator @limiter.limit).

Deve essere un singleton di modulo perché i decorator @limiter.limit vengono
valutati all'import di routes.py — prima che il lifespan di FastAPI parta.
Creare il Limiter qui anziché in main.py evita l'import circolare
main → routes → main.

Backend Redis (fail-open)
-------------------------
``in_memory_fallback_enabled=True`` attiva un fallback in-memory se Redis non
è raggiungibile. In quel caso il rate limit viene applicato per singolo
processo (non distribuito), ma il servizio non va in errore 500.
Questo garantisce che un'interruzione di Redis non blocchi il traffico.

Key function
------------
``get_remote_address`` limita per IP del chiamante (X-Forwarded-For o
REMOTE_ADDR). Gli endpoint sensibili al tenant (es. /lead) possono
sovrascrivere la key function nel decorator se necessario.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from core.config import get_settings

_settings = get_settings()

limiter: Limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_settings.redis_dsn,
    in_memory_fallback_enabled=True,
)
