// src/stores/settings.js
// ------------------------------------------------------------------
// Store Alpine: PROFILO AZIENDALE del tenant attivo (mittente del preventivo).
// La fonte di verità è il backend (GET/PUT /tenants/{id}/profile); questo store
// è il modello reattivo lato UI. Mapping snake_case (API) ↔ camelCase (UI) qui.

import { VAT_ENABLED, VAT_RATE, QUOTE_VALIDITY_DAYS } from "../config.js";

export default {
  // ── Dati profilo (camelCase) ──────────────────────────────────────────────
  companyName: "",
  senderName: "",
  vatNumber: "", // P.IVA
  taxCode: "", // Codice Fiscale
  address: "", // sede legale (multilinea)
  iban: "",
  paymentTerms: "", // condizioni di pagamento
  notes: "", // note / termini
  vatEnabled: VAT_ENABLED,
  vatRate: VAT_RATE, // frazione (0.22)
  validityDays: QUOTE_VALIDITY_DAYS,
  logoDataUrl: "", // data:image/...;base64,...

  // ── Stato UI ──────────────────────────────────────────────────────────────
  loading: false,
  saving: false,
  savedAt: 0,
  error: null,

  /** Popola lo store dal payload backend (snake_case). */
  applyProfile(p) {
    p = p || {};
    this.companyName = p.company_name || "";
    this.senderName = p.sender_name || "";
    this.vatNumber = p.vat_number || "";
    this.taxCode = p.tax_code || "";
    this.address = p.address || "";
    this.iban = p.iban || "";
    this.paymentTerms = p.payment_terms || "";
    this.notes = p.notes || "";
    this.vatEnabled = p.vat_enabled !== undefined ? !!p.vat_enabled : VAT_ENABLED;
    this.vatRate = p.vat_rate != null ? Number(p.vat_rate) : VAT_RATE;
    this.validityDays = p.validity_days != null ? Number(p.validity_days) : QUOTE_VALIDITY_DAYS;
    this.logoDataUrl = p.logo_data_url || "";
  },

  /** Serializza lo store nel payload backend (snake_case). */
  toPayload() {
    return {
      company_name: this.companyName,
      sender_name: this.senderName,
      vat_number: this.vatNumber,
      tax_code: this.taxCode,
      address: this.address,
      iban: this.iban,
      payment_terms: this.paymentTerms,
      notes: this.notes,
      vat_enabled: !!this.vatEnabled,
      vat_rate: Number(this.vatRate) || 0,
      validity_days: Number(this.validityDays) || 30,
      logo_data_url: this.logoDataUrl || "",
    };
  },
};
