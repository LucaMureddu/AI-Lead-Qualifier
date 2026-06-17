# AI Lead Qualifier

> Microservizio backend B2B multi-tenant per la qualificazione automatica dei lead e la generazione di preventivi, basato su LLM locali (on-premise / air-gapped).

---

## Architettura

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client / CRM                             │
│          (HTTP JSON  ·  SSE stream  ·  Webhook)                 │
└───────────────────────────┬─────────────────────────────────────┘
                            │  JWT Bearer (HS256, per-tenant)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI  (ASGI / Uvicorn)                     │
│   /qualify/stream  ·  /ingest/stream  ·  /ingest/{id}/approve   │
│   /token  ·  /health                                            │
└──────────┬──────────────────────────────────┬───────────────────┘
           │                                  │
           ▼                                  ▼
┌─────────────────────┐           ┌───────────────────────┐
│  Qualification      │           │   Ingestion Engine    │
│  Pipeline           │           │   (LangGraph)         │
│  (LangGraph)        │           │                       │
│                     │           │  chunker → normalizer │
│  Sanitizer (PII)    │           │    → validator        │
│  → Extractor (LLM)  │           │    → [HITL interrupt] │
│  → Mapper (RAG)     │           │    → finalizer        │
│  → Calculator       │           └──────────┬────────────┘
│  → Delivery         │                      │
└──────────┬──────────┘                      │
           │                                 │ write
           │  query                          ▼
           └──────────────┐    ┌─────────────────────────┐
                          ▼    │     ChromaDB             │
              ┌───────────────────────────────────────┐  │
              │  catalogue_{tenant_id}  (per tenant)  │◄─┘
              └───────────────────────────────────────┘

  Persistenza checkpoint:  SQLite  (AsyncSqliteSaver)
  Inferenza LLM:           Ollama / LM Studio / vLLM  (OpenAI-compatible)
```

### Feature chiave

**Sicurezza JWT Multi-Tenant** — ogni richiesta porta un token JWT firmato con `JWT_SECRET_KEY`. Il claim `sub` determina il `tenant_id`, che scopa collezioni ChromaDB, upload, log e checkpoint. Zero mixing tra tenant a livello applicativo.

**Ingestion con Human-in-the-Loop** — il grafo di ingestion si auto-sospende via `interrupt()` di LangGraph quando la confidenza media scende sotto 0.75 o almeno un item è flaggato. Il checkpoint viene persistito su SQLite; l'operatore riprende con `POST /ingest/{thread_id}/approve`.

**Qualifica Lead RAG con thresholding semantico** — l'Extractor identifica i servizi richiesti via LLM, il Mapper li trova nel catalogo tenant via similarità coseno su ChromaDB (soglia `MAPPER_MAX_DISTANCE`). Se il mapping fallisce, il grafo ritenta fino a `MAX_RETRY_COUNT` volte con feedback negativo nel prompt; oltre soglia, escalation a fallback umano.

**Delivery Layer a plugin** — il `BaseDeliveryAdapter` disaccoppia la consegna del preventivo: `ConsoleAdapter` in sviluppo, `WebhookAdapter` (httpx async, retry gestito dallo stato LangGraph) in produzione. La `factory` è il solo punto da estendere per aggiungere canali.

---

## Struttura del progetto

```
ai_lead_qualifier/
├── .env.example          # template variabili d'ambiente (copia in .env)
├── .gitignore
├── Makefile              # comandi make: test, cov, lint, eval-*
├── README.md
│
├── backend/              # tutto il codice Python
│   ├── main.py           # entrypoint FastAPI / Uvicorn
│   ├── api/
│   │   ├── routes.py     # endpoint REST + SSE
│   │   └── security.py   # JWT dependency injection
│   ├── core/
│   │   ├── config.py     # Settings (pydantic-settings, carica .env)
│   │   ├── graph.py      # grafo LangGraph della qualification pipeline
│   │   └── state.py      # LeadState TypedDict
│   ├── agents/           # nodi del grafo di qualifica
│   │   ├── sanitizer.py
│   │   ├── extractor.py
│   │   ├── mapper.py
│   │   ├── calculator.py
│   │   └── delivery.py
│   ├── ingestion/        # grafo LangGraph di ingestion
│   │   ├── graph.py
│   │   └── models.py     # ServiceItem (schema canonico Pydantic)
│   ├── adapters/         # delivery adapter pattern
│   │   ├── base.py
│   │   ├── console.py
│   │   ├── webhook.py
│   │   └── factory.py
│   ├── tests/
│   │   ├── unit/         # test puri, nessuna I/O
│   │   ├── integration/  # grafo/API con LLM e ChromaDB mockati
│   │   └── evals/        # piramide eval: CI (deterministico) + LIVE (LLM)
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── requirements-ci.txt
│   └── pyproject.toml    # configurazione pytest, coverage, ruff, mypy
│
└── frontend/             # UI operatore (Vite + Alpine.js + Tailwind)
    ├── src/
    └── ...
```

---

## Guida allo sviluppo

### Prerequisiti

- Python 3.11+
- [Ollama](https://ollama.com) con un modello scaricato (es. `ollama pull llama3`)
- [ChromaDB](https://www.trychroma.com) in modalità server

### Setup iniziale

```bash
# 1. Configura le variabili d'ambiente
cp .env.example .env
# Modifica .env: URL LLM, JWT_SECRET_KEY, porte, ecc.

# 2. Installa le dipendenze Python
make install          # dipendenze complete (sviluppo locale)
# oppure:
make install-ci       # dipendenze minime per CI (no llama-cpp/groq)
```

### Avvio dei servizi

```bash
# ChromaDB server (porta 8001, come da .env.example)
chroma run --host localhost --port 8001

# Ollama
ollama serve

# FastAPI
cd backend && python main.py
# Swagger UI: http://localhost:8000/docs
# Health check: http://localhost:8000/health
```

### Eseguire i test

```bash
make test             # unit + integration + evals CI (~30s, no LLM reale)
make cov              # stessa suite con report di copertura
make eval-local       # evals LIVE con modello reale (richiede Ollama attivo)
make eval-snapshot    # rigenera gli snapshot dopo modifiche a prompt/modello
make lint             # ruff + mypy
make check            # lint + test (gate da eseguire prima di ogni PR)
```

La suite si divide in tre livelli:

- **Unit** (`tests/unit/`) — test puri, nessuna I/O, veloci.
- **Integration** (`tests/integration/`) — grafo LangGraph e API con LLM e ChromaDB mockati.
- **Evals** (`tests/evals/`) — due binari: CI (deterministico, coseno su snapshot registrati) e LIVE (giudice LLM, solo locale con `make eval-local`).

Gli snapshot in `tests/evals/snapshots/` vanno versionati; rigenerarli solo dopo modifiche intenzionali a prompt o modello.

### Ottenere un token JWT (sviluppo)

```bash
# Genera un token per il tenant "acme" via endpoint /token
curl -X POST http://localhost:8000/token \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "acme"}'
```

---

## API — Endpoint principali

| Metodo | Path | Descrizione |
|--------|------|-------------|
| `POST` | `/token` | Genera un JWT per `tenant_id` (helper sviluppo) |
| `GET`  | `/health` | Health check |
| `POST` | `/qualify/stream` | Qualifica un lead, risposta SSE in real-time |
| `POST` | `/qualify` | Qualifica un lead, risposta JSON sincrona |
| `POST` | `/ingest/stream` | Carica e normalizza un catalogo, risposta SSE |
| `POST` | `/ingest/{thread_id}/approve` | Riprende un'ingestion sospesa (HITL) |

Tutti gli endpoint tranne `/token` e `/health` richiedono `Authorization: Bearer <jwt>`.

---

## Deploy

> Sezione in preparazione — pronta per accogliere i comandi Docker.

```bash
# Coming soon:
# docker-compose up
```

I servizi da orchestrare saranno: `backend` (FastAPI), `chromadb`, `ollama`.

---

## Stack tecnologico

| Ambito | Tecnologia |
|--------|-----------|
| Web framework | FastAPI + Uvicorn (ASGI), SSE via `StreamingResponse` |
| Orchestrazione agenti | LangGraph (`StateGraph`, `interrupt()`, `AsyncSqliteSaver`) |
| Database vettoriale | ChromaDB (Client/Server, `HttpClient`) |
| Validazione & config | Pydantic v2 + `pydantic-settings` |
| Inferenza LLM | Ollama / LM Studio / vLLM (OpenAI-compatible); fallback Groq |
| HTTP client async | httpx |
| Persistenza checkpoint | SQLite via `aiosqlite` (`AsyncSqliteSaver`) |
| Autenticazione | JWT HS256 (`PyJWT`) |
| Testing | pytest, pytest-asyncio, ruff, mypy |
| Frontend | Vite + Alpine.js + Tailwind CSS |
