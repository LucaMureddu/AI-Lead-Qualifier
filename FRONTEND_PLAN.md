# Piano di Sviluppo Frontend — AI Lead Qualifier

> Documento operativo per la costruzione dell'interfaccia web del sistema di qualificazione lead.
> Pensato come bussola: stack, architettura, design system e piano in 3 fasi con criteri di accettazione.

---

## 0. Premessa: allineamento col backend (LEGGERE PRIMA)

Questo piano è stato verificato contro il codice reale di `api/routes.py`. I disallineamenti tra la visione UI e i contratti effettivi degli endpoint sono stati discussi e **risolti** (vedi decisioni qui sotto).

### 0.1 Decisioni prese

| # | Tema | Decisione | Implicazioni |
|---|---|---|---|
| **B1** | **Upload del catalogo** | ✅ **Implementato:** `POST /upload` lato backend + dropzone drag&drop nella UI. | L'endpoint riceve il file, lo salva in una cartella tenant-scoped e ritorna `{file_path, file_format}` da passare a `/ingest/stream`. Esperienza moderna e coerente air-gapped (upload locale browser→server). Spec e stato in §9. |
| **B2** | **Selettore tenant** | ✅ **Dropdown client-side + input "Nuovo tenant" a testo libero.** | Nessuna modifica al backend. Il dropdown elenca i tenant noti (da `config.js`); il campo a testo libero permette di **onboardare dalla UI** un cliente nuovo digitandone l'ID. La prima `/ingest/stream` con quell'ID crea la collezione `catalogue_{tenant_id}` e di fatto "registra" il tenant. `GET /tenants` resta un miglioramento futuro (vedi §8). |

> **Razionale B2:** l'onboarding di un cliente nuovo *deve* poter partire dalla UI. Un dropdown puro (hardcoded o dinamico) può solo scegliere tra tenant esistenti, mai crearne uno → blocco uovo/gallina. L'input a testo libero è quindi la parte indispensabile; il dropdown è comodità.

### 0.2 Contratti verificati (da rispettare nel codice frontend)

**Endpoint disponibili:**

- `GET /health` → `{"status":"ok","service":"ai_lead_qualifier"}` (per il badge "API Connected").
- `POST /qualify/stream` → SSE. Eventi: `log`, `done`, `error`.
- `POST /qualify` → JSON sincrono (`QualifyResponse`). Per integrazioni non-SSE.
- `POST /ingest/stream` → SSE. Eventi: `log`, `interrupt`, `done`, `error`. Header di risposta **`X-Thread-Id`**.
- `POST /ingest/{thread_id}/approve` → JSON sincrono (`ApprovalResponse`).
- `POST /upload` → ✅ **implementato** (B1). Riceve `multipart/form-data` (`file` + `tenant_id`), ritorna `{file_path, file_format}`. Spec in §9.

**Dettagli critici:**

1. **SSE via POST**: gli endpoint stream sono `POST` con body JSON. `EventSource` nativo supporta solo `GET`, quindi serve un parser custom basato su `fetch` + `ReadableStream` (vedi §4.1).
2. **`X-Thread-Id`**: per `/ingest/stream` il `thread_id` necessario a chiamare `/approve` arriva **nell'header di risposta**, va catturato prima di consumare lo stream.
3. **L'ingest si chiude sull'interrupt**: dopo `event: interrupt` il generatore SSE fa `return` → la connessione termina. La UI riceve l'interrupt, lo stream finisce, poi la UI fa una POST separata a `/approve`. Il risultato del finalize torna come **JSON dalla risposta di `/approve`**, non da altro streaming.
4. **Flusso di rifiuto a due passi**: `POST /approve` con `approved:false` chiude la run (routing a END). Per riprocessare con feedback, il testo va passato come `review_feedback` in una **nuova** chiamata a `/ingest/stream` (non basta la `/approve`).
5. **CORS**: backend aperto a `*` → dev su `:3000` verso `:8000` funziona senza proxy.

---

## 1. Obiettivo del Progetto

Costruire un'interfaccia web professionale, moderna e performante per interagire col backend di qualificazione lead (FastAPI + LangGraph). L'interfaccia deve gestire flussi asincroni (SSE), il pattern Human-in-the-Loop (HITL) e il contesto multi-tenant — mantenendo la coerenza col principio **air-gapped** del backend (zero dipendenze da CDN esterni a runtime).

---

## 2. Stack Tecnologico

| Ambito | Scelta | Razionale |
|---|---|---|
| Build tool | **Vite** | Dev server istantaneo (HMR), bundle ottimizzato e tree-shaken. |
| Stile | **Tailwind CSS** via PostCSS | Compila solo le classi usate → CSS finale minuscolo. Niente CDN. |
| Logica reattiva | **Alpine.js** | Reattività dichiarativa "HTML-first", ~15kb, bundlato in locale. |
| Comunicazione | `fetch` + `ReadableStream` | Unico modo per consumare SSE su endpoint `POST`. |
| Standard | Vanilla JS modulare (ES6) | Manutenibilità, zero framework pesanti. |
| Font | **Self-hosted** (Inter + JetBrains Mono via `@fontsource`) | Coerenza air-gapped: niente Google Fonts CDN. |

**Coerenza air-gapped:** ogni asset (JS, CSS, font, icone) deve essere bundlato da Vite. Vietato `<script src="cdn...">`, vietati font remoti, vietate icon-font da CDN. Per le icone usare SVG inline o un set installato via npm (es. `lucide`).

---

## 3. Architettura del Codice

```
frontend/
├── index.html              # shell: sidebar + header + main container
├── package.json
├── vite.config.js          # base, server.port=3000, proxy opzionale verso :8000
├── postcss.config.js
├── tailwind.config.js      # palette, font, content paths
├── .env                    # VITE_API_BASE_URL=http://localhost:8000
└── src/
    ├── app.js              # regia: inizializza Alpine, gli store e i componenti
    ├── api.js              # fetch verso gli endpoint + parser SSE-via-POST + upload
    ├── ui.js               # manipolazioni DOM di basso livello (autoscroll, focus, dropzone)
    ├── config.js           # API base URL, lista tenant client-side, costanti
    ├── style.css           # @tailwind + @apply per componenti (btn, card, terminal)
    └── stores/
        ├── connection.js   # store Alpine: stato /health, tenant selezionato + nuovi tenant
        ├── qualify.js      # store Alpine: stato vista qualificazione
        └── ingest.js       # store Alpine: stato vista onboarding + HITL
```

**Separazione delle responsabilità:**

- `api.js` non tocca mai il DOM: ritorna dati / invoca callback su evento.
- Gli **store Alpine** sono l'unica fonte di verità dello stato reattivo.
- `ui.js` gestisce solo effetti imperativi che Alpine non copre bene (autoscroll del terminale, gestione del focus, eventi drag&drop della dropzone).
- `app.js` collega tutto: registra gli store, fa partire l'health-check, espone i metodi richiamati dal markup `x-on`.

---

## 4. Logica Core

### 4.1 Parser SSE-via-POST + upload (`src/api.js`)

Funzione unica riusata da entrambi gli stream. Cattura l'header `X-Thread-Id`, bufferizza i chunk e splitta sui doppi newline (`\n\n`), che il backend usa come delimitatore di frame.

```js
// src/api.js
const BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

/**
 * Consuma uno stream SSE da un endpoint POST.
 * @param {string} path        es. "/qualify/stream"
 * @param {object} body        payload JSON
 * @param {object} handlers    { onEvent(frame), signal }
 * @returns {Promise<{threadId: string|null}>}
 */
export async function streamSSE(path, body, { onEvent, signal } = {}) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const threadId = res.headers.get("X-Thread-Id"); // serve per /approve (ingest)
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const rawFrame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      onEvent?.(parseFrame(rawFrame));
    }
  }
  return { threadId };
}

function parseFrame(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  return { event, data: dataLines.join("\n") };
}

// --- Chiamate concrete ---

export const qualifyStream = (payload, handlers) =>
  streamSSE("/qualify/stream", payload, handlers);

export const ingestStream = (payload, handlers) =>
  streamSSE("/ingest/stream", payload, handlers);

/**
 * Carica un file catalogo (B1). Ritorna { file_path, file_format }.
 * @param {File} file        file scelto dalla dropzone
 * @param {string} tenantId  tenant proprietario (scopa la cartella di salvataggio)
 */
export async function uploadCatalogue(file, tenantId) {
  const form = new FormData();
  form.append("file", file);
  form.append("tenant_id", tenantId);
  const res = await fetch(`${BASE}/upload`, { method: "POST", body: form });
  if (!res.ok) throw new Error(`Upload fallito: HTTP ${res.status}`);
  return res.json(); // { file_path, file_format }
}

export async function approveIngestion(threadId, decision) {
  const res = await fetch(`${BASE}/ingest/${threadId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(decision), // { approved: bool, feedback?: string }
  });
  if (res.status === 404) throw new Error("Nessuna run sospesa per questo thread_id.");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json(); // ApprovalResponse
}

export async function checkHealth() {
  try {
    const res = await fetch(`${BASE}/health`);
    return res.ok;
  } catch {
    return false;
  }
}
```

### 4.2 Modello di stato (store Alpine)

```js
// src/stores/qualify.js — registrato in app.js con Alpine.store('qualify', ...)
{
  isLoading: false,
  logs: [],            // array di stringhe SSE (event: log)
  result: null,        // { total_quote, mapped_services } da event: done
  error: null,
}

// src/stores/ingest.js
{
  phase: "idle",       // idle | uploading | processing | review | done | error
  file: null,          // File selezionato dalla dropzone
  filePath: null,      // ritornato da POST /upload
  fileFormat: null,    // "csv" | "json" | "xlsx" (da /upload o estensione)
  threadId: null,      // catturato da X-Thread-Id
  logs: [],
  reviewPayload: null, // { flagged_items, confidence_score, ... } da event: interrupt
  result: null,        // ApprovalResponse da /approve
  feedback: "",        // testo per il rifiuto/correzione
  error: null,
}

// src/stores/connection.js
{
  apiConnected: false,
  tenants: [],            // lista nota, caricata da config.js (+ eventuali nuovi)
  tenantId: "cliente_acme_01",  // tenant attivo
  newTenant: "",          // input a testo libero per onboardare un tenant nuovo
  // addTenant(id): aggiunge id alla lista (se non presente) e lo seleziona
}
```

---

## 5. Design System ("Modern Enterprise / Minimalista")

### Palette (Tailwind)

| Ruolo | Classe | Uso |
|---|---|---|
| Sfondo app | `bg-slate-50` | Area di lavoro, riposa la vista. |
| Superfici | `bg-white` + `border-slate-200` + `shadow-sm` | Card, pannelli. |
| Testo | `text-slate-800` (titoli), `text-slate-500` (metadati) | Gerarchia tipografica. |
| Azione primaria | `indigo-600` (`hover:indigo-700`) | Bottoni principali. |
| Successo | `emerald-500/600` | Log di successo, `[DELIVERY] SUCCESS`. |
| Errore / flag | `rose-500` + `bg-rose-50` | Righe flaggate, errori. |
| Terminale | `bg-slate-900` + `text-slate-100` | Console SSE. |

### Tipografia

- UI: **Inter** (self-hosted via `@fontsource/inter`).
- Log/terminale: **JetBrains Mono** (self-hosted via `@fontsource/jetbrains-mono`).

### Elementi

- Bordi morbidi: `rounded-lg` / `rounded-xl`.
- Profondità leggera: `shadow-sm`, mai ombre pesanti.
- Focus marcati su input: `focus:ring-2 focus:ring-indigo-500`.

### Componenti base (in `style.css` con `@apply`)

```css
.btn-primary  { @apply px-4 py-2 rounded-lg bg-indigo-600 text-white font-medium
                 hover:bg-indigo-700 disabled:opacity-50 transition; }
.btn-success  { @apply px-4 py-2 rounded-lg bg-emerald-600 text-white font-medium hover:bg-emerald-700; }
.btn-danger   { @apply px-4 py-2 rounded-lg bg-rose-600 text-white font-medium hover:bg-rose-700; }
.card         { @apply bg-white border border-slate-200 rounded-xl shadow-sm p-6; }
.input-text   { @apply w-full rounded-lg border border-slate-200 px-3 py-2
                 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none; }
.terminal     { @apply bg-slate-900 text-slate-100 font-mono text-sm rounded-xl p-4
                 overflow-y-auto; }
.dropzone     { @apply border-2 border-dashed border-slate-300 rounded-xl p-10
                 text-center text-slate-500 transition; }
.dropzone--active { @apply border-indigo-500 bg-indigo-50 text-indigo-600; }
```

### Colorazione dei tag di log (mappa tag → classe)

Il backend emette log con prefissi `[SANITIZER]`, `[EXTRACTOR]`, `[MAPPER]`, `[CALCULATOR]`, `[DELIVERY] SUCCESS`, `[ERROR]`, ecc. In `ui.js` una piccola funzione assegna un colore al tag:

```js
const TAG_COLORS = {
  SANITIZER: "text-sky-400",
  EXTRACTOR: "text-violet-400",
  MAPPER: "text-amber-400",
  CALCULATOR: "text-cyan-400",
  DELIVERY: "text-emerald-400",
  ERROR: "text-rose-400",
  HUMAN_FALLBACK: "text-rose-300",
};
```

---

## 6. Layout Globale

- **Sidebar (sinistra):** navigazione. Voci: *Qualifica Lead*, *Gestione Cataloghi*, *Impostazioni*.
- **Top header:**
  - **Selettore Tenant** = dropdown con i tenant noti (da `config.js`) **+** campo "Nuovo tenant" a testo libero con bottone "+". Digitando un ID nuovo e confermando, viene aggiunto alla lista in sessione e selezionato come tenant attivo (abilita l'onboarding dalla UI).
  - Icona utente.
  - Badge **"API Connected"** (verde se `/health` risponde, altrimenti rosso — polling ogni ~15s).
- **Main content:** `max-w-7xl` centrato, padding generoso.

---

## 7. Piano in 3 Fasi

### Fase 1 — Impalcatura e Infrastruttura

**Obiettivo:** fondamenta air-gapped + design system di base + comunicazione col backend.

**Task:**

1. `npm create vite@latest` (vanilla), pulizia template.
2. Installare e configurare Tailwind via PostCSS (`tailwind.config.js` con `content` sui file `index.html` e `src/**/*.js`).
3. Installare Alpine.js e i font self-hosted (`@fontsource/inter`, `@fontsource/jetbrains-mono`); importarli in `app.js`.
4. Scrivere `src/api.js` completo (parser SSE §4.1 + `uploadCatalogue` + `checkHealth`).
5. Definire `config.js` (`VITE_API_BASE_URL`, lista tenant iniziale).
6. Registrare gli store Alpine (`connection`, `qualify`, `ingest`); implementare `addTenant()` nello store connection.
7. Costruire il layout shell in `index.html` (sidebar + header con selettore tenant + nuovo tenant + main) con i componenti base in `style.css`.
8. Implementare il badge "API Connected" con polling su `/health`.

**Criteri di accettazione:**

- `npm run dev` avvia il frontend su `:3000`.
- Il badge diventa verde quando il backend (`:8000`) risponde a `/health`, rosso se spento.
- Il selettore tenant mostra la lista e permette di aggiungere/selezionare un tenant nuovo a testo libero.
- Layout responsive (sidebar collassabile su mobile), font e palette applicati.

---

### Fase 2 — Vista "Qualificazione Lead"

**Obiettivo:** rendere visibile in tempo reale il lavoro del grafo di qualificazione.

**Layout:** split screen (2 colonne desktop, impilate su mobile).

- **Colonna sinistra (input):** `textarea` (placeholder "Incolla qui l'email o la richiesta del lead…"), bottone `.btn-primary` "Genera Preventivo AI" con spinner (`x-show="$store.qualify.isLoading"`). Validazione: `raw_text` min 10 caratteri (vincolo del backend).
- **Colonna destra (elaborazione + risultato):**
  - **Console SSE** (`.terminal`): ogni `event: log` appende una riga colorata per tag; autoscroll automatico (`ui.js`).
  - **Card Preventivo** (nascosta finché non arriva `event: done`): totale grande in grassetto (es. `800.00 €`), lista servizi da `mapped_services` con `matched_name`, `price`, e `distance` come metadato piccolo/grigio.

**Logica:**

```js
// pseudo: handler del bottone in app.js
async function generateQuote() {
  const s = Alpine.store("qualify");
  s.isLoading = true; s.logs = []; s.result = null; s.error = null;
  try {
    await qualifyStream(
      { tenant_id: Alpine.store("connection").tenantId, raw_text: text },
      { onEvent: (f) => {
          if (f.event === "log")   s.logs.push(f.data);
          if (f.event === "done")  s.result = JSON.parse(f.data);
          if (f.event === "error") s.error = JSON.parse(f.data).error;
        } }
    );
  } catch (e) { s.error = e.message; }
  finally { s.isLoading = false; }
}
```

**Criteri di accettazione:**

- Incollando un testo e premendo il bottone, i log compaiono progressivamente (riga per riga).
- All'`event: done` la card preventivo appare con totale e servizi corretti.
- Gli errori (`event: error` o catalogo tenant mancante) sono mostrati con stile `rose`.

> Nota realismo: gli SSE arrivano un frame **per nodo**, quindi l'effetto è "riga per riga", non carattere per carattere.

---

### Fase 3 — Vista "Onboarding Cataloghi" (Human-in-the-Loop)

**Obiettivo:** gestire l'ingestion del catalogo e l'approvazione umana.

> **Prerequisito (B1, ✅ implementato):** `POST /upload` è già attivo lato backend (spec §9). La dropzone carica il file, riceve `{file_path, file_format}` e passa il `file_path` a `/ingest/stream`.

**Flusso UI/UX:**

1. **Dropzone drag&drop** → l'utente trascina/seleziona il file catalogo. Al rilascio: `uploadCatalogue(file, tenantId)` → salva `filePath` e `fileFormat` nello store. Validazione client: estensione in `csv|json|xlsx`, dimensione sotto soglia.
2. **Avvio ingestion:** `POST /ingest/stream` con `{tenant_id, file_path, file_format}`. **Salvare `threadId`** dall'header `X-Thread-Id`. Mostrare loader "Analisi e normalizzazione del catalogo tramite AI in corso…" mentre arrivano gli `event: log`.
3. **Due esiti dallo stream:**
   - `event: done` → ingestion pulita, nessuna revisione. Mostrare riepilogo (`total_items`, `flagged_count`).
   - `event: interrupt` → lo **stream si chiude**; salvare `reviewPayload`, passare a `phase: "review"`.
4. **Schermata "Revisione Necessaria":** tabella / card dei `flagged_items`. Riga con sfondo `bg-rose-50`, label rossa con `flag_reason` e confidenza (es. "Confidenza bassa: 0.40"). Mostrare `raw_data` per tracciabilità.
5. **Action footer sticky:**
   - **Approva** (`.btn-success`) → `approveIngestion(threadId, {approved:true})` → risultato `ApprovalResponse` (JSON). Passare a `phase:"done"`.
   - **Rifiuta / Correggi** (`.btn-danger`) + campo feedback opzionale → due opzioni:
     - rifiuto secco: `approveIngestion(threadId, {approved:false})` → run chiusa.
     - correzione: usare il testo feedback per una **nuova** `/ingest/stream` con `review_feedback` (flusso a due passi, vedi §0.2 punto 4).

**Logica HITL:**

```js
async function uploadAndIngest(file) {
  const conn = Alpine.store("connection");
  const s = Alpine.store("ingest");
  s.phase = "uploading"; s.error = null;
  try {
    const { file_path, file_format } = await uploadCatalogue(file, conn.tenantId);
    s.filePath = file_path; s.fileFormat = file_format;
    await startIngestion({ tenant_id: conn.tenantId, file_path, file_format });
  } catch (e) { s.error = e.message; s.phase = "error"; }
}

async function startIngestion(payload) {
  const s = Alpine.store("ingest");
  s.phase = "processing"; s.logs = []; s.reviewPayload = null; s.error = null;
  try {
    const { threadId } = await ingestStream(payload, {
      onEvent: (f) => {
        if (f.event === "log")       s.logs.push(f.data);
        if (f.event === "interrupt") { s.reviewPayload = JSON.parse(f.data).review_payload; s.phase = "review"; }
        if (f.event === "done")      { s.result = JSON.parse(f.data); s.phase = "done"; }
        if (f.event === "error")     { s.error = JSON.parse(f.data).error; s.phase = "error"; }
      },
    });
    s.threadId = threadId; // catturato dall'header
  } catch (e) { s.error = e.message; s.phase = "error"; }
}

async function approve(approved) {
  const s = Alpine.store("ingest");
  s.result = await approveIngestion(s.threadId, { approved, feedback: s.feedback || null });
  s.phase = "done";
}
```

**Criteri di accettazione:**

- Trascinando un file "sporco" (es. `dirty_catalog.csv`) nella dropzone, parte l'upload, poi l'ingestion; i log scorrono e all'interrupt appare la schermata di revisione con gli item flaggati evidenziati.
- "Approva" scrive su ChromaDB (verificabile poi via `/qualify`) e mostra il riepilogo finale.
- "Rifiuta con feedback" ri-triggera correttamente una nuova run con `review_feedback`.
- Digitando un tenant nuovo nell'header e caricando il suo primo catalogo, la collezione `catalogue_{tenant_id}` viene creata (onboarding dalla UI funzionante).
- Un `thread_id` inesistente restituisce 404 gestito con messaggio chiaro.

**Traguardo:** ciclo SaaS completo — interfaccia sia per *usare* (Fase 2) sia per *configurare/addestrare/onboardare* (Fase 3) il sistema.

---

## 8. Riepilogo gap backend

**Chiuso:**

1. ✅ **`POST /upload`** (B1) — endpoint che riceve il file, lo salva in una cartella tenant-scoped e ritorna `{file_path, file_format}`. **Implementato** (prerequisito della Fase 3). Dettagli in §9.

**Miglioramenti futuri (non bloccanti):**

2. **`GET /tenants`** (B2) — elenca i tenant esistenti dalle collezioni ChromaDB `catalogue_*` per popolare il dropdown senza editare `config.js`. Utile *dopo* la creazione di un tenant; non sostituisce l'input a testo libero per l'onboarding.
3. **Autenticazione** — oggi assente; per produzione servirà almeno un token per scopare le richieste per tenant.
4. **CORS** — in produzione restringere `allow_origins` (oggi `*`).

---

## 9. Endpoint `POST /upload` (backend, B1) — ✅ IMPLEMENTATO

Implementato in `api/routes.py` tramite un router dedicato `upload_router` (path a root `/upload`), registrato in `main.py`. Config in `core/config.py` (`upload_dir`, `upload_max_bytes`) e in `.env.example`. La cartella `uploads/` è in `.gitignore`.

**File toccati:**
- `api/routes.py` — `upload_router`, `UploadResponse`, `_safe_tenant_dirname`, handler `upload_catalogue`.
- `main.py` — `app.include_router(upload_router)`.
- `core/config.py` — `upload_dir` (default `uploads`), `upload_max_bytes` (default 10 MB).
- `.env.example` — `UPLOAD_DIR`, `UPLOAD_MAX_BYTES`.
- `.gitignore` — esclude `uploads/`, `.env`, `data/checkpoints.db*`, `chroma_data/`.

**Request:** `multipart/form-data`
- `file`: il file del catalogo (`UploadFile`).
- `tenant_id`: stringa (form field).

**Comportamento:**

1. Validare l'estensione: solo `.csv`, `.json`, `.xlsx` → altrimenti `400`.
2. Validare la dimensione (es. max 10 MB) → altrimenti `413`.
3. **Sanitizzare il filename** (no path traversal): usare solo il basename, generare un nome sicuro (es. `uuid4 + estensione`).
4. Salvare in una cartella **tenant-scoped**, es. `uploads/{tenant_id}/{safe_name}`, creando la dir se assente.
5. Ricavare `file_format` dall'estensione.

**Response 200 (JSON):**

```json
{
  "file_path": "/abs/path/uploads/cliente_acme_01/3f2c…d9.csv",
  "file_format": "csv"
}
```

**Comportamento errori:** estensione non ammessa → `400`; file vuoto → `400`; oltre `upload_max_bytes` → `413`; `tenant_id` non valido (vuoto dopo sanitizzazione) → `400`; errore di scrittura su disco → `500`.

**Note di sicurezza (applicate):** il filename del client non viene mai usato (solo l'estensione); il file salvato è rinominato con un UUID generato dal server (no path traversal); il `tenant_id` è sanitizzato a `[A-Za-z0-9_-]` prima di diventare nome di cartella; il contenuto non viene aperto qui (lo farà il ChunkingNode). Il `file_path` ritornato è il path assoluto che la UI rigira a `/ingest/stream`.

> Verifica: i moduli compilano puliti e il wiring del router è confermato. Non è stato possibile avviare l'app nel sandbox Linux perché il `.venv` del progetto è una build macOS — il test runtime end-to-end va fatto sulla tua macchina (`python main.py`, poi `POST /upload`).
```

