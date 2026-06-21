// src/stores/catalog.js — V3 (Pricing Ibrido Tipizzato)
// ------------------------------------------------------------------
// Store Alpine: AMMINISTRAZIONE CATALOGO SERVIZI.
//
// Responsabilità (V3):
//  - caricamento paginato da GET /api/catalog/items
//  - possesso dell'array `items` (fonte di verità della tabella)
//  - paginazione (skip/limit) e stato di caricamento/errore
//
// Cosa NON fa più (rispetto a V2):
//  - NESSUN modal: la modifica avviene inline, riga per riga, tramite il
//    componente `rowComponent` (src/components/row.js). Lo store non tiene
//    più stato di edit (editService/editPrice/…): ogni riga clona da sola
//    `original`/`edit` e gestisce il proprio PATCH/rollback.
//  - NESSUNA formattazione `price.toLocaleString()`: la V3 ammette
//    `price: null` (VARIABLE) e il vecchio codice andava in TypeError.
//    La formattazione null-safe vive nel rowComponent / nei badge.

import * as api from "../api.js";

export default {
  // ── Stato tabella ─────────────────────────────────────────────────────────
  /** @type {Array<{id:string, service:string, price_type:string, price:number|null, description:string|null}>} */
  items: [],
  total: 0,
  skip: 0,
  limit: 20,
  loading: false,
  error: null,

  // ── Azioni ────────────────────────────────────────────────────────────────

  /** Carica (o ricarica) la pagina corrente dal backend. */
  async load() {
    this.loading = true;
    this.error = null;
    try {
      const data = await api.listCatalogueItems(this.skip, this.limit);
      // Supporta sia { items, total } sia un array semplice (robustezza API).
      this.items = data.items ?? data ?? [];
      this.total = data.total ?? this.items.length;
    } catch (err) {
      this.error = err.message || "Errore durante il caricamento del catalogo.";
      // Feedback non bloccante anche via toast, se registrato.
      window.Alpine?.store("toast")?.error(this.error);
    } finally {
      this.loading = false;
    }
  },

  /** Vai alla pagina successiva (se esiste). */
  async nextPage() {
    if (this.skip + this.limit >= this.total) return;
    this.skip += this.limit;
    await this.load();
  },

  /** Vai alla pagina precedente (se esiste). */
  async prevPage() {
    if (this.skip === 0) return;
    this.skip = Math.max(0, this.skip - this.limit);
    await this.load();
  },

  // ── Computed helpers (usati dal markup) ────────────────────────────────────

  /** Indice 1-based del primo elemento mostrato (per il footer paginazione). */
  get rangeStart() {
    return this.total === 0 ? 0 : this.skip + 1;
  },

  /** Indice 1-based dell'ultimo elemento mostrato. */
  get rangeEnd() {
    return Math.min(this.skip + this.limit, this.total);
  },
};
