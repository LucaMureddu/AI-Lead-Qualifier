# AI Lead Qualifier — V3 Pricing Ibrido Tipizzato

[![CI](https://github.com/LucaMureddu/AI-Lead-Qualifier/actions/workflows/ci.yml/badge.svg)](https://github.com/LucaMureddu/AI-Lead-Qualifier/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3110/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-FF6B35.svg)](https://www.langchain.com/langgraph)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Microservizio backend B2B multi-tenant per la qualificazione automatica dei lead e la generazione di preventivi. Architettura asincrona (ARQ/Redis), LLM on-premise (air-gapped), RAG semantico (PostgreSQL + pgvector), **pricing ibrido tipizzato** (`FIXED` / `FREE` / `VARIABLE`) con invarianza matematica garantita a livello DB via `CHECK` constraint, persistenza LangGraph via `AsyncPostgresSaver`, migrazioni schema con Alembic, e logging strutturato via structlog.

---

## Architettura

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client / UI / CRM                        │
│       (HTTP JSON  ·  Polling API  ·  Webhook Delivery)          │
└───────────────────────────┬─────────────────────────────────────┘
                            │  JWT Bearer (RS256 asimmetrico)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Traefik (TLS + Let's Encrypt)                 │
└───────────────────────────┬─────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI  (ASGI / Uvicorn)                    │
│   /lead (202)  ·  /status/{id}  ·  /lead/{id}/approve           │
└──────────┬──────────────────────────────────┬───────────────────┘
           │ Accoda Job (ARQ)                 │ Polling Stato
           ▼                                  ▼
┌─────────────────────┐           ┌───────────────────────────────┐
│     Redis (ARQ)     │           │      PostgreSQL 16 + pgvector │
│   Job Broker/Queue  │           │                               │
└──────────┬──────────┘           │  • Checkpoint LangGraph       │
           │                      │    (AsyncPostgresSaver)       │
           ▼                      │  • Vector Store (pgvector)    │
┌─────────────────────┐           │  • Tenant Profiles            │
│   ARQ Worker(s)     │           │  • Schema gestito da Alembic  │
│                     │           └───────────────▲───────────────┘
│  ┌───────────────┐  │                           │
│  │ LangGraph     │  │      ┌────────────────────┴────────────┐
│  │ Sanitizer     │  │      │ Ollama / LLM Endpoint           │
│  │ Extractor     ├──┼─────►│ (Generazione & Embedding)       │
│  │ Mapper (RAG)  │  │      │ host.docker.internal:11434      │
│  │ Evaluator     │  │      └─────────────────────────────────┘
│  │ Calculator    │  │
│  │ Delivery      │  │
│  └───────────────┘  │
└─────────────────────┘
```

**Flusso di qualificazione:**

```
SanitizerNode → ExtractorNode → MapperNode → EvaluatorNode
                    ▲                              │
                    │         score ≥ 0.75         ▼
                    │                       CalculatorNode → DeliveryNode → END
                    │
                    │    score < 0.75 & retry < max
                    └──────────────────────────────
                         score < 0.75 & retry == max
                                    ▼
                           hitl_interrupt (pending_review)
```

---

## Feature principali

**Elaborazione asincrona (ARQ + Redis)**
FastAPI risponde immediatamente `202 Accepted` e accoda il job in Redis. Il client fa polling su `/status/{id}`. Nessuna connessione HTTP bloccante.

**Ciclo di vita asincrono (lifespan FastAPI)**
Startup in sequenza rigorosa: structured logging → migrazioni Alembic → pool asyncpg → `AsyncPostgresSaver` → compilazione grafi LangGraph → pool Redis ARQ. Shutdown in ordine inverso, senza race condition.

**Schema e Migrazioni (Alembic)**
Il DB schema è versionato con Alembic. Le migration vengono applicate automaticamente all'avvio del backend. I file si trovano in `backend/migrations/versions/`.

| Migration | Contenuto |
|---|---|
| `001_initial_schema` | Estensione `vector`, tabella `catalogue_items`, indice HNSW coseno |
| `002_tenant_profiles` | Tabella `tenant_profiles` (JSONB) — sostituisce il filesystem JSON |
| `003_audit_log` | Tabella `audit_log` — traccia ogni modifica ai campi di `catalogue_items` |
| `004_hybrid_pricing` | Colonna `price_type VARCHAR(20) NOT NULL`, `price` diventa nullable, `CHECK` constraint che garantisce l'invariante `C(price_type, price)` |

**RAG Enterprise su PostgreSQL (pgvector)**
Catalogo servizi vettorializzato con isolamento multi-tenant nativo SQL (`WHERE tenant_id = $1`). Indice HNSW per nearest-neighbour in O(log n). Dimensione vettore configurabile via `PGVECTOR_EMBEDDING_DIM`.

**Pricing Ibrido Tipizzato (V3)**
La colonna `price_type` (`FIXED` / `FREE` / `VARIABLE`) è una colonna di prima classe nel DB, non un flag derivato. Un `CHECK` constraint PostgreSQL garantisce l'invariante `C(price_type, price)` a livello di motore: `VARIABLE ⟺ price IS NULL` (aggregati SQL corretti by-default, nessun sentinel `-1.0`), `FREE ⟺ price = 0.0`, `FIXED ⟺ price IS NOT NULL AND price ≥ 0`. La property `is_computable` su `ServiceItem` è la fonte canonica dell'informazione; i dict `mapped_services` nella pipeline LangGraph ne sono la proiezione serializzata.

**JWT RS256 asimmetrico**
I microservizi validano i token con la sola chiave pubblica, senza condividere il segreto di firma. In produzione: `TOKEN_ENDPOINT_ENABLED=false` disabilita l'endpoint `/token`.

**Human-in-the-Loop (HITL) adattivo**
`EvaluatorNode` calcola un confidence score dopo il mapping semantico. Se scende sotto soglia (o i retry sono esauriti), sospende l'esecuzione e persiste il checkpoint in Postgres. L'operatore sblocca il flusso tramite `POST /lead/{id}/approve`.

**Logging strutturato (structlog)**
Log JSON strutturati con campi `event`, `tenant_id`, `thread_id`, `error`. Zero PII nei log. Integrabili nativamente con Promtail → Loki → Grafana.

**Osservabilità (PLG Stack)**
Stack opzionale Promtail + Loki + Grafana attivabile via `docker-compose.obs.yml`.

---

## Stack tecnologico

| Ambito | Tecnologia |
|---|---|
| Web Framework | FastAPI + Uvicorn (ASGI) |
| Agent Orchestration | LangGraph (StateGraph, `interrupt()`, `AsyncPostgresSaver`) |
| Persistence & Vector DB | PostgreSQL 16 + pgvector + asyncpg |
| Schema Migrations | Alembic (applicato automaticamente allo startup) |
| Task Queue | ARQ + Redis 7 |
| LLM Inference | Ollama / LM Studio / vLLM (OpenAI-compatible) |
| Auth & Security | PyJWT (RS256 asimmetrico, chiavi PEM) |
| Observability | structlog (JSON) + Promtail + Loki + Grafana |
| Frontend | Vite + Alpine.js + Tailwind CSS |
| Testing | pytest + Testcontainers + Playwright |
| Linting / Types | Ruff + Mypy |
| Load Testing | Locust |

---

## Sviluppo locale

### 1. Setup iniziale

```bash
cp .env.example .env

mkdir -p backend/keys
openssl genrsa -out backend/keys/private.pem 2048
openssl rsa -in backend/keys/private.pem -outform PEM -pubout -out backend/keys/public.pem
```

### 2. Avvio

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

All'avvio il backend esegue automaticamente le migrazioni Alembic prima di servire traffico:

```
startup.begin
alembic.migrations_applied
startup.asyncpg_pool_ready
startup.graphs_compiled
startup.arq_pool_ready
```

| Servizio | URL |
|---|---|
| Frontend UI | http://localhost |
| Backend API (Swagger) | http://localhost:8000/docs |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

> **Live reload**: modifiche ai file in `backend/` riavviano automaticamente Uvicorn e il Worker ARQ.

---

## Testing

La suite è organizzata in quattro livelli, ognuno con requisiti e velocità diverse.

### Piramide dei test

```
         ┌──────────────────┐
         │   eval_live      │  LLM-as-judge end-to-end (manuale, richiede Ollama)
         ├──────────────────┤
         │   vector_store   │  pgvector con container reale (Testcontainers)
         ├──────────────────┤
         │   integration    │  Nodi e grafi con LLM/DB mockati
         ├──────────────────┤
         │      unit        │  Logica pura, nessuna I/O
         └──────────────────┘
```

| Marker | Cosa copre | Richiede | CI |
|---|---|---|---|
| `unit` | Router, calculator, adapters, sanitizer — logica pura | — | ✅ Automatico |
| `integration` | Nodi LangGraph, API endpoints, grafi completi | Chiavi JWT | ✅ Automatico |
| `vector_store` | pgvector con container Postgres reale (Testcontainers) | Docker + chiavi JWT | ✅ Automatico |
| `eval_live` | LLM-as-judge end-to-end con modello reale | Ollama + Postgres con catalogo | ❌ Solo manuale |

### Comandi

```bash
make install          # Installa dipendenze di sviluppo nel venv locale

pytest -m unit                    # Solo unit test (~10s)
pytest -m integration             # Integration (no Docker, ~30s)
pytest -m vector_store            # pgvector con container Docker (~60s)
pytest -m eval_live -v            # Evals LIVE (richiede stack completo)

make test                         # Unit + Integration (come in CI)
make eval-local                   # Evals LIVE con LLM reale
```

### Pipeline CI (GitHub Actions)

La CI si attiva ad ogni push e PR verso `main`. I job di test girano in parallelo dopo il lint.

```
Lint & Type-check (Ruff + Mypy)
        │
        ├── Test — Unit + eval_ci
        │
        ├── Test — Integration pgvector (Testcontainers)
        │
        └── E2E — Frontend (Playwright)
```

Gli evals live (`eval_live`) sono esclusi dalla CI per evitare dipendenze da GPU/Ollama in cloud.

---

## API

Tutti gli endpoint (eccetto `/health` e `/token`) richiedono `Authorization: Bearer <RS256_JWT>`.

### Lead Qualification (flusso asincrono)

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/lead` | Accoda un lead testuale. Restituisce `202 Accepted` + `thread_id`. |
| `GET` | `/status/{thread_id}` | Polling stato: `queued` → `processing` → `pending_review` / `completed` / `error`. |
| `POST` | `/lead/{thread_id}/approve` | Sblocca un job `pending_review` (HITL). |

### Catalogue Ingestion

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/upload` | Carica un file catalogo (CSV/JSON/XLSX). |
| `POST` | `/ingest/stream` | Avvia l'ingestion via SSE. Emette `log`, `interrupt`, `done`, `error`. |
| `POST` | `/ingest/{thread_id}/approve` | Conferma o rifiuta l'ingestion dopo un interrupt HITL. |

### Catalogue Admin

| Metodo | Path | Descrizione |
|---|---|---|
| `GET` | `/api/catalog/items` | Lista paginata dei servizi del tenant (`?skip=0&limit=20`). |
| `PATCH` | `/api/catalog/items/{id}` | Aggiorna nome, prezzo, `price_type` o descrizione di un servizio. Scrive `audit_log`, rigenera l'embedding via ARQ (solo se cambiano `service`/`description`). Un PATCH incoerente (es. `price_type=VARIABLE` con `price=150`) restituisce `422`. |

### Tenant & Admin

| Metodo | Path | Descrizione |
|---|---|---|
| `GET` | `/tenants/{id}/profile` | Legge il profilo aziendale del tenant (da Postgres). |
| `PUT` | `/tenants/{id}/profile` | Aggiorna il profilo aziendale. |
| `DELETE` | `/api/v1/tenants/{id}/vector-data` | Hard reset dati vettoriali del tenant (pgvector). |

### Ops

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/token` | *(Solo dev)* Genera un JWT RS256. Disabilitare in produzione. |
| `GET` | `/health` | Healthcheck — verifica connettività Postgres **e** Redis. Restituisce `503` se uno dei due è down. |

### Test E2E rapido (cURL)

```bash
# 1. Health check
curl http://localhost:8000/health

# 2. Ottieni JWT (dev only)
TOKEN=$(curl -s -X POST http://localhost:8000/token \
  -H "Content-Type: application/json" \
  -d '{"username": "tenant_test"}' | jq -r '.access_token')

# 3. Invia lead → 202 + thread_id
THREAD=$(curl -s -X POST http://localhost:8000/lead \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "Consulenza IT per migrazione cloud di 50 server e supporto continuativo 12 mesi."}' \
  | jq -r '.thread_id')

# 4. Polling status
curl -s "http://localhost:8000/status/$THREAD" \
  -H "Authorization: Bearer $TOKEN" | jq '{status: .status, result: .result}'

# 5. Approva se pending_review
curl -s -X POST "http://localhost:8000/lead/$THREAD/approve" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'
```

---

## Struttura del progetto

```
ai_lead_qualifier/
├── backend/
│   ├── main.py                  # Entrypoint FastAPI + lifespan (startup/shutdown)
│   ├── alembic.ini              # Configurazione Alembic
│   ├── migrations/              # Script di migrazione DB
│   │   └── versions/
│   │       ├── 001_initial_schema.py   # pgvector + catalogue_items
│   │       ├── 002_tenant_profiles.py  # tenant_profiles (sostituisce filesystem JSON)
│   │       ├── 003_audit_log.py        # audit_log (traccia modifiche campo per campo)
│   │       └── 004_hybrid_pricing.py   # price_type + price nullable + CHECK constraint
│   ├── api/
│   │   ├── routes.py            # Endpoint REST e Dependency Injection
│   │   ├── catalogue_routes.py  # Admin catalogo: GET /items, PATCH /items/{id}
│   │   └── dependencies.py      # JWT RS256, get_redis(), get_graph()
│   ├── core/
│   │   ├── config.py            # Settings Pydantic (lru_cache singleton)
│   │   ├── state.py             # AgentState e LeadContext (TypedDict)
│   │   ├── graph.py             # Build/init LangGraph + get_checkpointer()
│   │   └── logging_setup.py     # Configurazione structlog
│   ├── database/
│   │   ├── db_core.py           # Pool asyncpg (get_pool / close_pool)
│   │   ├── vector_store.py      # Wrapper pgvector (upsert, search, wipe_tenant)
│   │   └── profiles.py          # Profili tenant su Postgres (get_profile, upsert_profile)
│   ├── worker/
│   │   ├── worker_settings.py   # ARQ WorkerSettings
│   │   └── tasks.py             # Task ARQ: qualification, resume, ingestion, update_embedding
│   ├── agents/                  # Nodi LangGraph (Sanitizer, Extractor, Mapper…)
│   ├── adapters/                # Delivery adapters (Webhook, Console)
│   ├── ingestion/               # Grafo e modelli per l'ingestion del catalogo
│   ├── services/
│   │   └── embeddings.py        # OllamaEmbeddings wrapper
│   ├── scripts/
│   │   └── migrate_profiles_to_db.py  # Utilità one-shot: JSON → Postgres
│   └── tests/
│       ├── unit/                # Logica pura, nessuna I/O
│       │   └── test_catalogue_api.py  # Test admin catalogo + worker embedding
│       ├── integration/         # Nodi e API con mock
│       │   └── vector_store/    # pgvector con Testcontainers
│       └── evals/               # LLM-as-judge + golden datasets
├── frontend/                    # UI Operatore — Vite + Alpine.js + Tailwind
├── observability/               # Config Promtail + Loki + Grafana
├── docs/RUNBOOK.md              # Procedure operative (backup, restore, incident)
├── locustfile.py                # Load test Locust (lead_resolution_time custom event)
├── docker-compose.dev.yml       # Sviluppo: live reload, porte DB/Redis esposte
├── docker-compose.prod.yml      # Produzione: Traefik, rete isolata, TLS automatico
└── docker-compose.obs.yml       # Osservabilità: Loki, Grafana, Promtail
```

---

## Deploy in produzione

L'architettura di produzione chiude tutte le porte dirette. Solo Traefik è esposto pubblicamente (80/443) con TLS automatico via Let's Encrypt.

> **Nota:** `docker-compose.prod.yml` è pensato per girare su un server **Linux**. Su macOS Docker Desktop il provider Docker di Traefik non funziona correttamente.

```bash
# Solo stack applicativo
docker compose -f docker-compose.prod.yml up -d --build

# Con osservabilità (Loki + Grafana + Promtail)
docker compose -f docker-compose.prod.yml -f docker-compose.obs.yml up -d
```

Grafana sarà disponibile su `http://localhost:3000` (credenziali: `admin` / `admin`).

Per le procedure operative (backup Postgres, restore, flush coda ARQ, riavvio worker GPU OOM) consultare [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## Variabili d'ambiente (`.env`)

| Variabile | Descrizione | Default dev |
|---|---|---|
| `DATABASE_DSN` | Connection string Postgres (asyncpg) | `postgresql://app:password@postgres/ai_lead_qualifier` |
| `REDIS_DSN` | Connection string Redis (ARQ) | `redis://redis:6379` |
| `JWT_PUBLIC_KEY_PATH` | Path a `public.pem` per validare i token | `keys/public.pem` |
| `JWT_PRIVATE_KEY_PATH` | Path a `private.pem` (solo `/token` dev) | `keys/private.pem` |
| `TOKEN_ENDPOINT_ENABLED` | Abilita `POST /token`. **Impostare `false` in prod.** | `true` |
| `LLM_BASE_URL` | Endpoint OpenAI-compatible | `http://host.docker.internal:11434/v1` |
| `LLM_MODEL_NAME` | Modello LLM | `llama3` |
| `EMBEDDING_BASE_URL` | Endpoint Ollama per embedding | `http://host.docker.internal:11434` |
| `EMBEDDING_MODEL` | Modello embedding | `nomic-embed-text` |
| `PGVECTOR_EMBEDDING_DIM` | Dimensione vettore (deve coincidere col modello) | `768` |
| `ARQ_MAX_JOBS` | Concorrenza worker (1 per GPU condivisa) | `1` |
| `ARQ_JOB_TIMEOUT` | Timeout singolo job ARQ (secondi) | `300` |
| `CORS_ORIGINS` | Origini CORS ammesse (JSON array) | `["http://localhost"]` |
| `ACME_EMAIL` | Email Let's Encrypt (solo prod) | — |
| `API_DOMAIN` | Dominio pubblico API (solo prod) | — |

---

## Changelog

### V3.0 — Pricing Ibrido Tipizzato (2026-06-21)

**Motivazione:** il flag implicito `metadata.is_on_request` era inaffidabile — un valore `price=0.0` poteva significare sia "gratuito" sia "da preventivare", rendendo gli aggregati SQL potenzialmente errati. V3 promuove il tipo del prezzo a colonna di prima classe con invarianza garantita a livello di motore DB.

**Schema DB (migration `004_hybrid_pricing`)**
- `catalogue_items`: aggiunta colonna `price_type VARCHAR(20) NOT NULL DEFAULT 'FIXED'`
- `price` diventa nullable: `VARIABLE ⟺ price IS NULL` (no sentinel `-1.0`)
- Nuovo `CHECK` constraint `chk_hybrid_pricing_logic` che impone l'invariante `C(price_type, price)` a livello PostgreSQL — un INSERT/UPDATE incoerente è respinto dal motore prima di toccare l'applicazione

**Modelli Pydantic (`ingestion/models.py`)**
- Nuovo enum `PriceType` (`FIXED` / `FREE` / `VARIABLE`) come `str, Enum`
- Campo `price_type: PriceType` su `ServiceItem` con inferenza automatica (validator `mode="before"`: `price is None ⇒ VARIABLE`, altrimenti `FIXED`; `FREE` solo su scelta esplicita)
- `model_validator mode="after"` di coercizione: `FREE → price=0.0`, `VARIABLE → price=None`, `FIXED + price=None → ValueError` (loud failure)
- Property `is_computable → bool`: fonte canonica della computabilità; i dict della pipeline ne sono la proiezione serializzata

**Pipeline LangGraph**
- `ingestion/graph.py`: `price_type: item.price_type.value` sostituisce `is_on_request: item.price is None`; `price` passa come `None` per VARIABLE (non coerco a `0.0`)
- `agents/mapper.py`: `"price_type": best.metadata.get("price_type", "FIXED")` nei dict `mapped_services`
- `agents/calculator.py`: guardia `price_type == "VARIABLE"` prima di `float(entry["price"])` — impossibile `TypeError` su `None`
- `agents/delivery.py`: branch su `price_type` per il testo della email (VARIABLE → "su richiesta", FREE → "Gratis", FIXED → "{price:.2f} €")

**API Admin (`catalogue_routes.py`)**
- `price_type` aggiunto a `_PATCHABLE_COLUMNS`, `CatalogueItemPatch`, `CatalogueItemResponse`, `CatalogueItemPatchResponse`
- `asyncpg.CheckViolationError` intercettato → `422` con messaggio esplicativo
- Re-embedding asincrono accodato solo se cambiano `service` o `description` (`embedding_sync: "not_needed"` per patch di solo `price_type`)

**Test**
- **Unit** `test_models.py`: 9 nuovi casi (coercizione VARIABLE/FREE/FIXED, inferenza automatica, `is_computable`)
- **Unit** `test_calculator.py`: riscritto con `price_type`; nuovi casi `test_mathematical_invariance`, `test_variable_price_none_does_not_raise`
- **Unit** `test_catalogue_api.py`: 3 nuovi test (schema coercizione PATCH, `CheckViolationError → 422`, skip re-embedding su price_type-only)
- **Integration** `test_vector_store.py`: `price_type` aggiunto a tutti i fixture; 2 nuovi test (VARIABLE → NULL, FREE → 0.0 nel DB); nuova classe `TestHybridPricingConstraint` con 4 test sul CHECK constraint reale (Testcontainers)
- **Integration** `test_graph_qualify.py`, `test_nodes_qualify.py`, `test_api_qualify.py`: `price_type: "FIXED"` aggiunto a tutti i `mapped_services` mock

### V2.2 — Catalogue Admin (2026-06-19)

**Nuove funzionalità**
- **Admin catalogo (backend)**: `GET /api/catalog/items` (lista paginata) e `PATCH /api/catalog/items/{id}` (modifica parziale con validazione prezzo `ge=0` → 422, transazione atomica UPDATE + `audit_log`, dispatch ARQ)
- **Audit log**: nuova tabella `audit_log` (migration `003`) che traccia ogni modifica campo per campo (`field_changed`, `old_value`, `new_value`, `timestamp`) — nessun record viene mai eliminato per garantire la tracciabilità
- **Riesecuzione embedding asincrona**: `update_embedding_task` (ARQ) legge il record aggiornato, ricostruisce il testo con `_row_to_text`, ricalcola il vettore via Ollama e aggiorna `pgvector` — eventual consistency, il flusso di qualifica non è bloccato
- **UI catalogo (frontend)**: nuova sezione "Catalogo Servizi" con tabella paginata (Alpine.js + `$store.catalog`), modal di modifica con spinner, validazione lato client e ricarica automatica post-salvataggio
- **Test**: `test_catalogue_api.py` — 10 test unitari per validazione Pydantic, list, PATCH happy-path, 404, 422 prezzo negativo, audit no-op, worker (happy path, not found, EmbeddingError propagation)

### V2.1 — Security & Optimizations (2026-06-19)

**Security**
- **Fix IDOR** nel `DELETE /api/v1/tenants/{id}/vector-data`: verifica che il `tenant_id` del JWT coincida con il parametro del path, impedendo a un tenant di cancellare i dati vettoriali di un altro
- **Health check rafforzato**: `/health` verifica anche Redis oltre a Postgres — se ARQ è down, il servizio risponde `503` invece di accettare job non processabili

**Performance**
- **Worker singleton**: il grafo LangGraph compilato è ora un singleton di processo nel worker ARQ, costruito una sola volta al primo job invece che ad ogni invocazione

**Refactor**
- **Rimosso dead code**: `route_after_mapper` e il riferimento al nodo `human_fallback` (inesistente) eliminati da `core/graph.py`
- **Profili tenant su Postgres**: rimossa la persistenza su filesystem JSON (`data/profiles/`), sostituita dalla tabella `tenant_profiles` (migration `002`). Risolve la mancata consistenza in deploy multi-istanza

### V2.0 — Enterprise Rewrite

- Elaborazione asincrona ARQ + Redis (202 + polling) al posto delle SSE
- `AsyncPostgresSaver` per il checkpointing LangGraph su Postgres
- ChromaDB sostituito da pgvector (RAG transazionale, multi-tenant nativo)
- JWT HS256 → RS256 asimmetrico
- `EvaluatorNode` + HITL adattivo basato su confidence score
- Logging strutturato con structlog (JSON, zero PII)
- Stack osservabilità PLG (Promtail + Loki + Grafana)
- Suite test a quattro livelli (unit / integration / vector_store / eval_live)
