"""
services/vector_db.py
---------------------
Service layer per operazioni amministrative su ChromaDB.

Responsabilità
--------------
- Incapsulare tutta la logica di connessione e mutazione di ChromaDB.
- Esporre funzioni sincrone (chiamate via asyncio.to_thread dal router)
  per mantenere il pattern già usato nel resto del codebase.
- Non contenere logica HTTP/FastAPI: quella appartiene al router.

Prefisso collection
-------------------
Il prefisso ``catalogue_`` è centralizzato nella costante COLLECTION_PREFIX.
Cambiarlo qui aggiorna automaticamente tutti i metodi del service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import chromadb

logger: logging.Logger = logging.getLogger(__name__)

# Unico punto in cui il prefisso è definito — coerente con ingestion/graph.py.
COLLECTION_PREFIX: str = "catalogue_"


def _collection_name(tenant_id: str) -> str:
    """Restituisce il nome canonico della collection per un tenant."""
    return f"{COLLECTION_PREFIX}{tenant_id}"


@dataclass(frozen=True)
class WipeResult:
    """Risultato immutabile dell'operazione di wipe."""

    tenant_id: str
    collection_name: str
    dropped: bool
    message: str


def wipe_tenant_collection(host: str, port: int, tenant_id: str) -> WipeResult:
    """
    Cancella permanentemente la collection ChromaDB di un tenant.

    Questa funzione è **sincrona** e va chiamata via ``asyncio.to_thread``
    dall'endpoint FastAPI, esattamente come ``_write_to_chroma_sync`` e
    ``_query_chroma_sync`` nel resto del codebase.

    Strategia di errore
    -------------------
    - Collection inesistente → ritorna ``WipeResult(dropped=False)`` senza
      sollevare eccezioni (comportamento elegante richiesto dal Tech Lead).
    - Qualsiasi altro errore ChromaDB → rilancia l'eccezione al chiamante,
      che la gestisce a livello HTTP.

    Parameters
    ----------
    host : str
        Host del server ChromaDB (da settings.chroma_host).
    port : int
        Porta del server ChromaDB (da settings.chroma_port).
    tenant_id : str
        Identificatore del tenant di cui eliminare i dati vettoriali.

    Returns
    -------
    WipeResult
        Oggetto con esito e messaggio dell'operazione.
    """
    collection: str = _collection_name(tenant_id)

    logger.info(
        "[vector_db] wipe requested | tenant=%s | target_collection=%s",
        tenant_id,
        collection,
    )

    client: chromadb.ClientAPI = chromadb.HttpClient(host=host, port=port)

    try:
        client.delete_collection(name=collection)
    except Exception as exc:  # noqa: BLE001
        exc_msg: str = str(exc)

        # ChromaDB 0.5.x solleva una generica Exception con body JSON
        # {"detail": "..."} quando la collection non esiste (HTTP 404 interno).
        # Intercettiamo questo caso senza far fallire la request.
        if "does not exist" in exc_msg.lower() or "not found" in exc_msg.lower() or "404" in exc_msg:
            logger.info(
                "[vector_db] collection not found, nothing to wipe | tenant=%s",
                tenant_id,
            )
            return WipeResult(
                tenant_id=tenant_id,
                collection_name=collection,
                dropped=False,
                message=f"Nessuna collection trovata per il tenant '{tenant_id}'. Nessuna azione eseguita.",
            )

        # Errore inatteso: logghiamo e rilanciamo per gestione HTTP 500.
        logger.exception(
            "[vector_db] unexpected error during wipe | tenant=%s",
            tenant_id,
        )
        raise

    logger.info(
        "[vector_db] collection dropped successfully | tenant=%s | collection=%s",
        tenant_id,
        collection,
    )
    return WipeResult(
        tenant_id=tenant_id,
        collection_name=collection,
        dropped=True,
        message=f"Collection '{collection}' eliminata con successo.",
    )
