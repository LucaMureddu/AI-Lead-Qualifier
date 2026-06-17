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

**Qualifica Lead RAG con thresholding semantico** — l'Extractor identifica i servizi richiesti via LLM, il Mapper li trova nel catalogo tenant via similarità coseno su ChromaDB. Match con distanza coseno superiore a `MAPPER_MAX_DISTANCE` (default `0.55` in `.env`) vengono scartati per evitare preventivi su servizi fuori catalogo. Se il mapping fallisce, il grafo ritenta fino a `MAX_RETRY_COUNT` volte con feedback negativo nel prompt; oltre soglia, escalation a fallback umano.

**Preservazione della lingua nel prompt dell'Extractor** — il system prompt dell'Extractor impone esplicitamente di mantenere la lingua originale della richiesta del lead (italiano → estrae in italiano, inglese → estrae in inglese). Questo abbassa drasticamente la distanza coseno nel Mapper quando il catalogo è scritto in una lingua diversa da quella dominante del modello LLM (es. Qwen/Llama che tendono a ragionare in inglese su cataloghi in italiano).

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

## 🐳 Deploy in Produzione (Docker)

L'intero sistema è pronto per essere eseguito in un Private Cloud o su qualsiasi server dotato di Docker, garantendo il 100% di parità di ambiente.

### Prerequisiti

- Docker e Docker Compose installati sul server.
- Un file `.env` configurato nella radice del progetto (vedi `.env.example`).

### Avvio del Cluster

Lancia questo comando dalla radice del progetto:

```bash
docker compose up --build -d
```

Questo accenderà 3 container isolati su una rete privata interna:

1. **vectordb** — il database vettoriale ChromaDB (porta non esposta all'esterno).
2. **backend** — il motore FastAPI / LangGraph (porta non esposta all'esterno).
3. **frontend** — un server Nginx ultra-leggero che serve la UI e fa da reverse proxy verso il backend.

### Accesso

Una volta avviato, l'interfaccia sarà disponibile all'indirizzo:

👉 **http://localhost:8080** (o l'IP del tuo server)

Il backend non è esposto direttamente: tutte le chiamate API passano in modo sicuro attraverso Nginx. Per il login, usa qualsiasi stringa come username (es. `cliente_acme_01`) — il sistema è configurato con autenticazione JWT mock; vedi `backend/api/security.py` per integrare un Identity Provider reale.

### Comandi utili

```bash
# Avvia in background
docker compose up --build -d

# Visualizza i log in tempo reale
docker compose logs -f

# Ferma tutto (i dati nei volumi vengono conservati)
docker compose down

# Ferma tutto E cancella i volumi (ChromaDB, SQLite, upload)
docker compose down -v
```

### Persistenza dei dati

I dati sopravvivono ai restart grazie a tre volumi Docker gestiti automaticamente:

| Volume | Contenuto |
|--------|-----------|
| `chroma_data` | Embedding vettoriali del catalogo (ChromaDB) |
| `sqlite_data` | Checkpoint LangGraph (thread di qualifica e ingestion) |
| `uploads_data` | File CSV/JSON/XLSX caricati dai tenant |

### Variabili d'ambiente chiave

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `MAPPER_MAX_DISTANCE` | `0.55` | Soglia distanza coseno: match scartati se distanza > valore. Valori più bassi = più restrittivo; consigliato 0.55 per cataloghi multilingua o con sinonimi. |
| `INGESTION_CHUNK_SIZE` | `10` | Righe per batch nel normalizer. Valori bassi (5–10) per LLM locali con limite di token. |
| `JWT_SECRET_KEY` | — | Segreto HMAC per la firma dei JWT. Obbligatorio in produzione. |
| `LLM_BASE_URL` | `http://host.docker.internal:11434/v1` | Endpoint OpenAI-compatible del modello locale (Ollama/LM Studio/vLLM). |

### Nota per server Linux

Se esegui i container su un server Linux e il tuo modello Ollama si trova sull'host (non in Docker), decommenta le righe `extra_hosts` nel `docker-compose.yml` per permettere al backend di risolvere `host.docker.internal`:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

---

## Stack tecnologico

| Ambito | Tecnologia |
|--------|-----------|
| Web framework | FastAPI + Uvicorn (ASGI), SSE via `StreamingResponse` |
| Orchestrazione agenti | LangGraph (`StateGraph`, `interrupt()`, `AsyncSqliteSaver`) |
| Database vettoriale | ChromaDB `0.5.3` (Client/Server, `HttpClient`) — versione client pinnata in `requirements.txt` per compatibilità con l'immagine Docker |
| Validazione & config | Pydantic v2 + `pydantic-settings` |
| Inferenza LLM | Ollama / LM Studio / vLLM (OpenAI-compatible); fallback Groq |
| HTTP client async | httpx |
| Persistenza checkpoint | SQLite via `aiosqlite` (`AsyncSqliteSaver`) |
| Autenticazione | JWT HS256 (`PyJWT`) |
| Testing | pytest, pytest-asyncio, ruff, mypy |
| Frontend | Vite + Alpine.js + Tailwind CSS |
