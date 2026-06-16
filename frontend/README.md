# AI Lead Qualifier — Frontend

Interfaccia web **air-gapped** per il backend di qualificazione lead (FastAPI + LangGraph).
Implementa il piano in `FRONTEND_PLAN.md`: Vite + Tailwind + Alpine.js, stream SSE-via-POST,
Human-in-the-Loop per l'onboarding cataloghi e contesto multi-tenant.

## Stack

- **Vite** — dev server + bundle tree-shaken
- **Tailwind CSS** (via PostCSS) — solo le classi usate finiscono nel CSS finale
- **Alpine.js** — reattività dichiarativa, ~15kb, bundlato in locale
- **fetch + ReadableStream** — unico modo per consumare SSE su endpoint `POST`
- **Font self-hosted** — Inter + JetBrains Mono via `@fontsource` (niente Google Fonts CDN)

Tutti gli asset sono impacchettati da Vite: **nessuna dipendenza da CDN a runtime**.

## Prerequisiti

- Node.js 18+ (consigliato 20+)
- Il backend attivo su `http://localhost:8000` (`python main.py` nella cartella radice)

## Avvio (sviluppo)

```bash
cd frontend
npm install          # installa le dipendenze (richiede accesso al registry npm)
npm run dev          # avvia il dev server su http://localhost:3000
```

Il backend espone CORS `*`, quindi `:3000 → :8000` funziona senza proxy.

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
├── index.html              # shell: sidebar + header + viste (qualify/ingest/settings)
├── vite.config.js          # server.port=3000, proxy opzionale
├── tailwind.config.js      # palette, font, content paths
├── postcss.config.js
├── .env                    # VITE_API_BASE_URL
└── src/
    ├── app.js              # regia: registra store, health-check, azioni x-on
    ├── api.js              # fetch + parser SSE-via-POST + upload (nessun DOM)
    ├── ui.js               # autoscroll, colori tag log, dropzone, formatter
    ├── config.js           # API base URL, tenant noti, soglie
    ├── style.css           # @tailwind + componenti @apply
    └── stores/
        ├── connection.js   # /health, tenant attivo + nuovi tenant (addTenant)
        ├── qualify.js      # stato vista qualificazione
        └── ingest.js       # stato vista onboarding + HITL
```

## Allineamento col backend (contratti)

| Endpoint | Uso nel frontend |
|---|---|
| `GET /health` | badge "API Connected" (polling ~15s) |
| `POST /qualify/stream` | vista Qualifica — eventi `log` / `done` / `error` |
| `POST /upload` | dropzone → `{file_path, file_format}` |
| `POST /ingest/stream` | onboarding — `log` / `interrupt` / `done` / `error`, header `X-Thread-Id` |
| `POST /ingest/{thread_id}/approve` | decisione HITL (Approva / Rifiuta) |

Note sull'HITL: lo stream ingest si **chiude** sull'`interrupt`; il `thread_id`
necessario a `/approve` viene letto sia dall'header `X-Thread-Id` sia dal payload
dell'evento `interrupt` (robusto anche cross-origin). Il flusso "Correggi e
riprocessa" è a due passi: `/approve {approved:false}` + nuova `/ingest/stream`
con `review_feedback` (vedi §0.2 del piano).

## Flusso di lavoro

1. **Qualifica Lead** — incolla il testo del lead (min 10 caratteri), avvia lo
   stream; i log compaiono riga per riga (un frame per nodo) e alla fine appare
   la card preventivo con totale e servizi mappati.
2. **Gestione Cataloghi** — trascina un file (csv/json/xlsx); parte upload →
   ingestion; se servono revisioni, gli item flaggati appaiono evidenziati con
   footer Approva / Rifiuta / Correggi.
3. **Impostazioni** — stato API, tenant attivo e gestione/onboarding tenant.

> Onboarding di un nuovo tenant: digita l'ID nell'header (campo "Nuovo tenant"),
> premi `+`, poi carica il primo catalogo: la collezione `catalogue_<tenant>`
> viene creata lato backend alla prima `/ingest/stream`.
