// src/config.js
// ------------------------------------------------------------------
// Costanti dell'app e configurazione lato client.
// Unica fonte per: URL base API, lista tenant nota, soglie di validazione.

/** URL base del backend FastAPI. Vuoto ("") ⇒ same-origin (proxy Vite).
 *  Usa || (non ??) così una stringa vuota cade comunque sul default,
 *  evitando che Nginx riceva richieste POST dirette e risponda 405. */
export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

/**
 * Lista iniziale dei tenant noti (B2 — dropdown lato client).
 * Nuovi tenant si aggiungono a runtime dall'header (input a testo libero),
 * abilitando l'onboarding dalla UI senza toccare il backend.
 */
export const INITIAL_TENANTS = ["cliente_acme_01", "cliente_beta_02"];

/** Tenant attivo di default all'avvio. */
export const DEFAULT_TENANT = "cliente_acme_01";

/** Intervallo di polling del badge "API Connected" (ms). */
export const HEALTH_POLL_MS = 15000;

/** Estensioni catalogo ammesse (coerenti col backend /upload). */
export const ALLOWED_EXTENSIONS = ["csv", "json", "xlsx"];

/** Dimensione massima upload (10 MB) — coerente con upload_max_bytes lato backend. */
export const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;

/** Lunghezza minima del testo lead (vincolo backend: raw_text min_length=10). */
export const MIN_RAW_TEXT = 10;

// ── Preventivo condivisibile (email / PDF) ──────────────────────────────────────
/** Dati mittente di default (modificabili in Impostazioni, persistiti in localStorage). */
export const DEFAULT_COMPANY = "La Tua Azienda S.r.l.";
export const DEFAULT_SENDER = "Ufficio Commerciale";
/** IVA: abilitata di default, aliquota 22% (frazione). Configurabile in Impostazioni. */
export const VAT_ENABLED = true;
export const VAT_RATE = 0.22;
/** Validità dell'offerta in giorni. */
export const QUOTE_VALIDITY_DAYS = 30;
