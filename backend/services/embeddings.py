"""
services/embeddings.py
-----------------------
Wrapper asincrono centralizzato per OllamaEmbeddings — V2.

Fonte unica di verità per la generazione di vettori nel sistema.
Ogni componente che produce o consuma embedding DEVE passare da qui.

Consumatori
-----------
- agents/mapper.py        → aembed_query()     per la similarity_search
- ingestion/graph.py      → aembed_documents() per l'upsert batch in pgvector

Anti-pattern evitati
--------------------
- Nessuna chiamata sincrona (embed_query / embed_documents bloccano il loop).
  Usare esclusivamente aembed_query / aembed_documents.
- Nessuna istanza OllamaEmbeddings nei singoli nodi LangGraph.
  Il singleton è gestito qui via lru_cache.
- Nessun testo grezzo nei log di errore. In caso di eccezione vengono
  loggati solo: text_len, batch_size, tenant_id, messaggio d'errore.

Configurazione (.env o variabili d'ambiente)
--------------------------------------------
EMBEDDING_MODEL      — modello Ollama  (default: nomic-embed-text, dim 768)
EMBEDDING_BASE_URL   — URL base Ollama (default: http://localhost:11434)
PGVECTOR_EMBEDDING_DIM — dimensione attesa (default: 768)

Compatibilità dimensioni
------------------------
Il modello e la dimensione devono essere coerenti:
  nomic-embed-text  → 768 dim   (default)
  mxbai-embed-large → 1024 dim  (aggiornare pgvector_embedding_dim di conseguenza)

In caso di mismatch viene sollevata EmbeddingDimensionMismatchError
prima che i dati raggiungano pgvector, evitando inserimenti silentemente
errati nel DB.
"""

from __future__ import annotations

from functools import lru_cache

import structlog
from langchain_ollama import OllamaEmbeddings

from core.config import get_settings

log: structlog.BoundLogger = structlog.get_logger()


# ── Eccezioni custom ──────────────────────────────────────────────────────────

class EmbeddingError(RuntimeError):
    """
    La chiamata HTTP a Ollama è fallita (timeout, connessione rifiutata,
    risposta malformata, ecc.).
    """


class EmbeddingDimensionMismatchError(ValueError):
    """
    La dimensione del vettore ritornato da Ollama non coincide
    con ``pgvector_embedding_dim`` definita in ``core/config.py``.

    Causa più comune: ``embedding_model`` e ``pgvector_embedding_dim``
    non sono stati aggiornati insieme (es. cambio modello senza aggiornare
    la configurazione e la migrazione Alembic).
    """


# ── Singleton ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_embeddings_service() -> OllamaEmbeddings:
    """
    Ritorna il singleton ``OllamaEmbeddings`` configurato da ``core.config``.

    Viene istanziato al primo accesso e riutilizzato per tutta la durata
    del processo (FastAPI, ARQ worker, ingestion graph).

    Thread-safe: ``lru_cache`` garantisce al massimo un'istanza per processo.
    """
    settings = get_settings()
    instance = OllamaEmbeddings(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
    )
    log.info(
        "embeddings.service_initialized",
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        expected_dim=settings.pgvector_embedding_dim,
    )
    return instance


# ── Validazione interna ───────────────────────────────────────────────────────

def _validate_dim(vector: list[float], expected_dim: int, model: str) -> None:
    """
    Verifica che la lunghezza di ``vector`` coincida con ``expected_dim``.

    Raises
    ------
    EmbeddingDimensionMismatchError
        Se ``len(vector) != expected_dim``.
    """
    actual: int = len(vector)
    if actual != expected_dim:
        raise EmbeddingDimensionMismatchError(
            f"Dimensione embedding errata: ottenuto {actual} dim, atteso {expected_dim} dim. "
            f"Verificare che embedding_model='{model}' e pgvector_embedding_dim={expected_dim} "
            f"in core/config.py siano coerenti. "
            f"Hint: nomic-embed-text → 768 dim, mxbai-embed-large → 1024 dim."
        )


# ── API pubblica ──────────────────────────────────────────────────────────────

async def aembed_query(
    text: str,
    *,
    tenant_id: str = "",
) -> list[float]:
    """
    Genera un embedding asincrono per un singolo testo query.

    Destinato a: ``agents/mapper.py`` per la similarity_search su pgvector.

    Sicurezza dei log
    -----------------
    In caso di errore vengono loggati solo ``text_len`` e ``tenant_id``,
    mai il contenuto testuale grezzo (potenziale PII).

    Parameters
    ----------
    text : str
        Testo da embeddare (es. nome di un servizio estratto dal lead).
    tenant_id : str
        Identificatore tenant — usato solo nel logging, mai come payload.

    Returns
    -------
    list[float]
        Vettore con ``len == pgvector_embedding_dim``.

    Raises
    ------
    EmbeddingError
        Se la chiamata HTTP a Ollama fallisce.
    EmbeddingDimensionMismatchError
        Se ``len(vector) != pgvector_embedding_dim``.
    """
    settings = get_settings()
    svc: OllamaEmbeddings = get_embeddings_service()

    try:
        vector: list[float] = await svc.aembed_query(text)
    except Exception as exc:
        log.error(
            "embeddings.query_failed",
            text_len=len(text),
            tenant_id=tenant_id or "unknown",
            model=settings.embedding_model,
            error=str(exc),
        )
        raise EmbeddingError(
            f"Ollama aembed_query fallito [tenant='{tenant_id}', "
            f"model='{settings.embedding_model}']: {exc}"
        ) from exc

    _validate_dim(vector, settings.pgvector_embedding_dim, settings.embedding_model)
    return vector


async def aembed_documents(
    texts: list[str],
    *,
    tenant_id: str = "",
) -> list[list[float]]:
    """
    Genera embedding asincroni per un batch di documenti.

    Destinato a: ``ingestion/graph.py`` per embeddare ``ServiceItem``
    prima dell'upsert in pgvector. Preferire questo a chiamate singole
    ripetute per ridurre il numero di round-trip HTTP verso Ollama.

    Parameters
    ----------
    texts : list[str]
        Testi da embeddare (es. descrizioni dei servizi nel catalogo).
    tenant_id : str
        Identificatore tenant — solo per logging.

    Returns
    -------
    list[list[float]]
        Lista di vettori; ``result[i]`` è l'embedding di ``texts[i]``.
        Lista vuota se ``texts`` è vuoto.

    Raises
    ------
    EmbeddingError
        Se la chiamata HTTP a Ollama fallisce.
    EmbeddingDimensionMismatchError
        Se almeno un vettore ha dimensione != ``pgvector_embedding_dim``.
    """
    if not texts:
        return []

    settings = get_settings()
    svc: OllamaEmbeddings = get_embeddings_service()

    try:
        vectors: list[list[float]] = await svc.aembed_documents(texts)
    except Exception as exc:
        log.error(
            "embeddings.batch_failed",
            batch_size=len(texts),
            total_chars=sum(len(t) for t in texts),
            tenant_id=tenant_id or "unknown",
            model=settings.embedding_model,
            error=str(exc),
        )
        raise EmbeddingError(
            f"Ollama aembed_documents fallito [tenant='{tenant_id}', "
            f"batch_size={len(texts)}, model='{settings.embedding_model}']: {exc}"
        ) from exc

    # Valida ogni vettore del batch
    for i, vector in enumerate(vectors):
        _validate_dim(vector, settings.pgvector_embedding_dim, settings.embedding_model)

    log.debug(
        "embeddings.batch_done",
        batch_size=len(texts),
        dim=settings.pgvector_embedding_dim,
        model=settings.embedding_model,
        tenant_id=tenant_id or "unknown",
    )
    return vectors
