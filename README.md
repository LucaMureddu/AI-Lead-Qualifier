# AI Lead Qualifier — V2 Enterprise

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
┌─────────────────────┐           │  • Schema gestito da Alembic  │
│   ARQ Worker(s)     │           └───────────────▲───────────────┘
│                     │                           │
│  ┌───────────────┐  │      ┌────────────────────┴────────────┐
│  │ LangGraph     │  │      │ Ollama / LLM Endpoint           │
│  │ Sanitizer     │  │      │ (Generazione & Embedding)       │
│  │ Extractor     ├──┼─────►│ host.docker.internal:11434      │
│  │ Mapper (RAG)  │  │      └─────────────────────────────────┘
│  │ Evaluator     │  │
│  │ Calculator    │  │
│  │ Delivery      │  │
│  └───────────────┘  │
└─────────────────────┘
```

---

## Feature principali V2

**Elaborazione asincrona (ARQ + Redis)**
FastAPI risponde immediatamente `202 Accepted` e accoda il job in Redis. Il client fa polling su `/status/{id}`. Nessuna connessione HTTP bloccante, nessun SSE nella qualification flow.

**Ciclo di vita asincrono (lifespan FastAPI)**
Lo startup esegue le operazioni critiche in sequenza rigorosa e senza race condition:
1. Configurazione structured logging (structlog)
2. Migrazioni Alembic (`alembic upgrade head`) — lo schema è sempre aggiornato prima della prima query
3. Apertura pool asyncpg (query applicative + vector store)
4. Inizializzazione `AsyncPostgresSaver` (LangGraph checkpointer su Postgres via psycopg3)
5. Compilazione grafi LangGraph (singleton thread-safe, riutilizzati per ogni richiesta)
6. Apertura pool Redis ARQ (enqueue job)

Lo shutdown chiude le risorse in ordine inverso. Il worker ARQ è un processo separato che usa lo stesso checkpointer.

**Schema e Migrazioni (Alembic)**
Il DB schema è versionato con Alembic. Le migration vengono applicate automaticamente all'avvio del backend via `asyncio.to_thread` (non bloccante). Non è necessario eseguirle manualmente. I file di migrazione si trovano in `backend/migrations/`.

**RAG Enterprise su PostgreSQL (pgvector)**
ChromaDB sostituito da Postgres con estensione pgvector: gestione robusta, transazionale e scalabile. Il catalogo servizi viene vettorializzato e archiviato con isolamento multi-tenant nativo SQL (`WHERE tenant_id = $1`). La dimensione del vettore è configurabile via `PGVECTOR_EMBEDDING_DIM` (default: 768 per `nomic-embed-text`).

**JWT RS256 asimmetrico**
Autenticazione con chiavi RSA pubbliche/private. I microservizi validano i token con la sola chiave pubblica, senza condividere il segreto di firma. In produzione: `TOKEN_ENDPOINT_ENABLED=false` disabilita l'endpoint `/token`.

**Human-in-the-Loop (HITL) adattivo**
Un `EvaluatorNode` calcola dinamicamente un confidence score per ogni lead. Se scende sotto soglia (match semantici poveri o max retries superati), il worker sospende l'esecuzione e scrive il checkpoint su Postgres. L'operatore sblocca il flusso tramite `POST /lead/{id}/approve`. Il worker ARQ riprende dall'esatto punto di interruzione.

**Logging strutturato (structlog)**
Tutti i log sono emessi in formato JSON strutturato via structlog. Ogni evento include campi come `event`, `tenant_id`, `thread_id`, `error`. Nessuna PII viene scritta nei log. I log sono integrabili nativamente con Promtail → Loki → Grafana.

**ARQ Worker configurabile per GPU**
La concorrenza del worker è controllata da `ARQ_MAX_JOBS` (default: 1 per GPU condivisa con LLM 14B). Il timeout per job è configurabile via `ARQ_JOB_TIMEOUT` (default: 300s).

**Osservabilità (PLG Stack)**
Stack opzionale (Promtail + Loki + Grafana) attivabile via `docker-compose.obs.yml`.

---

## Struttura del progetto

```
ai_lead_qualifier/
├── backend/
│   ├── main.py              # Entrypoint FastAPI + lifespan (startup/shutdown)
│   ├── alembic.ini          # Configurazione Alembic
│   ├── migrations/          # Script di migrazione DB (env.py + versioni)
│   ├── api/
│   │   ├── routes.py        # Endpoint REST e Dependency Injection
│   │   └── dependencies.py  # JWT RS256, get_redis(), get_graph()
│   ├── core/
│   │   ├── config.py        # Settings Pydantic (lru_cache singleton)
│   │   ├── state.py         # AgentState e LeadContext (TypedDict)
│   │   ├── graph.py         # Build/init LangGraph + get_checkpointer()
│   │   └── logging_setup.py # Configurazione structlog
│   ├── database/
│   │   ├── db_core.py       # Pool asyncpg (get_pool / close_pool)
│   │   └── vector_store.py  # Wrapper pgvector (upsert, search, wipe_tenant)
│   ├── worker/
│   │   ├── worker_settings.py # ARQ WorkerSettings (max_jobs, timeout, funzioni)
│   │   └── tasks.py           # Task ARQ: run_qualification_task, resume, ingestion
│   ├── agents/              # Nodi LangGraph (Sanitizer, Extractor, Mapper, Evaluator…)
│   ├── ingestion/           # Grafo e modelli per l'ingestion del catalogo
│   ├── services/
│   │   └── embeddings.py    # OllamaEmbeddings wrapper
│   └── tests/               # Unit, Integration (Testcontainers) ed Evals
├── frontend/                # UI Operatore — Vite + Alpine.js + Tailwind + Poller
├── keys/                    # Chiavi RSA (da generare) public.pem / private.pem
├── docker-compose.dev.yml   # Sviluppo: live reload, porte DB/Redis esposte
├── docker-compose.prod.yml  # Produzione: Traefik, rete isolata, TLS automatico
├── docker-compose.obs.yml   # Osservabilità: Loki, Grafana, Promtail
└── docs/RUNBOOK.md          # Procedure operative (rollback, incident response)
```

---

## Sviluppo locale

### 1. Setup iniziale

Copia il template dell'ambiente e genera le chiavi RSA per il JWT:

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

All'avvio il backend esegue automaticamente le migrazioni Alembic prima di servire traffico. Nei log vedrai la sequenza:

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

> **Live reload**: modifiche ai file in `backend/` riavviano automaticamente Uvicorn e il Worker ARQ grazie ai volumi montati.

### 3. Test

Prima di eseguire i test localmente, genera le chiavi JWT (necessarie anche per i test di integrazione):

```bash
mkdir -p backend/keys
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out backend/keys/private.pem
openssl rsa -pubout -in backend/keys/private.pem -out backend/keys/public.pem
```

```bash
make install          # Installa dipendenze di sviluppo nel venv locale
make test             # Unit + Integration (Testcontainers per Postgres/Redis)
make eval-local       # Evals LIVE con modello LLM reale (richiede Ollama sull'host)
```

La suite è divisa in tre livelli tramite marker pytest:

| Marker | Cosa copre | Richiede |
|---|---|---|
| `unit` | Logica pura, nessuna I/O | — |
| `integration` | Nodi e grafi con LLM/DB mockati | Chiavi JWT |
| `vector_store` | pgvector con container reale (Testcontainers) | Docker + chiavi JWT |
| `eval_live` | LLM-as-a-judge end-to-end | Ollama + Postgres con catalogo ingestito |

Per eseguire solo una categoria:

```bash
# Solo unit test
pytest -m unit

# Solo integration (no Docker)
pytest -m integration

# Solo pgvector (avvia container Docker automaticamente)
pytest -m vector_store

# Evals LIVE (richiede stack completo attivo)
pytest -m eval_live -v
```

**CI pipeline** (GitHub Actions): i job `Lint & Type-check`, `Test — Unit + eval_ci` e `Test — Integration pgvector` girano in parallelo ad ogni push. Gli evals live (`eval_live`) sono esclusi dalla CI e si eseguono solo manualmente.

---

## API

Tutti gli endpoint (eccetto `/health` e `/token`) richiedono `Authorization: Bearer <RS256_JWT>`.

### Lead Qualification (flusso asincrono)

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/lead` | Accoda un lead testuale. Restituisce `202 Accepted` + `thread_id`. |
| `GET` | `/status/{thread_id}` | Polling stato job: `queued` → `processing` → `pending_review` / `completed` / `error`. |
| `POST` | `/lead/{thread_id}/approve` | Sblocca un job `pending_review` (flusso HITL). |

### Catalogue Ingestion

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/upload` | Carica un file catalogo (CSV/JSON/XLSX). Restituisce il path server-side. |
| `POST` | `/ingest/stream` | Avvia l'ingestion via SSE. Emette `log`, `interrupt`, `done`, `error`. |
| `POST` | `/ingest/{thread_id}/approve` | Conferma o rifiuta l'ingestion dopo un interrupt HITL. |

### Tenant & Admin

| Metodo | Path | Descrizione |
|---|---|---|
| `GET` | `/tenants/{id}/profile` | Legge il profilo aziendale del tenant. |
| `PUT` | `/tenants/{id}/profile` | Aggiorna il profilo aziendale. |
| `DELETE` | `/api/v1/tenants/{id}/vector-data` | Hard reset dati vettoriali di un tenant (pgvector). |

### Ops

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/token` | *(Solo dev)* Genera un JWT RS256. Disabilitare in produzione. |
| `GET` | `/health` | Healthcheck — verifica connettività Postgres. Restituisce `503` se il pool non è disponibile. |

---

## Test E2E rapido (cURL)

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

# 4. Polling status (ripeti finché non esce da "queued"/"processing")
curl -s "http://localhost:8000/status/$THREAD" \
  -H "Authorization: Bearer $TOKEN" | jq '{status: .status, result: .result}'

# 5. Approva se pending_review
curl -s -X POST "http://localhost:8000/lead/$THREAD/approve" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'
```

---

## Deploy in produzione

L'architettura di produzione chiude tutte le porte dirette. Solo Traefik è esposto pubblicamente (80/443) con TLS automatico via Let's Encrypt.

> **Nota:** `docker-compose.prod.yml` è pensato per girare su un server **Linux**. Su macOS Docker Desktop il provider Docker di Traefik non funziona correttamente — usa `docker-compose.dev.yml` per lo sviluppo locale.

```bash
# Solo stack applicativo
docker compose -f docker-compose.prod.yml up -d --build

# Con osservabilità (Loki + Grafana + Promtail)
docker compose -f docker-compose.prod.yml -f docker-compose.obs.yml up -d
```

Grafana sarà disponibile su `http://localhost:3000` (credenziali: `admin` / `admin`).

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
| Observability | structlog (JSON structured logging) + Promtail + Loki + Grafana |
| Frontend | Vite + Alpine.js + Tailwind CSS |
| Testing | pytest + Testcontainers (Postgres, Redis) |
