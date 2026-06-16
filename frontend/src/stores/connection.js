// src/stores/connection.js
// ------------------------------------------------------------------
// Store Alpine: stato di connessione (/health), tenant selezionato e
// gestione dei tenant noti (+ onboarding di nuovi tenant a testo libero — B2).

import { INITIAL_TENANTS, DEFAULT_TENANT } from "../config.js";

export default {
  apiConnected: false,
  tenants: [...INITIAL_TENANTS], // lista nota (config.js + nuovi aggiunti a runtime)
  tenantId: DEFAULT_TENANT, // tenant attivo
  newTenant: "", // input a testo libero per onboardare un tenant nuovo

  /**
   * Aggiunge un tenant nuovo alla lista (se assente) e lo seleziona come attivo.
   * Abilita l'onboarding dalla UI: la prima /ingest/stream con questo ID crea
   * la collezione catalogue_{tenant_id} lato backend.
   * Sanitizza l'ID come fa il backend ([A-Za-z0-9_-]) per evitare sorprese.
   */
  addTenant(id) {
    const raw = (id ?? this.newTenant ?? "").trim();
    const clean = raw.replace(/[^A-Za-z0-9_-]/g, "");
    if (!clean) return false;
    if (!this.tenants.includes(clean)) this.tenants.push(clean);
    this.tenantId = clean;
    this.newTenant = "";
    return true;
  },
};
