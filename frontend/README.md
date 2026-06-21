# AI Lead Qualifier — Frontend

Interfaccia web **air-gapped** per il backend di qualificazione lead (FastAPI + LangGraph).
Vite + Tailwind CSS + Alpine.js, transport di qualificazione via **polling HTTP** (no SSE),
Human-in-the-Loop per l'onboarding cataloghi, admin catalogo inline e preventivi
esportabili (email / PDF). Contesto multi-tenant via JWT RS256.

## Stack

- **Vite** — dev server + bundle tree-shaken
- **Tailwind CSS** (via PostCSS) — solo le classi usate finiscono nel CSS finale
- **Alpine.js** — reattività dichiarativa, ~15 kB, bundlato in locale
- **jsPDF** — generazione PDF preventivi, importato in modo lazy (code-split)
- **fetch + ReadableStream** — usato solo per SSE dell'ingestion catalogo (`POST /ingest/stream`)
- **Font self-hosted** — Inter + JetBrains Mono via `@fontsource` (niente Google Fonts CDN)

Tutti gli asset sono impacchettati da Vite: **nessuna dipendenza da CDN a runtime**.

## Prerequisiti

- Node.js 18+ (consigliato 20+)
- Il backend attivo su `http://localhost:8000`

## Avvio (sviluppo)

```bash
cd frontend
npm install          # installa le dipendenze (richiede accesso al registry npm)
npm run dev          # avvia il dev server su http://localhost:3000
```

Il backend espone CORS configurabile; in dev `CORS_ORIGINS=["http://localhost"]` copre `:3000`.

## Configurazione

`.env` (già presente):

```
VITE_API_BASE_URL=http://localhost:8000
```

Lasciare la variabile vuota (`""`) solo se si serve tutto dallo stesso origin
(in quel caso scommentare il blocco `proxy` in `vite.config.js`).

## Build di produzione

```bash
npm run build        # output in dist/
npm run preview      # serve la build su :3000 per una verifica locale
```

## Struttura

```
frontend/
├── index.html              # shell: sidebar + header + viste (qualify/ingest/catalog/settings)
├── vite.config.js          # server.port=3000, proxy opzionale
├── tailwind.config.js      # palette, font, content paths
├── postcss.config.js
├── .env                    # VITE_API_BASE_URL
└── src/
    ├── app.js              # regia: registra store, health-check, azioni orchestrate (window.App)
    ├── api.js              # fetch, Poller (polling lead), SSE ingestion, tutti gli endpoint
    ├── ui.js               # autoscroll, colori tag log, dropzone, formatter bytes
    ├── config.js           # API base URL, soglie, estensioni ammesse
    ├── quote.js            # generazione email preventivo + PDF (jsPDF lazy)
    ├── style.css           # @tailwind + componenti @apply
    ├── components/
    │   └── row.js          # Alpine component per la riga catalogo (edit inline + PATCH)
    └── stores/
        ├── auth.js         # JWT: login, token, tenantId, logout, gestione 401
        ├── connection.js   # /health polling, tenantId legacy (compat)
        ├── qualify.js      # vista qualificazione: submit lead + Poller lifecycle
        ├── ingest.js       # vista onboarding catalogo + HITL (upload → stream → approve)
        ├── catalog.js      # admin catalogo: lista paginata, load/nextPage/prevPage
        ├── settings.js     # profilo tenant (nome, logo, IBAN, termini)
        └── toast.js        # notifiche non bloccanti
```

## Allineamento col backend (contratti)

| Endpoint | Uso nel frontend |
|---|---|
| `GET /health` | badge "API Connected" (polling ogni ~15 s) |
| `POST /token` | login — restituisce JWT RS256; conservato in `localStorage` |
| `POST /lead` | submit lead → `202 Accepted` + `thread_id`; avvia il Poller |
| `GET /status/{thread_id}` | Poller: polling fino a stato terminale (`completed` / `pending_review` / `error`) |
| `POST /lead/{thread_id}/approve` | sblocca un job `pending_review` (HITL qualificazione) |
| `POST /upload` | dropzone catalogo → `{ object_key, file_format }` |
| `POST /ingest/stream` | SSE ingestion — eventi `log` / `interrupt` / `done` / `error`; header `X-Thread-Id` |
| `POST /ingest/{thread_id}/approve` | decisione HITL ingestion (Approva / Rifiuta / Correggi) |
| `GET /api/catalog/items` | tabella catalogo paginata (`?skip=&limit=`) |
| `PATCH /api/catalog/items/{id}` | modifica inline riga (nome, prezzo, `price_type`, descrizione) |
| `GET /tenants/{id}/profile` | carica profilo tenant in `settings` store |
| `PUT /tenants/{id}/profile` | salva profilo (logo, IBAN, termini, …) |
| `DELETE /api/v1/tenants/{id}/vector-data` | reset hard dei dati vettoriali del tenant |

**Note sul transport:**

- **Qualificazione**: non usa SSE. `POST /lead` → `202` + `thread_id`; il `Poller` (classe in `api.js`) fa `GET /status/{thread_id}` ogni 2 s e si ferma automaticamente allo stato terminale.
- **Ingestion catalogo**: usa SSE (`ReadableStream`). Lo stream si chiude all'evento `interrupt`; il `thread_id` viene letto sia dall'header `X-Thread-Id` sia dal payload dell'evento (robustezza cross-origin). Il flusso "Correggi e riprocessa" è a due passi: `POST /approve {approved:false}` → nuova `/ingest/stream` con `review_feedback`.
- **Pricing ibrido V3**: i `mapped_services` nel risultato di qualificazione portano `price_type` (`FIXED` / `FREE` / `VARIABLE`) al posto del vecchio `is_on_request`. `VARIABLE` ha `price: null`; `quote.js` gestisce i tre rami nella formattazione email e PDF.

## Flusso di lavoro

1. **Qualifica Lead** — autenticati, incolla il testo del lead (min 10 caratteri) e premi Avvia. Lo store `qualify` invia `POST /lead`, avvia il Poller e aggiorna lo stato (`queued → processing → completed / pending_review`). Al completamento compare la card preventivo con totale, servizi mappati e i pulsanti Copia Email / Apri Mail / Scarica PDF. Se la confidence scende sotto soglia, appare il pannello di revisione umana (HITL).
2. **Onboarding Catalogo** — trascina un file (CSV/JSON/XLSX) o selezionalo via dialog; parte upload → ingestion SSE. I log compaiono riga per riga; se il grafo richiede una revisione, gli item flaggati appaiono evidenziati con il footer Approva / Rifiuta / Correggi.
3. **Catalogo Servizi** — tabella paginata dei servizi del tenant con editing inline: clicca su una riga per modificare nome, prezzo, tipo (`FIXED` / `FREE` / `VARIABLE`) e descrizione; il PATCH viene inviato e l'embedding viene ricalcolato in background.
4. **Impostazioni** — profilo aziendale del tenant (logo, ragione sociale, IBAN, termini di pagamento); le modifiche sono persistite su Postgres tramite `PUT /tenants/{id}/profile`.

> **Autenticazione**: inserisci un tenant ID (es. `cliente_acme_01`) nella schermata di login.
> La password non è validata (mock auth dev). Il token JWT RS256 emesso da `/token` viene
> conservato in `localStorage` e allegato a ogni chiamata API nell'header `Authorization: Bearer <token>`.
> Un 401 dal backend emette l'evento DOM `auth:unauthorized`, che `app.js` intercetta per
> eseguire il logout automatico senza refresh di pagina.
