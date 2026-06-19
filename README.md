# AI Lead Qualifier — V2 Enterprise

[![CI](https://github.com/LucaMureddu/AI-Lead-Qualifier/actions/workflows/ci.yml/badge.svg)](https://github.com/LucaMureddu/AI-Lead-Qualifier/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3110/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-FF6B35.svg)](https://www.langchain.com/langgraph)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Microservizio backend B2B multi-tenant per la qualificazione automatica dei lead e la generazione di preventivi. Architettura asincrona (ARQ/Redis), LLM on-premise (air-gapped), RAG semantico (PostgreSQL + pgvector), persistenza LangGraph via `AsyncPostgresSaver`, migrazioni schema con Alembic, e logging strutturato via structlog.

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

**RAG Enterprise su PostgreSQL (pgvector)**
Catalogo servizi vettorializzato con isolamento multi-tenant nativo SQL (`WHERE tenant_id = $1`). Indice HNSW per nearest-neighbour in O(log n). Dimensione vettore configurabile via `PGVECTOR_EMBEDDING_DIM`.

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
│   │       └── 002_tenant_profiles.py  # tenant_profiles (sostituisce filesystem JSON)
│   ├── api/
│   │   ├── routes.py            # Endpoint REST e Dependency Injection
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
│   │   └── tasks.py             # Task ARQ: qualification, resume, ingestion
│   ├── agents/                  # Nodi LangGraph (Sanitizer, Extractor, Mapper…)
│   ├── adapters/                # Delivery adapters (Webhook, Console)
│   ├── ingestion/               # Grafo e modelli per l'ingestion del catalogo
│   ├── services/
│   │   └── embeddings.py        # OllamaEmbeddings wrapper
│   ├── scripts/
│   │   └── migrate_profiles_to_db.py  # Utilità one-shot: JSON → Postgres
│   └── tests/
│       ├── unit/                # Logica pura, nessuna I/O
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
