# AI Lead Qualifier

> Microservizio backend B2B multi-tenant per la qualificazione automatica dei lead e la generazione di preventivi, basato su LLM eseguiti interamente in locale (on-premise / air-gapped).

---

## 1. Panoramica del Progetto

**AI Lead Qualifier** è un servizio backend SaaS B2B che trasforma una richiesta testuale non strutturata proveniente da un potenziale cliente (lead) in un preventivo strutturato e calcolato, recapitandolo poi al sistema esterno del tenant (CRM, webhook, ecc.).

Il flusso end-to-end è il seguente: un lead invia del testo libero (es. il corpo di un'email o la compilazione di un form), il sistema lo ripulisce dai dati sensibili, ne estrae i servizi richiesti tramite LLM, li mappa contro il listino prezzi del tenant memorizzato su database vettoriale, calcola il totale del preventivo e infine lo consegna al sistema di destinazione.

A monte di questo flusso esiste un secondo motore, l'**Ingestion Engine**, che si occupa di onboardare il catalogo servizi di ciascun tenant: legge file grezzi ed eterogenei (CSV/JSON/Excel), li normalizza in uno schema canonico tramite LLM, li valida e — quando la confidenza è bassa — sospende l'esecuzione per una revisione umana prima di scriverli su database.

### Principi cardine

- **100% Local AI / Air-gapped.** Tutta l'inferenza LLM avviene su un server locale OpenAI-compatibile (Ollama, LM Studio, vLLM). Non vengono importate librerie di telemetria o analytics, e tutte le chiamate di rete puntano a servizi locali (LLM, ChromaDB). Esiste un fallback opzionale verso l'API cloud Groq, da disabilitare per un deployment completamente isolato.

- **Multi-Tenancy logica.** L'isolamento tra i clienti è garantito a livello applicativo: ogni tenant possiede una collezione ChromaDB dedicata, denominata `catalogue_{tenant_id}`. Il `tenant_id` è presente a ogni livello dello stato (lead, item di catalogo, log) e viene propagato attraverso tutta la pipeline, scopando query, scritture su database e prefissi di log.

- **Asincronia nativa.** L'intera applicazione è costruita su uno stack async (FastAPI + `astream`/`ainvoke` di LangGraph). Le chiamate verso backend bloccanti (Groq SDK, llama-cpp-python, client sincrono di ChromaDB) non bloccano mai l'event loop: vengono offloadate a un thread pool tramite `asyncio.to_thread`, mentre il backend OpenAI-compatibile usa direttamente `httpx.AsyncClient`. La persistenza dei checkpoint LangGraph usa `AsyncSqliteSaver` (basato su `aiosqlite`), requisito necessario per i metodi async del grafo e per i workflow di interrupt/resume.

---

## 2. Stack Tecnologico

| Ambito | Tecnologia |
|---|---|
| Web framework / API | **FastAPI** + Uvicorn (ASGI), con risposte SSE via `StreamingResponse` |
| Orchestrazione agenti | **LangGraph** (`StateGraph`, conditional edges, `interrupt()`, `AsyncSqliteSaver`) |
| Database vettoriale | **ChromaDB** (in modalità Client/Server tramite `chromadb.HttpClient`) |
| Validazione & settings | **Pydantic v2** + `pydantic-settings` |
| Inferenza LLM | **Ollama** (o qualsiasi endpoint OpenAI-compatibile: LM Studio, vLLM); fallback Groq cloud; GGUF locale via `llama-cpp-python` (legacy) |
| HTTP client async | **httpx** |
| Persistenza checkpoint | **SQLite** via `aiosqlite` (`AsyncSqliteSaver`) |
| Testing & qualità | pytest, pytest-asyncio, ruff, mypy |

---

## 3. Architettura del Sistema (I 3 Motori)

Il sistema è composto da tre macro-blocchi indipendenti ma complementari. Ognuno è implementato come un grafo LangGraph (o come livello di astrazione a sé stante) con uno stato condiviso fortemente tipizzato.

### 3.1 Ingestion Engine (`ingestion/`)

Responsabile dell'onboarding del listino servizi di ogni tenant. Trasforma file grezzi ed eterogenei in entry di catalogo normalizzate e validate dentro ChromaDB. È un grafo LangGraph (`ingestion/graph.py`) con cinque nodi e tre router condizionali.

**Topologia:**

```
chunker → normalizer ──(loop sui chunk)──┐
              │                          │
              └─(tutti i chunk fatti)→ validator
                                          │
                            ┌─ clean ─────┴──→ finalizer → END
                            │
                            └─ needs review → approval (interrupt)
                                                  │
                                    ┌─ approved ──┴──→ finalizer → END
                                    │
                                    └─ rejected ──→ END
```

**Chunking.** Il `chunker_node` legge il file sorgente (formato dichiarato esplicitamente, non inferito dall'estensione) e lo suddivide in batch di righe. I lettori supportano CSV, JSON e Excel (`xlsx` via openpyxl) e vengono eseguiti in un thread (`asyncio.to_thread`). La dimensione del batch è fissata da `CHUNK_SIZE = 50`. Il `normalizer_node` processa un chunk per volta; il router `route_after_normalizer` controlla se `current_chunk_index < len(raw_chunks)` per decidere se iterare o passare alla validazione.

**Normalizzazione con Pydantic.** Il cuore della normalizzazione è il modello Pydantic `ServiceItem` (`ingestion/models.py`), unità canonica verso cui ogni formato sorgente deve mappare. L'LLM riceve le righe grezze e restituisce un array JSON che viene istanziato come `ServiceItem`, beneficiando della validazione automatica: prezzi non negativi, currency normalizzata a codice ISO 4217 a 3 lettere, unit in minuscolo, `confidence` nell'intervallo `[0, 1]`. Un `model_validator` auto-flagga gli item con confidenza inferiore a 0.5. In caso di errore di costruzione, invece di scartare la riga viene creato un placeholder flaggato, così nessun dato viene perso silenziosamente. Il `validator_node` riapplica le regole di business (prezzo zero senza descrizione, nome troppo corto, unit sconosciute) e calcola la confidenza media del run.

**Salvataggio isolato in ChromaDB.** Il `finalizer_node` scrive gli item nella collezione tenant-scoped `catalogue_{tenant_id}` tramite `get_or_create_collection` (con spazio metrico coseno). Il documento embeddato è la concatenazione di nome + descrizione + categoria; i campi strutturati (prezzo, currency, unit, categoria, `tenant_id`, timestamp) finiscono nei metadata; l'ID è lo UUID stabile del `ServiceItem`. Prima della scrittura viene applicata una deduplica difensiva per ID, a protezione da eventuali re-run del grafo.

**Pattern Human-in-the-Loop (interrupt).** Quando la confidenza media scende sotto `CONFIDENCE_THRESHOLD = 0.75` oppure almeno un item è flaggato, il router `route_after_validator` instrada verso l'`approval_node`. Questo nodo chiama `interrupt(value=review_payload)`: LangGraph persiste il checkpoint corrente (via `AsyncSqliteSaver`) e solleva una `GraphInterrupt`, **sospendendo** il grafo senza crasharlo. Il payload di revisione (item flaggati, confidenza, conteggi, `raw_data` per la tracciabilità) viene inoltrato all'operatore umano. La ripresa avviene chiamando il grafo con `Command(resume={"approved": ..., "feedback": ...})`: il valore passato a `resume` diventa il valore di ritorno di `interrupt()`. Se approvato si procede al `finalizer`; se rifiutato si va a `END` e il chiamante può ri-triggerare la run iniettando `review_feedback` nel prompt del normalizer.

### 3.2 Qualification Pipeline (`agents/` e `core/graph.py`)

È il grafo principale che qualifica un singolo lead. Lo stato condiviso è `LeadState` (`core/state.py`), un `TypedDict` in cui il campo `sse_logs` usa il reducer `operator.add` per essere append-only e fan-in safe, mentre gli altri campi sono last-write-wins.

**Flusso del lead:**

```
Sanitizer → Extractor ◄─────────────────────────┐
                │                                │ retry (retry_count < max)
            Mapper ── ok ──→ Calculator          │
                │              │                  │
                │          Delivery ◄─────────────┼── retry (delivery_attempts < max)
                │              │                  │
                │         SUCCESS → END           │
                │                                 │
                ├─ fail & retry_count < max ──────┘
                └─ fail & retry_count == max → Human Fallback (interrupt) → END
```

- **Sanitizer** (`agents/sanitizer.py`) — Primo nodo obbligatorio. Maschera i dati personali (PII) nel testo grezzo via regex prima che qualsiasi dato raggiunga l'LLM o servizi esterni: carte di credito, codice fiscale italiano, SSN, email, numeri di telefono, IBAN. Il token di sostituzione è configurabile (`PII_MASK_TOKEN`, default `[REDACTED]`). Tutti i nodi a valle consumano `sanitized_text`, mai `raw_text`.

- **Extractor** (`agents/extractor.py`) — Nodo async LLM-powered. Costruisce un prompt dal testo sanitizzato e lo invia al backend configurato: `openai` (nativo async via `httpx.AsyncClient`), `groq` o `llama` (bloccanti, offloadati con `asyncio.to_thread`). Restituisce un array JSON di nomi di servizio, con parsing robusto (gestisce markdown fences e JSON malformato). In un ciclo di retry, i servizi precedentemente non mappabili vengono reimmessi nel prompt come feedback negativo. Incrementa `retry_count`.

- **Mapper** (`agents/mapper.py`) — Interroga ChromaDB tramite il client SDK ufficiale `chromadb.HttpClient` (modalità Client/Server, mai embedded) per la collezione `catalogue_{tenant_id}`, offloadando la chiamata bloccante con `asyncio.to_thread`. Per ogni servizio estratto seleziona il match più vicino (distanza minima) e costruisce `mapped_services` con `{service, matched_name, price, unit, distance}`. Se la collezione del tenant non esiste, restituisce un errore chiaro che invita a eseguire prima l'ingestion.

- **Calculator** (`agents/calculator.py`) — Puro Python, zero LLM. Somma il campo `price` di tutte le entry mappate e scrive `total_quote`, producendo un breakdown leggibile per lo stream SSE. Deterministico, testabile e auditabile.

**Routing e retry.** Il router `route_after_mapper` (`core/graph.py`) implementa la matrice decisionale: se gli item mappati sono ≥ `mapper_min_results` → Calculator; se vuoti e `retry_count < max_retry_count` → ritorno a Extractor (loop di raffinamento); se vuoti ed esauriti i retry → `human_fallback_node`, che sospende il grafo via `interrupt()` per intervento manuale. Le soglie sono lette da `Settings` e quindi tunabili via variabili d'ambiente.

### 3.3 Delivery Layer (`adapters/`)

Disaccoppia la logica di consegna del preventivo dal grafo tramite il **pattern Adapter**.

**Pattern Adapter.** Tutti gli adapter implementano la classe astratta `BaseDeliveryAdapter` (`adapters/base.py`), il cui contratto è un metodo coroutine `deliver(payload) -> bool` (True su consegna confermata, False su fallimento recuperabile; le eccezioni di rete vengono propagate). Le implementazioni concrete sono:

- **ConsoleAdapter** (`adapters/console.py`) — per sviluppo e test: logga il payload come JSON e ritorna sempre True.
- **WebhookAdapter** (`adapters/webhook.py`) — per produzione: invia il payload in POST JSON all'URL webhook del tenant con `httpx.AsyncClient`, timeout configurabile (`delivery_timeout_seconds`). Risponde False sugli errori HTTP 4xx/5xx (retryabili) e rilancia gli errori di rete. Non logga mai il body del payload, per evitare leak di PII.

**Factory.** La funzione `get_delivery_adapter(tenant_id)` (`adapters/factory.py`) risolve l'adapter corretto per il tenant. Attualmente restituisce un `ConsoleAdapter` per tutti i tenant; il punto di estensione previsto è una lookup contro un config store per-tenant (DB, secrets manager) che ritorni `WebhookAdapter` con l'URL del cliente — senza che il resto del codice cambi.

**Logica di retry nello stato di LangGraph.** Il `delivery_node` (`agents/delivery.py`) incrementa `delivery_attempts` all'inizio di ogni invocazione, costruisce un payload PII-safe (mai `raw_text`) e tenta la consegna, aggiornando `delivery_status` (`PENDING`/`SUCCESS`/`FAILED`) e `delivery_error`. La decisione di ripetere è separata dal nodo: il router `route_after_delivery` (`core/graph.py`) legge stato e tentativi e applica il tetto `delivery_max_attempts` (default 3) — su `SUCCESS` → END, su `FAILED` con tentativi sotto soglia → ritorno a `delivery`, su tentativi esauriti → END con log di abbandono. Lo stato di delivery è quindi gestito interamente nello stato condiviso del grafo, separando responsabilità (nodo = tentativo, router = policy di retry).

---

## 4. Setup e Avvio

### Prerequisiti

- Python 3.11+
- [Ollama](https://ollama.com) (o altro server OpenAI-compatibile) con un modello scaricato
- [ChromaDB](https://www.trychroma.com) installato come server

### 1. Dipendenze

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configurazione

```bash
cp .env.example .env
# Modifica .env con i tuoi valori (URL LLM, porta ChromaDB, ecc.)
```

### 3. Avvio dei servizi

**Ollama** (modello LLM locale):

```bash
ollama serve
ollama pull llama3        # deve combaciare con LLM_MODEL_NAME
```

**ChromaDB** (server vettoriale, default porta 8001 come da `.env.example`):

```bash
chroma run --host localhost --port 8001
```

**FastAPI** (l'applicazione):

```bash
python main.py
# oppure, per produzione:
# uvicorn main:app --host 0.0.0.0 --port 8000
```

L'API sarà disponibile su `http://localhost:8000` con documentazione interattiva su `/docs` (Swagger) e `/redoc`. Health check su `/health`.

> **Nota.** Lo script `seed_db.py` è un helper di sviluppo che popola una collezione `service_catalogue` generica; il flusso di produzione carica i cataloghi tramite l'endpoint `/ingest/stream` nelle collezioni tenant-scoped `catalogue_{tenant_id}`.

---

## 5. API Endpoints Principali

### `POST /ingest/stream`

Avvia una run di ingestion del catalogo e ne trasmette il progresso via SSE. Il `thread_id` generato è esposto nell'header di risposta `X-Thread-Id` (serve per chiamare `/approve`). Se il grafo incontra item a bassa confidenza, si sospende ed emette `event: interrupt`.

Payload:

```json
{
  "tenant_id": "acme",
  "file_path": "/uploads/acme/catalogue.csv",
  "file_format": "csv",
  "review_feedback": null
}
```

Eventi SSE emessi: `log` (un frame per entry di `sse_logs`), `interrupt` (con `thread_id`, `tenant_id`, `review_payload`), `done` (riepilogo conteggi), `error`.

### `POST /ingest/{thread_id}/approve`

Riprende una run di ingestion sospesa all'`ApprovalNode`. Internamente esegue `graph.ainvoke(Command(resume={...}), config)` ricaricando il checkpoint identificato dal `thread_id`.

Payload:

```json
{
  "approved": true,
  "feedback": "Prezzi corretti, procedi pure."
}
```

Comportamento: `approved: true` → il grafo esegue il `FinalizeNode` e scrive su ChromaDB; `approved: false` → routing a `END` (il chiamante può ri-triggerare `/ingest/stream` con `review_feedback`). Restituisce `404` se non esiste un checkpoint per il `thread_id`.

### `POST /qualify/stream`

Qualifica un lead e trasmette gli eventi di elaborazione via SSE in tempo reale, ideale per dashboard operatore.

Payload:

```json
{
  "tenant_id": "acme",
  "raw_text": "Salve, avremmo bisogno di sviluppare un sito web e configurare un server email.",
  "lead_id": null
}
```

Eventi SSE emessi: `log` (progresso per nodo), `done` (JSON finale con `total_quote` e `mapped_services`), `error`.

> Esiste anche `POST /qualify` (sincrono, stessa request) che esegue l'intero grafo con `ainvoke` e restituisce un JSON `QualifyResponse` completo — pensato per webhook CRM che non consumano SSE.

---

## 6. Next Steps futuri

Sezione aperta per i prossimi sviluppi:

- **Frontend UI.** Costruire un'interfaccia operatore che consumi gli stream SSE (`/qualify/stream`, `/ingest/stream`), visualizzi gli item flaggati durante l'HITL e permetta approvazione/rifiuto con un click.
- **Dockerizzazione.** `Dockerfile` + `docker-compose` per orchestrare FastAPI, ChromaDB e Ollama come stack riproducibile e isolato.
- **Persistenza di produzione.** Migrazione da `AsyncSqliteSaver` ad `AsyncPostgresSaver` per i checkpoint (la factory `get_checkpointer` è già progettata per essere sostituita senza toccare altri moduli).
- **Factory di delivery per-tenant.** Implementare la lookup contro un config store reale per assegnare `WebhookAdapter` con URL dedicati per tenant.
- **Hardening sicurezza.** Restringere il CORS via env var, aggiungere autenticazione/autorizzazione per tenant, rate limiting.
- **Osservabilità.** Metriche, tracing e dashboard sullo stato delle pipeline e sui tassi di fallback umano.
```

