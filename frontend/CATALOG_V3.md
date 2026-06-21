# Catalogo Servizi — Frontend V3 (Pricing Ibrido Tipizzato)

Rifacimento della sezione **Catalogo Servizi** per supportare il backend V3
(`price_type` ∈ `FIXED | FREE | VARIABLE`, con `price: null` obbligatorio per
`VARIABLE`) e un sistema di **toast globali**. Integrato nel build Vite esistente.

## File toccati

| File | Ruolo |
| --- | --- |
| `src/stores/toast.js` | **Nuovo.** Store Alpine dei toast: `success/error/info`, TTL configurabile, impilabili, `remove`/`clear`. |
| `src/components/row.js` | **Nuovo.** Factory `rowComponent(item)`: cloni `original`/`edit`, `isDirty` null-safe, coercizioni prezzo, PATCH + rollback su 422. |
| `src/stores/catalog.js` | **Riscritto.** Solo lista paginata + `items[]` (fonte di verità). Niente più modal né `price.toLocaleString()` (causa dei crash su `null`). |
| `src/app.js` | Registra `Alpine.store('toast', toast)` e `Alpine.data('rowComponent', rowComponent)`; `logout()` svuota i toast. |
| `index.html` | Container toast globale (top-right), nuova tabella interattiva inline al posto del vecchio modal, titolo header per la view `catalog`. |

## Come si collega

Tutto passa per `src/app.js`, che è già l'entry di Vite (`<script type="module" src="/src/app.js">` in `index.html`). I nuovi moduli sono importati lì:

```js
import toast from "./stores/toast.js";
import rowComponent from "./components/row.js";
// ...
Alpine.store("toast", toast);
Alpine.data("rowComponent", rowComponent);
```

Nessuno script CDN: in coerenza col progetto, Alpine e Tailwind restano quelli
del build (`alpinejs` da `node_modules`, Tailwind via PostCSS). Il markup usa
`x-data="rowComponent(item)"` e legge `$store.toast` / `$store.catalog`.

### Dev / build

```bash
npm install        # se cambi macchina/arch (rigenera i binari nativi di rollup/esbuild)
npm run dev        # Vite dev server
npm run build      # bundle di produzione
```

> Nota: il bundle richiede i binari nativi di rollup/esbuild per la **tua**
> piattaforma. Se hai copiato `node_modules` da un'altra architettura, esegui
> `npm install` per rigenerarli.

## Logica chiave (rowComponent)

- **Salvataggio esplicito.** La `PATCH` parte solo dal bottone *Salva* (mai
  on-change del dropdown), così non si inviano stati intermedi incoerenti che
  violerebbero il `CHECK` constraint.
- **Null-safe.** Confronti e formattazioni usano un normalizzatore
  (`null === undefined === ""`); nessun accesso diretto a `price.toLocaleString()`.
- **Invarianti prezzo:** `FIXED` → numero ≥ 0; `FREE` → `0` (badge read-only);
  `VARIABLE` → `null` (badge "Su richiesta", input nascosto).
- **Flusso PATCH:**
  - `200` → `original` sincronizzato dalla risposta server, flash ✓, toast success, `isDirty` reset.
  - `422` (CheckViolationError) → `edit = clone(original)` (rollback), toast error.
  - Altri errori → nessun rollback (edit preservato per ritentare), toast error.
- **Sicurezza:** i log mostrano solo un frammento dell'id e lo status HTTP, mai il payload.

## Toast store

```js
Alpine.store("toast").success("Servizio aggiornato.");
Alpine.store("toast").error("Vincolo DB violato (422).");
Alpine.store("toast").info("…", 6000); // TTL custom
```

Impilabili (array `items`), autochiusura via TTL, chiusura manuale con `remove(id)`.
Il container vive una sola volta in `index.html`, in alto a destra, reattivo a `$store.toast.items`.
