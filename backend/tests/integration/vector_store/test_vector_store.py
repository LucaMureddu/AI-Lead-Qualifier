"""
tests/integration/vector_store/test_vector_store.py
-----------------------------------------------------
Test di integrazione: isolamento multi-tenant in database.vector_store.

Principio
---------
Nessun mock di asyncpg. Tutte le query vengono eseguite contro un container
pgvector/pgvector:pg16 reale (gestito da conftest.py). Questo valida:
- la correttezza delle query SQL (inclusa la distanza coseno <=>)
- il contratto di sicurezza multi-tenant
- il comportamento dell'indice HNSW su dati reali

Contratto di sicurezza testato
-------------------------------
Una riga appartenente a ``tenant_a`` non deve MAI comparire nei risultati
di una query con scope ``tenant_b``, e viceversa — anche quando i vettori
di embedding sono identici.

Sicurezza dei log
-----------------
I test non stampano mai vettori completi o testo grezzo.
I messaggi di asserzione contengono solo: nomi di servizio (metadati),
conteggio righe, tenant_id.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from database.vector_store import similarity_search, upsert_items, wipe_tenant
from langchain_core.documents import Document

# ── Costanti di test ──────────────────────────────────────────────────────────

TENANT_A: str = "tenant_acme"
TENANT_B: str = "tenant_globex"
EMBEDDING_DIM: int = 768


def _unit_vec(hot_index: int) -> list[float]:
    """
    Vettore unitario con 1.0 alla posizione ``hot_index``, 0.0 altrove.

    Distanza coseno tra due vettori unitari diversi = 1.0 (ortogonali).
    Distanza coseno tra copie identiche = 0.0 (direzione identica).
    Questi valori deterministici permettono asserzioni esatte sull'ordinamento.
    """
    v: list[float] = [0.0] * EMBEDDING_DIM
    v[hot_index] = 1.0
    return v


# ── Fixture dati ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture  # type: ignore[name-defined]
async def seeded_catalogue(pg_pool: asyncpg.Pool) -> None:  # noqa: ARG001
    """
    Inserisce dati di test per entrambi i tenant.

    tenant_a: "Web Development" (dim 0) + "Email Setup" (dim 1)
    tenant_b: "Cloud Hosting"   (dim 0)  ← stesso asse di tenant_a[0]

    Il fatto che tenant_b usi lo stesso vettore di un servizio di tenant_a
    è intenzionale: serve a verificare che il filtro tenant_id non sia
    aggirato dalla similitudine vettoriale.

    ``pg_pool`` è richiesto esplicitamente per assicurare che il singleton
    db_core._pool sia iniettato prima che upsert_items chiami get_pool().
    """
    await upsert_items(
        items=[
            {
                "service": "Web Development",
                "price": 1500.0,
                "description": "Sviluppo web",
                "embedding": _unit_vec(0),
            },
            {
                "service": "Email Setup",
                "price": 300.0,
                "description": "Config email",
                "embedding": _unit_vec(1),
            },
        ],
        tenant_id=TENANT_A,
    )
    await upsert_items(
        items=[
            {
                "service": "Cloud Hosting",
                "price": 800.0,
                "description": "Hosting cloud",
                "embedding": _unit_vec(0),  # stesso asse di tenant_a Web Development
            },
        ],
        tenant_id=TENANT_B,
    )


# ── Necessario per pytest_asyncio.fixture nei file con asyncio_mode="auto" ───
import pytest_asyncio  # noqa: E402


# ── Test suite ────────────────────────────────────────────────────────────────


@pytest.mark.vector_store
class TestMultiTenantIsolation:
    """
    Contratto di sicurezza fondamentale: ogni query è confinata a un tenant.

    Questi test sono la rete di sicurezza per le modifiche a vector_store.py.
    Qualsiasi cambiamento che rompa l'isolamento multi-tenant deve fallire qui.
    """

    async def test_tenant_a_non_vede_righe_di_tenant_b(
        self,
        seeded_catalogue: None,
    ) -> None:
        """
        Query su tenant_a con il vettore usato da tenant_b per 'Cloud Hosting'.
        Il risultato deve contenere solo servizi di tenant_a.
        """
        results: list[Document] = await similarity_search(
            query_embedding=_unit_vec(0),
            tenant_id=TENANT_A,
            n_results=10,
        )
        service_names: list[str] = [doc.metadata["service"] for doc in results]

        assert "Cloud Hosting" not in service_names, (
            "LEAK: riga di tenant_b 'Cloud Hosting' visibile nella query di tenant_a"
        )
        assert "Web Development" in service_names, (
            "Servizio legittimo di tenant_a non trovato"
        )

    async def test_tenant_b_non_vede_righe_di_tenant_a(
        self,
        seeded_catalogue: None,
    ) -> None:
        """
        Query su tenant_b — deve vedere solo 'Cloud Hosting', mai i servizi di tenant_a.
        """
        results: list[Document] = await similarity_search(
            query_embedding=_unit_vec(0),
            tenant_id=TENANT_B,
            n_results=10,
        )
        service_names: list[str] = [doc.metadata["service"] for doc in results]

        assert "Web Development" not in service_names, (
            "LEAK: riga di tenant_a 'Web Development' visibile nella query di tenant_b"
        )
        assert "Email Setup" not in service_names, (
            "LEAK: riga di tenant_a 'Email Setup' visibile nella query di tenant_b"
        )
        assert "Cloud Hosting" in service_names, (
            "Servizio legittimo di tenant_b non trovato"
        )

    async def test_metadata_tenant_id_coincide_con_tenant_interrogato(
        self,
        seeded_catalogue: None,
    ) -> None:
        """
        Ogni Document.metadata['tenant_id'] deve essere uguale al tenant richiesto.

        Questa asserzione cattura bug in cui la query restituisce righe corrette
        per contenuto ma con metadati di un altro tenant.
        """
        for tenant in (TENANT_A, TENANT_B):
            results: list[Document] = await similarity_search(
                query_embedding=_unit_vec(0),
                tenant_id=tenant,
                n_results=10,
            )
            for doc in results:
                actual_tenant: str = doc.metadata["tenant_id"]
                assert actual_tenant == tenant, (
                    f"tenant_id nel metadata ({actual_tenant!r}) != "
                    f"tenant interrogato ({tenant!r})"
                )


@pytest.mark.vector_store
class TestSimilaritySearch:
    """Correttezza funzionale di similarity_search."""

    async def test_ordine_nearest_neighbour(
        self,
        seeded_catalogue: None,
    ) -> None:
        """
        Query con vettore dim-0 → 'Web Development' (dim-0, distanza 0.0)
        deve precedere 'Email Setup' (dim-1, distanza 1.0).
        """
        results: list[Document] = await similarity_search(
            query_embedding=_unit_vec(0),
            tenant_id=TENANT_A,
            n_results=2,
        )

        assert len(results) == 2
        assert results[0].metadata["service"] == "Web Development", (
            "Il servizio più vicino deve essere il primo risultato"
        )
        assert results[1].metadata["service"] == "Email Setup"

    async def test_n_results_rispettato(
        self,
        seeded_catalogue: None,
    ) -> None:
        """n_results=1 deve ritornare esattamente un documento."""
        results: list[Document] = await similarity_search(
            query_embedding=_unit_vec(0),
            tenant_id=TENANT_A,
            n_results=1,
        )
        assert len(results) == 1

    async def test_catalogo_vuoto_ritorna_lista_vuota(
        self,
        pg_pool: asyncpg.Pool,  # noqa: ARG002 — inietta il singleton
    ) -> None:
        """Query su tenant inesistente deve ritornare []."""
        results: list[Document] = await similarity_search(
            query_embedding=_unit_vec(0),
            tenant_id="tenant_inesistente_xyz",
            n_results=5,
        )
        assert results == []

    async def test_distance_presente_nel_metadata(
        self,
        seeded_catalogue: None,
    ) -> None:
        """
        Il campo 'distance' deve essere presente nei metadati e >= 0.0.

        Non logghiamo il valore esatto della distanza (potrebbe contenere
        informazioni sul vettore query). Verifichiamo solo tipo e range.
        """
        results: list[Document] = await similarity_search(
            query_embedding=_unit_vec(0),
            tenant_id=TENANT_A,
            n_results=1,
        )
        assert len(results) == 1
        meta: dict[str, Any] = results[0].metadata
        assert "distance" in meta, "Campo 'distance' assente nei metadati"
        assert isinstance(meta["distance"], float), "distance deve essere float"
        assert meta["distance"] >= 0.0, "distance deve essere non-negativa"

    async def test_max_distance_filtra_risultati_distanti(
        self,
        seeded_catalogue: None,
    ) -> None:
        """
        max_distance=0.5 deve escludere 'Email Setup' (distanza ~1.0 da dim-0).

        Verifica che il filtro di soglia in vector_store.py funzioni
        contro il DB reale (non mockato).
        """
        results: list[Document] = await similarity_search(
            query_embedding=_unit_vec(0),
            tenant_id=TENANT_A,
            n_results=5,
            max_distance=0.5,
        )
        service_names: list[str] = [doc.metadata["service"] for doc in results]
        assert "Email Setup" not in service_names, (
            "Email Setup (distanza ~1.0) non dovrebbe superare max_distance=0.5"
        )
        assert "Web Development" in service_names, (
            "Web Development (distanza ~0.0) deve essere incluso"
        )


@pytest.mark.vector_store
class TestUpsertItems:
    """Correttezza di upsert_items incluso ON CONFLICT update."""

    async def test_upsert_inserisce_nuove_righe(
        self,
        pg_pool: asyncpg.Pool,
    ) -> None:
        """Primo upsert di un servizio deve creare una riga."""
        written: int = await upsert_items(
            items=[
                {
                    "service": "SEO Optimization",
                    "price": 200.0,
                    "description": "SEO per PMI",
                    "embedding": _unit_vec(5),
                }
            ],
            tenant_id=TENANT_A,
        )
        assert written == 1

        count: int = await pg_pool.fetchval(
            "SELECT COUNT(*) FROM catalogue_items WHERE tenant_id = $1 AND service = $2",
            TENANT_A,
            "SEO Optimization",
        )
        assert count == 1

    async def test_upsert_aggiorna_prezzo_su_conflitto(
        self,
        pg_pool: asyncpg.Pool,
    ) -> None:
        """Secondo upsert dello stesso (tenant_id, service) deve aggiornare il prezzo."""
        base_item: dict[str, Any] = {
            "service": "SEO Optimization",
            "price": 200.0,
            "description": "SEO",
            "embedding": _unit_vec(5),
        }
        await upsert_items(items=[base_item], tenant_id=TENANT_A)

        updated_item: dict[str, Any] = {**base_item, "price": 250.0}
        await upsert_items(items=[updated_item], tenant_id=TENANT_A)

        price: float = await pg_pool.fetchval(
            "SELECT price FROM catalogue_items WHERE tenant_id = $1 AND service = $2",
            TENANT_A,
            "SEO Optimization",
        )
        assert price == 250.0, f"Prezzo atteso 250.0, trovato {price}"

    async def test_stesso_servizio_su_tenant_diversi_crea_righe_separate(
        self,
        pg_pool: asyncpg.Pool,
    ) -> None:
        """Lo stesso nome di servizio sotto tenant diversi deve creare due righe distinte."""
        item: dict[str, Any] = {
            "service": "Consulting",
            "price": 500.0,
            "description": "IT consulting",
            "embedding": _unit_vec(10),
        }
        await upsert_items(items=[item], tenant_id=TENANT_A)
        await upsert_items(items=[item], tenant_id=TENANT_B)

        count: int = await pg_pool.fetchval(
            "SELECT COUNT(*) FROM catalogue_items WHERE service = 'Consulting'"
        )
        assert count == 2, (
            f"Attese 2 righe (una per tenant), trovate {count}"
        )

    async def test_upsert_batch_multipli_servizi(
        self,
        pg_pool: asyncpg.Pool,
    ) -> None:
        """Upsert di N servizi deve scrivere esattamente N righe."""
        items: list[dict[str, Any]] = [
            {
                "service": f"Servizio_{i}",
                "price": float(i * 100),
                "description": f"Descrizione {i}",
                "embedding": _unit_vec(i),
            }
            for i in range(3)
        ]
        written: int = await upsert_items(items=items, tenant_id=TENANT_A)
        assert written == 3

        count: int = await pg_pool.fetchval(
            "SELECT COUNT(*) FROM catalogue_items WHERE tenant_id = $1",
            TENANT_A,
        )
        assert count == 3


@pytest.mark.vector_store
class TestWipeTenant:
    """Correttezza di wipe_tenant — deve eliminare solo il tenant bersaglio."""

    async def test_wipe_rimuove_solo_tenant_bersaglio(
        self,
        pg_pool: asyncpg.Pool,
    ) -> None:
        """
        wipe_tenant(TENANT_A) deve eliminare le righe di TENANT_A
        e lasciare intatte quelle di TENANT_B.
        """
        await upsert_items(
            items=[
                {
                    "service": "Servizio A",
                    "price": 1.0,
                    "description": "desc",
                    "embedding": _unit_vec(0),
                }
            ],
            tenant_id=TENANT_A,
        )
        await upsert_items(
            items=[
                {
                    "service": "Servizio B",
                    "price": 2.0,
                    "description": "desc",
                    "embedding": _unit_vec(1),
                }
            ],
            tenant_id=TENANT_B,
        )

        deleted: int = await wipe_tenant(TENANT_A)
        assert deleted == 1, f"Attesa 1 riga eliminata, trovate {deleted}"

        righe_a: int = await pg_pool.fetchval(
            "SELECT COUNT(*) FROM catalogue_items WHERE tenant_id = $1",
            TENANT_A,
        )
        righe_b: int = await pg_pool.fetchval(
            "SELECT COUNT(*) FROM catalogue_items WHERE tenant_id = $1",
            TENANT_B,
        )

        assert righe_a == 0, "TENANT_A deve avere 0 righe dopo il wipe"
        assert righe_b == 1, "TENANT_B non deve essere influenzato dal wipe di TENANT_A"

    async def test_wipe_tenant_inesistente_ritorna_zero(
        self,
        pg_pool: asyncpg.Pool,  # noqa: ARG002
    ) -> None:
        """wipe_tenant su un tenant senza dati deve ritornare 0 senza errori."""
        deleted: int = await wipe_tenant("tenant_vuoto_xyz")
        assert deleted == 0

    async def test_wipe_ritorna_conteggio_corretto_multi_righe(
        self,
        pg_pool: asyncpg.Pool,  # noqa: ARG002
    ) -> None:
        """wipe_tenant deve ritornare il numero esatto di righe eliminate."""
        items: list[dict[str, Any]] = [
            {
                "service": f"Svc_{i}",
                "price": float(i),
                "description": "d",
                "embedding": _unit_vec(i),
            }
            for i in range(4)
        ]
        await upsert_items(items=items, tenant_id=TENANT_A)

        deleted: int = await wipe_tenant(TENANT_A)
        assert deleted == 4, f"Attese 4 righe eliminate, trovate {deleted}"
