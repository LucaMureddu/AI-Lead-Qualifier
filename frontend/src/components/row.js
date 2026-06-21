// src/components/row.js — V3 (Pricing Ibrido Tipizzato)
// ------------------------------------------------------------------
// Componente Alpine di RIGA del catalogo. Registrato in app.js come:
//
//   Alpine.data("rowComponent", rowComponent);
//
// e usato nel markup dentro l'x-for:
//
//   <tr x-data="rowComponent(item)"> … </tr>
//
// Contratto di stato (per ogni riga):
//   original        — clone profondo dei dati server; NON mutato fino al 200 OK
//   edit            — clone reattivo legato agli input via x-model
//   isDirty (get)   — true quando edit ≠ original (confronto null-safe)
//   isSaving        — true durante la PATCH in volo (disabilita i bottoni)
//   savedFlash      — mostra il ✓ per 2 s dopo un salvataggio riuscito
//
// Invarianti di prezzo (rispecchiano il CHECK constraint del DB):
//   FIXED    → price numerico ≥ 0
//   FREE     → price forzato a 0    (input read-only / badge)
//   VARIABLE → price forzato a null (input nascosto / badge "Su richiesta")
//
// Anti-pattern EVITATI:
//   - Auto-save on-change: la PATCH parte SOLO dal bottone "Salva".
//     Cambiare il dropdown non invia nulla → niente stati intermedi
//     incoerenti al backend (che violerebbero il CHECK constraint → 422).
//   - Crash su null: nessun `price.toLocaleString()`; ogni accesso al prezzo
//     è protetto da null-check / coercizione.
//   - DOM vanilla: nessun document.getElementById; tutto via direttive Alpine.
//
// Sicurezza: i log mostrano solo un frammento dell'id e lo status HTTP,
// mai il payload completo.

import { patchCatalogueItem } from "../api.js";

/** Clone profondo robusto (structuredClone con fallback JSON). */
function clone(obj) {
  try {
    return structuredClone(obj);
  } catch {
    return JSON.parse(JSON.stringify(obj));
  }
}

/** Normalizzatore null-safe: null, undefined e "" sono equivalenti a null. */
function norm(v) {
  return v === null || v === undefined || v === "" ? null : v;
}

/**
 * Factory del componente di riga.
 * @param {{id:string, service:string, price_type:string, price:number|null, description:string|null}} item
 */
export default function rowComponent(item) {
  return {
    // ── Stato ────────────────────────────────────────────────────────────────
    original: clone(item),
    edit: clone(item),
    isSaving: false,
    savedFlash: false,

    // ── Computed ───────────────────────────────────────────────────────────────

    /** True se la riga ha modifiche non salvate (confronto campo-per-campo, null-safe). */
    get isDirty() {
      return (
        norm(this.edit.service) !== norm(this.original.service) ||
        this.edit.price_type !== this.original.price_type ||
        norm(this.edit.price) !== norm(this.original.price) ||
        norm(this.edit.description) !== norm(this.original.description)
      );
    },

    /** Etichetta IT del tipo prezzo, per badge/title accessibili. */
    get priceTypeLabel() {
      return (
        { FIXED: "Fisso", FREE: "Gratis", VARIABLE: "Su richiesta" }[
          this.edit.price_type
        ] || this.edit.price_type
      );
    },

    /** Frammento d'id per messaggi/log (mai l'id intero nei toast). */
    get idTail() {
      return "…" + String(this.edit.id).slice(-6);
    },

    // ── Handlers ───────────────────────────────────────────────────────────────

    /**
     * Coercizione del prezzo quando cambia il tipo (NON invia nulla al backend).
     * Mantiene la UI coerente con l'invariante del DB prima del "Salva".
     */
    onPriceTypeChange() {
      switch (this.edit.price_type) {
        case "VARIABLE":
          this.edit.price = null; // su richiesta → prezzo assente
          break;
        case "FREE":
          this.edit.price = 0; // gratis → 0 read-only
          break;
        case "FIXED":
          // Da VARIABLE (null) → svuota così l'utente digita un valore reale.
          if (this.edit.price === null || this.edit.price === undefined) {
            this.edit.price = "";
          }
          break;
      }
    },

    /** Annulla le modifiche locali — ripristina l'ultimo snapshot confermato. */
    discard() {
      this.edit = clone(this.original);
    },

    /**
     * Invia la PATCH con price_type + price (sempre insieme, per soddisfare
     * il CHECK constraint in modo atomico).
     *
     * 200 → aggiorna `original` dal server, flash ✓, toast di successo.
     * 422 (CheckViolationError) → rollback edit = clone(original) + toast errore.
     * Altri errori → toast errore SENZA rollback (l'utente può correggere e ritentare).
     */
    async save() {
      if (!this.isDirty || this.isSaving) return;

      // Validazione client-side: FIXED richiede un numero ≥ 0.
      // Previene 422 inutili su stati palesemente incoerenti.
      if (this.edit.price_type === "FIXED") {
        const n = parseFloat(this.edit.price);
        if (Number.isNaN(n) || n < 0) {
          this.$store.toast.error(
            `Inserisci un prezzo valido (≥ 0) per "${this.edit.service}".`
          );
          return;
        }
      }

      this.isSaving = true;

      // Payload minimale: solo i campi cambiati. I campi prezzo viaggiano
      // sempre in coppia (price_type + price) quando uno dei due cambia.
      const payload = {};

      if (norm(this.edit.service) !== norm(this.original.service)) {
        payload.service = (this.edit.service || "").trim();
      }

      if (norm(this.edit.description) !== norm(this.original.description)) {
        payload.description = norm(this.edit.description);
      }

      const priceDirty =
        this.edit.price_type !== this.original.price_type ||
        norm(this.edit.price) !== norm(this.original.price);

      if (priceDirty) {
        payload.price_type = this.edit.price_type;
        payload.price =
          this.edit.price_type === "VARIABLE"
            ? null
            : this.edit.price_type === "FREE"
              ? 0
              : parseFloat(this.edit.price);
      }

      try {
        const updated = await patchCatalogueItem(this.edit.id, payload);

        // Sincronizza dal server (cattura eventuali coercizioni lato backend).
        this.original = clone(updated);
        this.edit = clone(updated);

        this.savedFlash = true;
        setTimeout(() => {
          this.savedFlash = false;
        }, 2000);

        this.$store.toast.success(`"${updated.service}" aggiornato.`);
      } catch (err) {
        const is422 = /\b422\b/.test(err.message || "");

        if (is422) {
          // Rollback: la UI torna allo stato confermato, nessun blocco persistente.
          this.edit = clone(this.original);
          this.$store.toast.error(
            `Vincolo DB violato (${this.idTail}): combinazione tipo/prezzo non valida.`
          );
        } else {
          // Errore non-422: conserva edit così l'utente può correggere e ritentare.
          this.$store.toast.error(
            `Salvataggio non riuscito (${this.idTail}): ${err.message}`
          );
        }

        // SICUREZZA: logga solo frammento id + messaggio, mai il payload.
        console.error(
          `[catalog] PATCH fallita id=${this.idTail}`,
          err.message
        );
      } finally {
        this.isSaving = false;
      }
    },
  };
}
