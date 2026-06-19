"""
worker/worker_settings.py
--------------------------
ARQ WorkerSettings — V2.

Rimpiazza il blocking SSE di V1 con una coda di task asincrona (ARQ + Redis).
FastAPI accoda i job; questo worker li esegue in un processo separato.

Concorrenza e GPU
-----------------
Su un server single-node con GPU condivisa (LLM 14B + embedding), la
concorrenza deve essere severamente limitata per evitare OOM del VRAM.

Il parametro ``max_jobs`` viene letto da ``ARQ_MAX_JOBS`` (env var):

- **Produzione GPU condivisa** (default): ``ARQ_MAX_JOBS=1``
  Un solo job LLM alla volta. Ollama non parallelizza l'inferenza su
  una singola GPU; job concorrenti causerebbero accodamento interno in
  Ollama e potenziale OOM se le sequenze sono lunghe.

- **Data center futuro, GPU dedicata**: ``ARQ_MAX_JOBS=3``
  Con GPU separata per embedding e LLM si possono gestire 2-3 job paralleli.
  Aumentare con cautela: testare con ``ollama ps`` il VRAM residuo.

Configurazione tramite variabili d'ambiente
-------------------------------------------
``ARQ_MAX_JOBS``    — concorrenza massima (default: 1)
``ARQ_JOB_TIMEOUT`` — timeout per singolo job in secondi (default: 300)
``ARQ_KEEP_RESULT`` — TTL risultati in Redis in secondi (default: 86400 = 24h)

Avvio
-----
::

    arq worker.worker_settings.WorkerSettings

In Docker Compose (produzione)::

    command: python -m arq worker.worker_settings.WorkerSettings
"""

from __future__ import annotations

import os

# Deve stare prima di qualsiasi import langgraph/core per evitare il warning
# di deserializzazione msgpack sui tipi custom nel checkpointer Postgres.
os.environ.setdefault(
    "LANGGRAPH_ALLOWED_MSGPACK_MODULES",
    "core.state,ingestion.models",
)

from arq.connections import RedisSettings

from core.config import get_settings
from worker.tasks import (
    run_ingestion_task,
    run_qualification_task,
    run_qualification_task_resume,
)

# ── Lettura parametri da env var ──────────────────────────────────────────────


def _read_int_env(name: str, default: int) -> int:
    """
    Legge una variabile d'ambiente come intero.

    Fallisce con errore chiaro se il valore non è un intero valido,
    evitando avvii silenziosi con parametri sbagliati.
    """
    raw: str | None = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"Variabile d'ambiente {name!r} non è un intero valido: {raw!r}"
        ) from exc


_max_jobs: int = _read_int_env("ARQ_MAX_JOBS", default=1)
"""
Concorrenza massima del worker.

Default 1: conservativo per GPU condivisa con LLM 14B.
Aumentare in ambienti con risorse GPU dedicate (data center futuro).
Vedere docstring del modulo per la guida alla scelta del valore.
"""

_job_timeout: int = _read_int_env("ARQ_JOB_TIMEOUT", default=300)
"""
Timeout di un singolo job in secondi (default: 5 minuti).

Se il modello LLM impiega più di questo tempo, il job viene cancellato
e l'errore viene propagato al client via GET /status/{thread_id}.
Aumentare per cataloghi molto grandi (ingestion) o prompt complessi.
"""

_keep_result: int = _read_int_env("ARQ_KEEP_RESULT", default=86_400)
"""
TTL dei risultati in Redis in secondi (default: 24 ore).

I client che fanno polling su GET /status/{thread_id} trovano il risultato
fino a questo TTL dopo il completamento del job.
"""


# ── WorkerSettings ────────────────────────────────────────────────────────────


class WorkerSettings:
    """
    Configurazione ARQ letta all'avvio del worker da env var.

    Tutti i parametri numerici hanno default conservativi per GPU condivisa.
    """

    functions = [
        run_qualification_task,
        run_qualification_task_resume,
        run_ingestion_task,
    ]

    redis_settings: RedisSettings = RedisSettings.from_dsn(get_settings().redis_dsn)

    max_jobs: int = _max_jobs
    job_timeout: int = _job_timeout
    keep_result: int = _keep_result
