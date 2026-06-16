// src/stores/qualify.js
// ------------------------------------------------------------------
// Store Alpine: stato della vista "Qualificazione Lead".
// Unica fonte di verità reattiva per input, log SSE, risultato ed errori.

import { MIN_RAW_TEXT } from "../config.js";

export default {
  inputText: "", // testo del lead (x-model sulla textarea)
  isLoading: false,
  logs: [], // righe SSE (event: log)
  result: null, // { lead_id, total_quote, mapped_services, error } da event: done
  error: null,

  // Destinatario del preventivo (per email/PDF). L'email viene precompilata
  // dal testo del lead; nome e azienda sono editabili al volo.
  recipientName: "",
  recipientCompany: "",
  recipientEmail: "",

  /** true se il testo soddisfa il vincolo backend (>= 10 char) e non è in corso una run. */
  get canSubmit() {
    return this.inputText.trim().length >= MIN_RAW_TEXT && !this.isLoading;
  },

  /** Caratteri attuali (per il contatore in UI). */
  get charCount() {
    return this.inputText.trim().length;
  },

  reset() {
    this.logs = [];
    this.result = null;
    this.error = null;
    this.recipientName = "";
    this.recipientCompany = "";
    this.recipientEmail = "";
  },
};
