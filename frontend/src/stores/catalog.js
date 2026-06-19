// src/stores/catalog.js
// ------------------------------------------------------------------
// Store Alpine: AMMINISTRAZIONE CATALOGO SERVIZI.
//
// Gestisce:
//  - caricamento paginato da GET /api/catalog/items
//  - apertura / chiusura del modal di modifica
//  - invio PATCH /api/catalog/items/{id} con spinner durante il salvataggio
//  - ricarica automatica della tabella dopo un salvataggio riuscito

import * as api from "../api.js";

export default {
  // ── Stato tabella ─────────────────────────────────────────────────────────
  items: [],
  total: 0,
  skip: 0,
  limit: 20,
  loading: false,
  error: null,

  // ── Stato modal ───────────────────────────────────────────────────────────
  modalOpen: false,
  editId: null,
  editService: "",
  editPrice: 0,
  editDescription: "",
  saving: false,
  saveError: null,

  // ── Azioni ────────────────────────────────────────────────────────────────

  /** Carica (o ricarica) la pagina corrente dal backend. */
  async load() {
    this.loading = true;
    this.error = null;
    try {
      const data = await api.listCatalogueItems(this.skip, this.limit);
      this.items = data.items;
      this.total = data.total;
    } catch (err) {
      this.error = err.message || "Errore durante il caricamento del catalogo.";
    } finally {
      this.loading = false;
    }
  },

  /** Vai alla pagina successiva. */
  async nextPage() {
    if (this.skip + this.limit >= this.total) return;
    this.skip += this.limit;
    await this.load();
  },

  /** Vai alla pagina precedente. */
  async prevPage() {
    this.skip = Math.max(0, this.skip - this.limit);
    await this.load();
  },

  /** Apre il modal di modifica popolando i campi con i valori correnti. */
  openEdit(item) {
    this.editId = item.id;
    this.editService = item.service;
    this.editPrice = item.price;
    this.editDescription = item.description || "";
    this.saveError = null;
    this.modalOpen = true;
  },

  /** Chiude il modal senza salvare. */
  closeEdit() {
    this.modalOpen = false;
    this.saveError = null;
  },

  /** Invia la PATCH, mostra lo spinner, chiude il modal e ricarica la tabella. */
  async saveEdit() {
    if (!this.editService.trim()) {
      this.saveError = "Il nome del servizio non può essere vuoto.";
      return;
    }
    if (this.editPrice < 0) {
      this.saveError = "Il prezzo non può essere negativo.";
      return;
    }

    this.saving = true;
    this.saveError = null;
    try {
      await api.patchCatalogueItem(this.editId, {
        service: this.editService.trim(),
        price: this.editPrice,
        description: this.editDescription || null,
      });
      this.modalOpen = false;
      await this.load();
    } catch (err) {
      this.saveError = err.message || "Errore durante il salvataggio. Riprova.";
    } finally {
      this.saving = false;
    }
  },
};
