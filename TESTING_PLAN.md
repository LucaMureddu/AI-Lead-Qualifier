# Piano di Test — AI Lead Qualifier

> Strategia completa e attuabile per testare il backend (FastAPI + LangGraph), gli agenti AI e il frontend (Vite/Alpine), con automazione su GitHub Actions. Documento di pianificazione: **non modifica codice**, descrive cosa costruire e in che ordine.

---

## 0. Sintesi (TL;DR)

L'obiettivo è una **piramide di test** a quattro livelli, dal più veloce/economico al più lento/realistico:

```
        ╱ Livello 4 ╲          CI/CD — GitHub Actions (orchestrazione)
       ╱─────────────╲
      ╱  Livello 3 E2E ╲       Playwright (pochi, lenti, realistici)
     ╱──────────────────╲
    ╱   Livello 2 Evals   ╲     qualità AI (golden dataset + giudice)
   ╱────────────────────────╲
  ╱  Livello 1 Unit/Integr.  ╲   pytest + mock (tanti, veloci, deterministici)
 ╱────────────────────────────╲
```

**Principio guida:** tanti test veloci e deterministici alla base (mock di LLM e ChromaDB), pochi test realistici in cima. Gli unit/integration test verificano che i "tubi" funzionino; gli evals verificano che l'"intelligenza" sia accurata; gli E2E verificano che l'utente possa davvero cliccare e ottenere il risultato.

**Decisioni prese in fase di discussione:**

| Tema | Scelta |
|---|---|
| Output di questa fase | Solo il piano dettagliato (questo documento) |
| Giudice degli evals | Gratuito e locale di default (asserzioni deterministiche + semantiche + giudice LLM locale via Ollama). Giudice cloud GPT‑4 documentato come opt‑in a pagamento, spento di default. |
| Piattaforma CI | GitHub Actions (richiede `git init` + repo remoto, oggi assenti) |
| LLM/ChromaDB nei test | **Due binari** (rivisto): in CI mock per unit/integration + evals **deterministici e semantici su generazioni registrate**, senza LLM generativo nei runner; il **giudice LLM** gira **solo in locale** sul Mac con un modello capace (Llama 3 8B/70B), mai un 1B. |

**Nota sull'air‑gapped.** Il vincolo "100% locale" riguarda il **prodotto in produzione**, non l'ambiente di sviluppo/test. La CI nel cloud che testa il codice è quindi legittima; il deployment resta isolato. Per gli evals manteniamo comunque il giudice **locale e gratuito** di default, così non si spende nulla.

---

## 1. Stato attuale e correzioni preliminari

Prima di costruire i quattro livelli, vanno sanate alcune cose che ho rilevato esaminando il progetto. Sono il prerequisito: senza, il "Livello 1 con mocking" sarebbe solo apparente.

### 1.1 I test esistenti sono rotti

`tests/test_graph.py` crea lo stato così:

```python
"lead_info": LeadInfo(id="test-lead-001", raw_text=raw_text),
```

ma in `core/state.py` il modello richiede ora **tre** campi obbligatori:

```python
class LeadInfo(BaseModel):
    id: str = Field(...)
    raw_text: str = Field(...)
    tenant_id: str = Field(...)   # ← obbligatorio, mancante nei test
```

Risultato: ogni test che passa per `_make_state` o per le integration (`TestFullGraphIntegration`) fallisce in setup con `ValidationError`. **Va corretto per primo**, aggiungendo `tenant_id="test-tenant"` ovunque si costruisca un `LeadInfo`.

### 1.2 Due bug di mocking nei test di integrazione

Anche dopo aver corretto il `tenant_id`, i due test integration non testano ciò che credono di testare:

1. **Provider sbagliato.** Patchano `agents.extractor._invoke_llm_blocking`, ma con `llm_provider="openai"` (il default in `core/config.py`) l'extractor instrada su `_call_openai_compatible` (httpx), **non** su `_invoke_llm_blocking`. Il mock viene aggirato e il test chiamerebbe il vero endpoint Ollama. → Vanno mockate entrambe le strade, oppure forzato `llm_provider` nel test, oppure (meglio) mockato direttamente `agents.extractor._call_openai_compatible`.

2. **Target di patch errato per il mapper.** Patchano `agents.mapper.mapper_node`, ma `core/graph.py` esegue `from agents.mapper import mapper_node` **al momento dell'import**: il nome è già legato nel namespace di `core.graph`. Sostituire l'attributo su `agents.mapper` non rieffettua il binding. → Il target corretto è **`core.graph.mapper_node`** (si patcha dove il nome è *usato*, non dove è *definito*). È la classica trappola del mocking in Python ed è centrale per tutto il Livello 1.

### 1.3 Infrastruttura mancante

- Nessun `conftest.py`, nessun `pytest.ini`/`pyproject.toml`/`setup.cfg`: niente configurazione `asyncio_mode`, niente marker, niente isolamento di env/DB tra i test.
- **Nessun repository git inizializzato** (`git status` → nessun repo). Il flusso "git push → CI" del Livello 4 non è quindi ancora possibile: serve `git init`, un `.gitignore` (già presente e ben fatto) e un remote su GitHub.
- Nessun test frontend né Playwright.

### 1.4 Quello che invece è già pronto (e ci aiuta)

- **Dataset per gli evals già presenti:** `dirty_catalog.csv` (prezzi "sporchi": `trecento euro`, `1.200,50 €`, separatore `;`) e `catalogo_problematico.csv` (prezzi `0`, negativi, `gratis`, nomi di 1 carattere). Quest'ultimo è il caso perfetto per verificare che l'**HITL** scatti (item flaggati / confidenza < `0.75`).
- `llm_temperature = 0.0` di default: l'estrazione è quasi deterministica, quindi gli evals saranno molto più riproducibili del solito.
- Le dipendenze di test di base ci sono già in `requirements.txt`: `pytest`, `pytest-asyncio`, `pytest-mock`, più `ruff` e `mypy`.
- Il codice è già scritto per essere testabile: nodi che ritornano dict di patch, checkpointer iniettabile (`build_graph(checkpointer=None)`), adapter dietro interfaccia astratta, helper puri (`_sum_prices`, `_mask_pii`).

---

## 2. Fondamenta condivise (setup di base)

Questi artefatti servono a tutti i livelli e vanno creati una sola volta.

### 2.1 Dipendenze di sviluppo

Creare `requirements-dev.txt` (separa le dipendenze di test da quelle di runtime):

```text
-r requirements.txt
pytest>=8.2.0
pytest-asyncio>=0.23.0
pytest-mock>=3.14.0
pytest-cov>=5.0.0          # coverage
respx>=0.21.0             # mock di httpx (extractor / webhook / OpenAI-compat)
anyio>=4.0.0              # utility async per i test
# Evals (Livello 2) — tutti girabili in locale, zero costi:
promptfoo                 # via npx (Node) — vedi Livello 2; in alternativa pytest puro
```

> `httpx` è già una dipendenza di runtime, quindi `httpx.ASGITransport`/`AsyncClient` per i test API sono disponibili senza aggiungere nulla. `respx` rende i mock di rete molto più leggibili dei `monkeypatch` manuali usati oggi in `test_webhook_adapter.py`.

### 2.2 Configurazione pytest (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"           # niente @pytest.mark.asyncio ripetuti
testpaths = ["tests"]
addopts = "-ra -q --strict-markers -m 'not eval'"   # gli eval LIVE (modello reale) sono esclusi di default
markers = [
    "unit: test puri e veloci, nessuna I/O",
    "integration: grafo/API con LLM e ChromaDB mockati",
    "eval_ci: evals model-free (deterministico + coseno) su generazioni registrate — girano in CI",
    "eval: evals LIVE col modello reale + giudice LLM — solo in locale, esclusi di default",
]

[tool.coverage.run]
omit = ["tests/*", "frontend/*", ".venv/*"]
```

Con `asyncio_mode = "auto"` i test async non hanno più bisogno del decoratore. Gli `eval` si escludono dai run normali con `-m "not eval"`.

### 2.3 `conftest.py` — le fixture condivise

Cuore della strategia: isolare ogni test da servizi esterni e dallo stato globale. Punti chiave:

- **Override delle Settings.** `get_settings()` è cachata con `@lru_cache`; nei test va svuotata la cache e impostato l'ambiente (es. `llm_provider`, path SQLite su `:memory:`/tmp) prima di costruire l'app.
- **Checkpointer in‑memory.** `AsyncSqliteSaver` su `aiosqlite.connect(":memory:")`, come già fa correttamente `test_graph.py`. Nessun file su disco, test paralleli sicuri.
- **Fake LLM e fake ChromaDB** riusabili da tutti i test del grafo.
- **Client API asincrono** su `httpx.ASGITransport` (no rete, no porta).

```python
# tests/conftest.py
import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from core.config import get_settings
from core.state import LeadInfo, LeadState


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch, tmp_path):
    """Isola le Settings: cache pulita + DB su file temporaneo per test."""
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def checkpointer():
    conn = await aiosqlite.connect(":memory:")
    try:
        yield AsyncSqliteSaver(conn)
    finally:
        await conn.close()


@pytest.fixture
def make_lead_state():
    """Factory di LeadState valido (con tenant_id!)."""
    def _make(raw_text="Serve un sito web e un server email.", **ovr) -> LeadState:
        base: LeadState = {
            "lead_info": LeadInfo(id="lead-001", raw_text=raw_text, tenant_id="acme"),
            "sanitized_text": "", "extracted_services": [], "mapped_services": [],
            "total_quote": 0.0, "retry_count": 0, "sse_logs": [], "error": None,
            "delivery_status": "PENDING", "delivery_attempts": 0, "delivery_error": None,
        }
        base.update(ovr)
        return base
    return _make


@pytest.fixture
async def api_client():
    from main import create_app
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

### 2.4 Struttura delle cartelle di test

```
tests/
├── conftest.py                 # fixture condivise
├── unit/                       # Livello 1 — funzioni pure (no I/O)
│   ├── test_sanitizer.py
│   ├── test_calculator.py
│   ├── test_models.py          # validator di ServiceItem
│   └── test_routers.py         # route_after_* (decisioni di routing)
├── integration/                # Livello 1 — grafo + API con mock
│   ├── test_graph_qualify.py
│   ├── test_graph_ingestion.py
│   ├── test_api_qualify.py
│   ├── test_api_ingest.py
│   ├── test_api_upload.py
│   └── test_api_profile.py
└── evals/                      # Livello 2
    ├── datasets/
    │   ├── leads_golden.jsonl  # lead → servizi/pacchetti attesi
    │   └── catalogs/           # riusa dirty_catalog.csv, catalogo_problematico.csv
    ├── snapshots/              # generazioni registrate (per gli eval model-free in CI)
    ├── test_extraction_eval.py    # Binario B (live, -m eval) — locale
    ├── test_extraction_ci.py      # Binario A (model-free, -m eval_ci) — CI
    ├── test_mapping_eval.py
    └── promptfoo.yaml          # (se si sceglie promptfoo)
```

---

## 3. Livello 1 — Test di integrazione e backend

**Obiettivo:** verificare che i "tubi" funzionino. Server che risponde `200`, grafo che instrada correttamente, stato che si propaga, errori gestiti senza crash. In questa fase **LLM e ChromaDB sono sempre mockati**: i test girano in millisecondi, senza modello, senza rete, senza token.

### 3.1 Unit test puri (nessun mock, nessuna I/O)

Le parti deterministiche del sistema. Sono i test più preziosi: velocissimi e a prova di regressione.

| File | Cosa testa | Esempi di caso |
|---|---|---|
| `test_sanitizer.py` | `_mask_pii` | email, codice fiscale, IBAN, carta, telefono mascherati; testo senza PII invariato; conteggio redazioni |
| `test_calculator.py` | `_sum_prices`, `calculator_node` | somma corretta, lista vuota → 0, arrotondamento (0.1+0.2), `KeyError` su `price` mancante → `error` impostato |
| `test_models.py` | validator di `ServiceItem` | prezzo negativo → `ValueError`; currency `eur`→`EUR`; currency non 3 lettere → errore; `confidence < 0.5` → auto‑`flagged` |
| `test_routers.py` | `route_after_mapper`, `route_after_delivery`, `route_after_validator`, `route_after_normalizer` | matrice decisionale completa (vedi sotto) |

I router sono funzioni pure `state → stringa`: testarli copre tutta la logica di retry/fallback senza eseguire il grafo. Esempio:

```python
# tests/unit/test_routers.py
from core.graph import route_after_mapper, route_after_delivery

def test_mapper_ok_va_al_calculator(make_lead_state):
    state = make_lead_state(mapped_services=[{"service": "X", "price": 100.0}])
    assert route_after_mapper(state) == "calculator"

def test_mapper_vuoto_con_retry_disponibili_torna_a_extractor(make_lead_state):
    state = make_lead_state(mapped_services=[], retry_count=0)
    assert route_after_mapper(state) == "extractor"

def test_mapper_vuoto_retry_esauriti_va_a_human_fallback(make_lead_state):
    state = make_lead_state(mapped_services=[], retry_count=2)  # = max_retry_count
    assert route_after_mapper(state) == "human_fallback"

def test_delivery_success_termina(make_lead_state):
    state = make_lead_state(delivery_status="SUCCESS", delivery_attempts=1)
    assert route_after_delivery(state) == "__end__"
```

### 3.2 Test dei singoli nodi (con mock mirati)

Ogni nodo testato in isolamento, mockando solo la sua dipendenza esterna.

- **Extractor** — mock di `_call_openai_compatible` (con `respx`, intercettando la POST a `/chat/completions`). Verifica: parsing JSON pulito, JSON dentro fence markdown, JSON malformato → `[]`, incremento `retry_count`, `error` impostato su 4xx/5xx, feedback dei servizi precedenti nel prompt in retry.
- **Mapper** — mock di `_query_chroma_sync` (così non serve un server Chroma). Verifica: selezione del match a distanza minima, collezione assente → `ValueError` gestita con messaggio "Run /ingest/stream first", `extracted_services` vuoto → ritorno pulito.
- **Normalizer** (ingestion) — mock di `_call_openai_compatible`. Verifica: riga malformata → placeholder `flagged` (nessun dato perso), `current_chunk_index` che avanza, sanitizzazione delle chiavi NaN.
- **Finalizer** (ingestion) — mock di `_write_to_chroma_sync`. Verifica: deduplica per ID, scrittura della collezione `catalogue_{tenant_id}`.
- **Delivery** — mock dell'adapter (`get_delivery_adapter`). Verifica: `SUCCESS`/`FAILED`, incremento `delivery_attempts`, `httpx.RequestError` → `FAILED` senza propagare.

Esempio con `respx` (sostituisce il `monkeypatch` manuale, molto più chiaro):

```python
# tests/integration/test_graph_qualify.py
import respx, httpx, pytest
from core.config import get_settings

@pytest.mark.integration
async def test_extractor_parsa_servizi(make_lead_state, respx_mock):
    url = f"{get_settings().llm_base_url.rstrip('/')}/chat/completions"
    respx_mock.post(url).mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": '["Web Development", "SEO Audit"]'}}]}
    ))
    from agents.extractor import extractor_node
    state = make_lead_state(sanitized_text="Vorrei un sito e un audit SEO")
    out = await extractor_node(state)
    assert out["extracted_services"] == ["Web Development", "SEO Audit"]
    assert out["retry_count"] == 1
```

### 3.3 Test del grafo completo (LLM + ChromaDB mockati)

Qui si esegue **tutto** il grafo con `ainvoke`, ma con i confini esterni finti. È la versione *corretta* dei test che oggi sono rotti.

```python
# tests/integration/test_graph_qualify.py
from unittest.mock import AsyncMock, patch
import respx, httpx, pytest
from core.config import get_settings

@pytest.mark.integration
async def test_happy_path(make_lead_state, checkpointer, respx_mock):
    # LLM: l'extractor estrae due servizi
    url = f"{get_settings().llm_base_url.rstrip('/')}/chat/completions"
    respx_mock.post(url).mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": '["Cloud Migration", "SEO Audit"]'}}]}
    ))
    # ChromaDB: si patcha mapper_node DOVE È USATO → core.graph (non agents.mapper!)
    with patch("core.graph.mapper_node", new_callable=AsyncMock) as m:
        m.return_value = {"mapped_services": [
            {"matched_name": "Cloud Migration", "price": 3000.0, "unit": "€"},
            {"matched_name": "SEO Audit", "price": 500.0, "unit": "€"},
        ], "sse_logs": ["[MAPPER] mapped=2"], "error": None}

        from core.graph import build_graph
        graph = build_graph(checkpointer=checkpointer)
        state = make_lead_state()
        final = await graph.ainvoke(state, config={"configurable": {"thread_id": "t1"}})

    assert final["total_quote"] == 3500.0
    assert final["error"] is None
```

Scenari da coprire: **happy path** (sopra), **retry → human fallback** (mapper sempre vuoto → `GraphInterrupt`), **delivery retry** (adapter che fallisce N volte poi riesce). Stesso schema per il grafo di ingestion: chunk multipli, routing a `approval` quando `catalogo_problematico.csv` produce flag, resume con `Command(resume=...)`.

### 3.4 Test delle API (httpx ASGITransport)

Si testano gli endpoint reali senza avviare un server, con `httpx.ASGITransport`. La dipendenza LLM/Chroma resta mockata.

| Endpoint | Casi |
|---|---|
| `GET /health` | `200`, `{"status": "ok"}` |
| `POST /qualify` (sync) | `200` con `total_quote`/`mapped_services`; `422` se `raw_text` < 10 char; `500` gestito |
| `POST /qualify/stream` | content‑type `text/event-stream`; frame `event: log` poi `event: done`; `event: error` su eccezione |
| `POST /ingest/stream` | header `X-Thread-Id` presente; `event: interrupt` con `review_payload` su catalogo problematico |
| `POST /ingest/{id}/approve` | `404` se thread_id inesistente; `200` con `status: completed/rejected` |
| `POST /upload` | estensione non ammessa → `400`; file vuoto → `400`; troppo grande → `413`; tenant_id sanificato (niente path traversal) |
| `PUT /tenants/{id}/profile` | upsert e rilettura; `logo_data_url` non‑immagine → `400`; oltre `profile_max_bytes` → `413` |

Esempio (validazione + dependency mock):

```python
# tests/integration/test_api_qualify.py
import pytest

@pytest.mark.integration
async def test_qualify_raw_text_troppo_corto(api_client):
    r = await api_client.post("/qualify", json={"tenant_id": "acme", "raw_text": "ciao"})
    assert r.status_code == 422   # min_length=10 sul campo raw_text

@pytest.mark.integration
async def test_health(api_client):
    r = await api_client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
```

Per testare lo **streaming SSE** si legge la risposta e si verifica la sequenza dei frame (`event: log` … `event: done`), confermando che il diff di `sse_logs` tra snapshot produce un frame per riga.

### 3.5 Strategia di mocking — regola d'oro

> **Patcha il nome dove viene *usato*, non dove è *definito*.** `core/graph.py` importa `mapper_node` con `from agents.mapper import ...`: quindi si patcha `core.graph.mapper_node`. Questo è il singolo errore più importante da non ripetere, perché altrimenti i test "verdi" in realtà chiamano i servizi reali.

In alternativa al patching, FastAPI offre `app.dependency_overrides` — utile se in futuro si introducono `Depends()` per LLM/Chroma. Oggi i nodi non usano `Depends`, quindi il patching mirato è la via più diretta.

### 3.6 Obiettivo di copertura

Target ragionevole: **≥ 85%** sui moduli di logica (`agents/`, `core/`, `ingestion/`, `adapters/`, `api/`). La copertura si misura con `pytest --cov` e si pubblica come gate soft in CI (avviso, non blocco, all'inizio).

---

## 4. Livello 2 — AI Evals (valutazione dell'intelligenza)

**Obiettivo:** misurare se l'AI fa la cosa *giusta*, non solo se il codice non crasha. Gli LLM non rispondono mai con le stesse identiche parole, quindi servono asserzioni **semantiche/logiche**, non `==` sulle stringhe.

### 4.1 Cosa valutare in questo progetto

Quattro capacità AI distinte, ognuna con il suo dataset:

1. **Extractor** — dal testo del lead estrae i nomi di servizio giusti. *Es.:* "Voglio vendere online" → deve contenere qualcosa di mappabile a "e‑commerce".
2. **Mapper** — i servizi estratti vengono agganciati alla voce di listino corretta (è un problema RAG: retrieval su ChromaDB). *Es.:* dato il lead "e‑commerce", il match deve essere l'ID del pacchetto e‑commerce, con `distance` sotto una soglia.
3. **Normalizer (ingestion)** — trasforma righe sporche nello schema canonico. *Es.:* `dirty_catalog.csv` con `1.200,50 €` deve dare `price≈1200.5`, `currency="EUR"`.
4. **Routing HITL** — il sistema sa *dubitare*. *Es.:* `catalogo_problematico.csv` (prezzi 0/negativi/"gratis") deve produrre item `flagged` e instradare ad `approval`. Questo è un eval **deterministico e gratuito** particolarmente importante.

### 4.2 Il golden dataset

Un file versionato (in `tests/evals/datasets/`) con input → output atteso. Per i lead, formato `jsonl`:

```jsonl
{"id": "L01", "raw_text": "Vorremmo aprire un negozio online per vendere scarpe", "expect_contains_category": "e-commerce", "min_services": 1}
{"id": "L02", "raw_text": "Ci serve solo posizionarci su Google", "expect_contains_category": "seo", "min_services": 1}
{"id": "L03", "raw_text": "asdkjh qwe lorem ipsum", "expect_services": 0, "expect_human_fallback": true}
```

Per i cataloghi si **riusano i CSV esistenti** come fixture: `dirty_catalog.csv` (deve normalizzare con qualche flag) e `catalogo_problematico.csv` (deve scatenare l'HITL). Il dataset va ampliato nel tempo: ogni bug trovato in produzione diventa una nuova riga ("regression‑driven evals").

### 4.3 Asserzioni a tre livelli (dal gratis al sofisticato)

```
1. Deterministiche  →  gratis, istantanee, riproducibili      ← inizia da qui
2. Semantiche       →  gratis (embedding locale già in Chroma)
3. Giudice LLM      →  locale = gratis  |  cloud GPT-4 = a pagamento (opt-in)
```

**(1) Deterministiche** — coprono la maggior parte dei casi e non costano nulla:

- *subset/contains*: la lista dei servizi estratti contiene la categoria attesa.
- *match su ID*: `mapped_services` include l'ID del pacchetto giusto.
- *soglia su distanza*: il miglior match ha `distance < 0.4`.
- *invarianti*: `currency=="EUR"`, `price>=0`, `total_quote == sum(prezzi)`.
- *deve flaggare*: il catalogo problematico produce `flagged_count > 0`.

**(2) Semantiche senza costo** — quando le parole variano ma il senso no. Si riusa l'**embedder** che ChromaDB già impiega (`all-MiniLM-L6-v2` via onnxruntime): si confronta l'output con l'atteso via similarità del coseno e si richiede `≥ 0.8`. Precisazione importante (recependo la tua osservazione): è un modello di **embedding** piccolo (~80 MB), deterministico e CPU‑only — **non** un LLM generativo. Per questo *può* girare anche in CI (basta cachare i pesi una volta). Nessun servizio esterno, nessun token a pagamento.

**(3) Giudice LLM** — per giudizi sfumati ("questa estrazione è ragionevole?"). Un giudice affidabile **richiede un modello capace**: un 1B (es. `llama3.2:1b`) **non** è adatto — ha bassa concordanza col giudizio umano e produce falsi positivi/negativi che rendono l'eval rumoroso e la CI inaffidabile. Quindi il giudice gira **solo in locale**, sul tuo **Mac M4 Pro**, con **Llama 3 8B** (baseline solida) o un **32B/70B quantizzato** (più affidabile, più lento — accettabile perché l'eval non è interattivo). **Mai sui runner di CI.** Il giudice **cloud GPT‑4** resta solo come interruttore opt‑in a pagamento (`EVAL_JUDGE=openai` + API key), spento di default.

### 4.4 Strumento consigliato

Due opzioni valide; **consiglio di iniziare con pytest puro** e adottare promptfoo solo se l'eval cresce.

| Opzione | Pro | Contro | Quando |
|---|---|---|---|
| **pytest parametrizzato** (consigliato per partire) | zero nuove dipendenze, gira accanto agli altri test, `@pytest.mark.eval` | report meno "ricco" | subito, per i livelli (1) e (2) |
| **promptfoo** (Node, via `npx`) | YAML dichiarativo, giudice integrato, report HTML, matrici di prompt | richiede Node, è un secondo ecosistema | quando vuoi confrontare prompt/modelli sistematicamente |
| ~~LangSmith~~ | — | servizio **cloud** a pagamento + telemetria, in conflitto con l'ethos air‑gapped | escluso (lo vedo già installato nel `.venv`: probabilmente dipendenza transitiva di langchain, non usato) |

Esempio di eval in pytest **del Binario B** (locale, col modello reale; vedi §4.6), escluso dai run normali e lanciato con `-m eval`:

```python
# tests/evals/test_extraction_eval.py
import json, pathlib, pytest
from agents.extractor import extractor_node

DATA = [json.loads(l) for l in
        (pathlib.Path(__file__).parent / "datasets/leads_golden.jsonl").read_text().splitlines()]

@pytest.mark.eval
@pytest.mark.parametrize("row", DATA, ids=[r["id"] for r in DATA])
async def test_estrazione_golden(row, make_lead_state):
    state = make_lead_state(sanitized_text=row["raw_text"])
    out = await extractor_node(state)              # chiama il VERO Ollama
    services = [s.lower() for s in out["extracted_services"]]
    if row.get("expect_services") == 0:
        assert services == []                      # gibberish → niente
    else:
        assert len(services) >= row["min_services"]
        # match semantico/contains sulla categoria attesa
        assert any(row["expect_contains_category"] in s for s in services)
```

Esempio promptfoo equivalente (se si sceglie quella strada), con **provider e giudice locali**:

```yaml
# tests/evals/promptfoo.yaml
providers:
  - id: openai:chat:llama3            # endpoint OpenAI-compatibile = Ollama locale
    config: { apiBaseUrl: http://localhost:11434/v1 }
prompts:
  - "Estrai i servizi da: {{raw_text}}"
defaultTest:
  options:
    provider: openai:chat:llama3      # giudice = stesso modello locale (gratis)
tests:
  - vars: { raw_text: "Vorrei aprire un negozio online" }
    assert:
      - { type: contains, value: "commerce" }
      - { type: llm-rubric, value: "elenca servizi pertinenti all'e-commerce" }
```

### 4.5 Soglia di passaggio

Gli evals non devono essere "tutto o niente" (un LLM sbaglia ogni tanto). Si fissa un **pass‑rate minimo**, es. **≥ 90%** delle righe del golden set; sotto soglia il job fallisce. La soglia si alza man mano che il dataset matura.

### 4.6 Dove girano gli evals — i due binari

Qui recepiamo la critica architetturale: **un modello generativo non gira in CI.** Motivi: un modello piccolo (1B) è inaffidabile sia come giudice sia come generatore — introduce rumore e falsi esiti — mentre un modello capace è troppo lento/pesante per i runner cloud e contro l'ethos del progetto. La soluzione professionale è separare in due binari.

**Binario A — in CI, a ogni PR (veloce, niente LLM generativo).** Asserzioni deterministiche (§4.3.1) + semantiche via coseno con il piccolo embedder (§4.3.2). Girano contro **generazioni registrate** (*snapshot*): gli output del modello catturati **una volta in locale** e versionati in `tests/evals/snapshots/`. Così la CI verifica la logica di pipeline/parsing/routing e i criteri di accettazione **senza** invocare un LLM. Marcati `@pytest.mark.eval_ci`, quindi rientrano nel normale `backend-tests`.

```python
# tests/evals/test_extraction_ci.py — Binario A: nessun LLM, usa snapshot
import json, pathlib, pytest
SNAP = json.loads((pathlib.Path(__file__).parent / "snapshots/extractions.json").read_text())

@pytest.mark.eval_ci
@pytest.mark.parametrize("row", SNAP, ids=lambda r: r["id"])
def test_estrazione_su_snapshot(row):
    services = [s.lower() for s in row["output"]]   # output catturato in locale, non rigenerato
    assert any(row["expect_contains_category"] in s for s in services)
```

**Binario B — in locale sul Mac M4 Pro (completo).** Esegue la pipeline col **modello vero** (generazione fresca) e applica deterministico + semantico + **giudice LLM** (Llama 3 8B, o 32B/70B quantizzato). È qui che si intercettano le regressioni di **modello/prompt**. Quando cambi prompt o modello, **rigeneri gli snapshot** e li committi, così il Binario A resta allineato:

```bash
make eval-snapshot   # rigenera tests/evals/snapshots/ col modello reale (dopo modifiche a prompt/modello)
make eval-local      # pipeline reale + giudice LLM  →  pytest -m eval
```

**Il compromesso, in chiaro.** Poiché in CI non gira un LLM, la CI **non** intercetta da sola la deriva di modello/prompt: quella la cattura il Binario B. La disciplina minima è lanciare `make eval-local` (e rigenerare gli snapshot) **prima di mergiare** modifiche a prompt, modello o catalogo. In opzione, un git pre‑push hook che lo ricordi.

---

## 5. Livello 3 — E2E UI (Playwright)

**Obiettivo:** simulare le dita e gli occhi dell'utente. Playwright apre un browser reale, incolla il testo nella textarea, clicca "Genera Preventivo AI", aspetta che compaia la Card del preventivo e verifica il contenuto. Lo stesso per il flusso di onboarding con revisione umana.

### 5.1 Decisione chiave: backend mockato, non reale

Per gli E2E ci sono due approcci. **Consiglio di mockare il backend a livello di rete** (Playwright intercetta le chiamate `fetch`) per la stragrande maggioranza dei test:

| Approccio | Pro | Contro |
|---|---|---|
| **Mock di rete** (consigliato) | deterministico, veloce, niente Ollama/Chroma, testa *davvero* la UI in isolamento | non verifica l'integrazione reale end‑to‑end |
| Backend reale (smoke) | massima fedeltà | lento, richiede Ollama + Chroma + dati seed, fragile |

Strategia: **tutti i flussi UI con mock di rete**; **1–2 smoke test** con backend reale da lanciare a mano in locale (mai in CI a ogni push).

### 5.2 Dettaglio tecnico: mockare lo stream SSE‑via‑POST

Attenzione: il frontend (`src/api.js`) **non** usa `EventSource`, ma `fetch` + `ReadableStream`, perché gli endpoint stream sono POST. Quindi in Playwright bisogna restituire un body che simula i frame SSE (linee `event:`/`data:` separate da doppio `\n\n`). Esempio per il flusso di qualifica:

```js
// frontend/e2e/qualify.spec.js
import { test, expect } from "@playwright/test";

test("flusso qualifica: testo → preventivo", async ({ page }) => {
  // /health → online (per il badge)
  await page.route("**/health", r => r.fulfill({ json: { status: "ok" } }));

  // /qualify/stream → finto SSE: due log + done
  await page.route("**/qualify/stream", r => r.fulfill({
    status: 200,
    headers: { "content-type": "text/event-stream" },
    body:
      "event: log\ndata: [SANITIZER] ok\n\n" +
      "event: log\ndata: [MAPPER] mapped=1\n\n" +
      'event: done\ndata: {"lead_id":"x","total_quote":2500,' +
      '"mapped_services":[{"matched_name":"Sviluppo Sito","price":2500,"unit":"€","distance":0.12}]}\n\n',
  }));

  await page.goto("/");
  await page.getByPlaceholder(/Incolla qui/).fill("Ci serve un sito web aziendale completo");
  await page.getByRole("button", { name: /Genera Preventivo AI/ }).click();

  await expect(page.getByText("2500.00 €")).toBeVisible();   // ui.formatMoney usa toFixed(2), niente separatore migliaia
  await expect(page.getByText("Sviluppo Sito")).toBeVisible();
});
```

### 5.3 Flussi da coprire

- **Qualifica lead:** gating del bottone sotto i 10 caratteri; invio; comparsa dei log in console; Card preventivo con `total_quote` e lista `mapped_services`; blocco "Invia al cliente" (anteprima email, copia testo, PDF, IVA/validità da Impostazioni).
- **Onboarding HITL (il caso clou):** drop del file → fase `uploading`/`processing` → `event: interrupt` → fase `review` con item flaggati e dati grezzi; click **"Approva e scrivi"** → `done`; percorso **"Rifiuta"**; percorso **"Correggi e riprocessa"** (due passi: `approve(false)` poi nuova `ingest/stream` con `review_feedback`).
- **Impostazioni:** cambio tenant; salvataggio profilo (`PUT` mockato), validazioni; badge API online/offline (mock `/health` che fallisce).

### 5.4 Aggiungere `data-testid` (consigliato)

Oggi i bottoni si selezionano per testo (es. "Approva e scrivi"), il che funziona ma è fragile rispetto a modifiche di copy. Suggerisco di aggiungere pochi `data-testid` agli snodi critici di `frontend/index.html`, ad esempio: `qualify-input`, `qualify-submit`, `quote-card`, `quote-total`, `ingest-dropzone`, `review-panel`, `approve-btn`, `reject-btn`, `retry-btn`, `tenant-select`. Rende gli E2E stabili e leggibili. È l'unica piccola modifica al codice di produzione che il piano consiglia (a parte i fix del §1).

### 5.5 Setup

`npm i -D @playwright/test` in `frontend/`, poi `npx playwright install --with-deps chromium`. Config con `webServer` che lancia `vite preview` così Playwright avvia da sé il frontend:

```js
// frontend/playwright.config.js
export default {
  testDir: "./e2e",
  use: { baseURL: "http://localhost:4173" },
  webServer: { command: "npm run preview", port: 4173, reuseExistingServer: !process.env.CI },
};
```

---

## 6. Livello 4 — CI/CD (GitHub Actions)

**Obiettivo:** automatizzare tutto. A ogni `git push`/PR, GitHub esegue lint, type‑check, test backend e E2E; se anche un solo test fallisce, compare la X rossa e il merge è bloccato.

### 6.1 Prerequisiti (oggi mancanti)

1. `git init` nella cartella del progetto (non c'è ancora un repo).
2. Verificare `.gitignore` (già buono): ignora `.venv/`, `uploads/`, `chroma_data/`, `data/checkpoints.db*`, `.env`. **Attenzione:** i dataset degli evals in `tests/evals/datasets/` **non** devono finire negli ignore. `dirty_catalog.csv` e `catalogo_problematico.csv` sono in root e vanno committati (o spostati sotto `tests/evals/datasets/`).
3. Creare un repository su GitHub e fare il primo push.
4. Non committare `.env` (già ignorato): le eventuali chiavi vivono nei **GitHub Secrets**.

### 6.2 Strategia ibrida (la scelta professionale)

Questo risponde direttamente alla domanda "qual è la scelta più professionale": **due binari separati**.

```
Ad ogni push / PR  (veloce, gratis, niente LLM generativo)
 ├─ job: lint-and-type     → ruff + mypy
 ├─ job: backend-tests     → pytest -m "not eval"   (mock + evals su generazioni registrate)
 └─ job: frontend-e2e      → Playwright (backend mockato di rete)

In locale, sul Mac (completo, col modello vero)
 └─ make eval-local        → pipeline reale + giudice Llama 3 8B/70B  →  pytest -m eval
```

I tre job di CI fanno da **gate al merge** (branch protection) e **non avviano alcun LLM**. Il **Binario B** (evals live col giudice LLM) gira **solo in locale** e non è un gate: serve a intercettare la deriva di modello/prompt prima di mergiare modifiche sensibili (vedi §4.6). Nessun job *nightly* con Ollama: come hai notato, un giudice da 1B sui runner sarebbe inaffidabile.

### 6.3 Workflow principale — `.github/workflows/ci.yml`

```yaml
name: CI
on:
  push: { branches: [main] }
  pull_request: { branches: [main] }

jobs:
  lint-and-type:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements-dev.txt
      - run: ruff check .
      - run: mypy core agents ingestion adapters api

  backend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements-dev.txt
      - run: pytest -m "not eval" --cov --cov-report=term-missing

  frontend-e2e:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: frontend } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "20", cache: npm, cache-dependency-path: frontend/package-lock.json }
      - run: npm ci
      - run: npx playwright install --with-deps chromium
      - run: npx playwright test
```

### 6.4 Evals in CI: nessun LLM nei runner

Recependo la tua critica, **non installiamo Ollama nei runner** e non c'è un workflow `evals-nightly.yml`. Un modello da 1B è un giudice inaffidabile (falsi positivi/negativi → CI rumorosa), e un modello capace è troppo lento/pesante per il cloud. In CI gira quindi solo il **Binario A** (§4.6): deterministico + semantico su **generazioni registrate**, senza LLM generativo (serve solo il piccolo embedder, lo stesso di ChromaDB).

Dato che `eval_ci` **non** è escluso da `-m "not eval"`, il Binario A rientra già nel job `backend-tests`. Se vuoi un report separato e pulito, puoi aggiungere a `ci.yml` un job dedicato (opzionale):

```yaml
  evals-ci:                     # opzionale — model-free, veloce; il piccolo embedder gira su CPU
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11", cache: pip }
      - run: pip install -r requirements-dev.txt
      - run: pytest -m eval_ci          # deterministico + coseno sugli snapshot
```

Il **Binario B** (modello vero + giudice LLM) gira **solo sul tuo Mac**:

```bash
make eval-snapshot   # rigenera tests/evals/snapshots/ col modello reale (dopo modifiche a prompt/modello)
make eval-local      # pipeline reale + giudice Llama 3 8B/70B  →  pytest -m eval
```

> In sintesi: zero LLM in CI, giudice capace in locale. È esattamente la separazione che proponi, ed è la prassi più solida per evitare una CI inaffidabile.

### 6.5 Caching e tempi

I caching di `pip`, `npm` e dei browser Playwright sono già impostati sopra: tengono i job veloci sotto i 2–3 minuti dopo il primo run. Il free tier di GitHub Actions è ampiamente sufficiente per i job di codice.

---

## 7. Roadmap consigliata (ordine e sforzo)

L'ordine **non** è "prima tutto il Livello 1, poi tutto il 2…": conviene mettere subito le fondamenta e una CI minima, poi crescere. Stime per una persona che conosce il codice.

| Fase | Cosa | Esito | Sforzo |
|---|---|---|---|
| **0 — Sblocco** | Fix dei test rotti (§1.1–1.2), `pyproject.toml`, `conftest.py`, `requirements-dev.txt` | la suite esistente torna verde e affidabile | ~½ giornata |
| **1 — Backend** | Unit (§3.1) + nodi (§3.2) + grafo (§3.3) + API (§3.4); coverage | rete di sicurezza sul backend | 1–2 giorni |
| **2a — CI minima** | `git init` + repo GitHub + `ci.yml` con solo lint e backend‑tests | ogni push è verificato | ~½ giornata |
| **3 — Evals** | Golden dataset + snapshot + harness pytest (deterministico → semantico); Binario A (`eval_ci`) in CI | qualità AI misurabile, model-free in CI | 1–2 giorni |
| **4 — E2E** | Playwright + `data-testid` + mock SSE; aggiungere job `frontend-e2e` alla CI | flussi utente verificati | 1–2 giorni |
| **5 — Giudice locale** | `make eval-local` (Binario B: modello reale + giudice Llama 3 8B/70B) + soglia di pass‑rate; rigenerazione snapshot | regressioni di modello/prompt intercettate in locale | ~½ giornata |

Mettere la **CI minima in Fase 2a** (subito dopo il backend) dà valore prima: ogni commit successivo è protetto mentre costruisci evals ed E2E.

---

## 8. Struttura dei file da creare

```
ai_lead_qualifier/
├── pyproject.toml                      # NEW — config pytest/coverage
├── requirements-dev.txt                # NEW — dipendenze di test
├── Makefile                            # NEW (opz.) — scorciatoie comandi
├── .github/
│   └── workflows/
│       └── ci.yml                      # NEW — lint, backend (+eval_ci), e2e (su PR) — nessun LLM
├── tests/
│   ├── conftest.py                     # NEW — fixture condivise
│   ├── unit/                           # NEW — test puri
│   ├── integration/                    # NEW — grafo + API mockati
│   │   └── (sostituisce/espande test_graph.py e test_webhook_adapter.py)
│   └── evals/                          # NEW — golden dataset + eval
│       ├── datasets/                   # include i CSV già esistenti
│       ├── snapshots/                  # generazioni registrate (Binario A, model-free)
│       └── capture_snapshots.py        # genera gli snapshot col modello reale (make eval-snapshot)
└── frontend/
    ├── playwright.config.js            # NEW
    ├── e2e/                            # NEW — *.spec.js
    └── index.html                      # MOD (lieve) — aggiunta data-testid
```

I file di codice di produzione toccati sono **solo**: i fix dei test del §1 e i `data-testid` opzionali nell'HTML. Tutto il resto è additivo.

---

## 9. Appendice — comandi e Makefile

Un `Makefile` rende i comandi memorizzabili e identici tra locale e CI:

```makefile
install:      ## installa le dipendenze di sviluppo
	pip install -r requirements-dev.txt

test:         ## test veloci (no eval live): unit + integration + eval_ci (model-free)
	pytest -m "not eval"

cov:          ## test con report di copertura
	pytest -m "not eval" --cov --cov-report=term-missing

eval-local:   ## Binario B: evals LIVE col modello reale + giudice LLM (richiede Ollama+Chroma)
	pytest -m eval

eval-snapshot: ## rigenera tests/evals/snapshots/ col modello reale (dopo modifiche a prompt/modello)
	python -m tests.evals.capture_snapshots

lint:         ## lint + type-check
	ruff check . && mypy core agents ingestion adapters api

e2e:          ## test end-to-end del frontend
	cd frontend && npx playwright test

check: lint test   ## tutto ciò che gira su ogni PR
```

Comandi rapidi durante lo sviluppo:

```bash
pytest tests/unit -v                  # solo gli unit, in watch mentre scrivi codice
pytest -m "not eval" -k mapper        # solo i test che citano "mapper"
pytest --cov --cov-report=html        # report HTML navigabile in htmlcov/
cd frontend && npx playwright test --ui  # E2E in modalità interattiva
```

---

## 10. Recap delle decisioni

- **Output:** questo documento di pianificazione, nessuna modifica al codice (a parte i fix indicati, da fare in Fase 0).
- **Giudice evals:** deterministico + semantico (coseno con embedder piccolo) ovunque; **giudice LLM solo in locale** sul Mac con modello capace (Llama 3 8B/70B) — **mai un 1B, mai in CI**. Cloud GPT‑4 opt‑in a pagamento, spento.
- **CI:** GitHub Actions, con `git init` + repo come prerequisito. **Nessun LLM nei runner.**
- **LLM/Chroma nei test:** due binari — A) in CI mock + evals model-free su **generazioni registrate** (snapshot); B) modello reale + giudice LLM **solo in locale** (`make eval-local`). Tradeoff: la deriva di modello/prompt la intercetta il Binario B, da lanciare prima di mergiare modifiche sensibili.
- **Air‑gapped:** vincolo del prodotto, non dell'ambiente di test; la CI cloud testa il codice, il deployment resta isolato.

**Primo passo operativo quando vorrai partire:** Fase 0 — sistemare `tests/test_graph.py` (aggiungere `tenant_id`, correggere i target di patch a `core.graph.mapper_node` e `agents.extractor._call_openai_compatible`), aggiungere `pyproject.toml` e `conftest.py`, e verificare che `pytest -m "not eval"` sia verde.



