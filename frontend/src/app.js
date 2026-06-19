// src/app.js
// ------------------------------------------------------------------
// Regia dell'applicazione:
//  - importa lo stile, Alpine e i font self-hosted (air-gapped),
//  - registra gli store Alpine,
//  - definisce le azioni orchestrate richiamate dal markup (x-on),
//  - avvia il polling /health.
//
// api.js non tocca il DOM; gli store sono la fonte di verità; ui.js fa gli
// effetti imperativi (autoscroll/dropzone/colori); qui colleghiamo tutto.

import "./style.css";

import Alpine from "alpinejs";

// Font self-hosted (nessun CDN). Pesi usati da UI e terminale.
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";

import * as api from "./api.js";
import * as ui from "./ui.js";
import * as quote from "./quote.js";
import auth from "./stores/auth.js";
import connection from "./stores/connection.js";
import qualify from "./stores/qualify.js";
import ingest from "./stores/ingest.js";
import settings from "./stores/settings.js";
import {
  API_BASE_URL,
  HEALTH_POLL_MS,
  ALLOWED_EXTENSIONS,
  UPLOAD_MAX_BYTES,
  MIN_RAW_TEXT,
} from "./config.js";

// ── Helpers ────────────────────────────────────────────────────────────────────
const store = (name) => Alpine.store(name);
const safeParse = (s) => {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
};

// ── Azioni orchestrate (esposte come window.App per il markup x-on) ─────────────
const App = {
  // ── Auth ─────────────────────────────────────────────────────────────────────

  /**
   * POST /token con lo username; salva il JWT e sblocca l'interfaccia.
   * Lo username diventa il tenant_id (claim "sub") per tutta la sessione.
   */
  async login(username) {
    const a = store("auth");
    if (!username || !username.trim()) return;
    a.isLoggingIn = true;
    a.loginError = null;
    try {
      const token = await api.login(username.trim());
      a.setToken(token);
      // Sincronizza il connection store così il profilo e i display legacy
      // che leggono $store.connection.tenantId continuano a funzionare.
      const conn = store("connection");
      conn.tenantId = a.tenantId;
      conn.tenants = [a.tenantId];
    } catch (e) {
      a.loginError = e.message;
    } finally {
      a.isLoggingIn = false;
    }
  },

  /**
   * Logout: svuota il token e resetta gli store di lavoro.
   * Chiamato dal bottone Logout e dall'handler dell'evento "auth:unauthorized".
   */
  logout() {
    store("auth").clear();
    store("qualify").reset?.();
    store("ingest").reset?.();
  },

  // ── FASE 1: connessione ──────────────────────────────────────────────────────
  startHealthPolling() {
    const tick = async () => {
      store("connection").apiConnected = await api.checkHealth();
    };
    tick();
    setInterval(tick, HEALTH_POLL_MS);
  },

  // ── FASE 2: qualificazione lead ──────────────────────────────────────────────
  async generateQuote() {
    const s = store("qualify");
    const token = store("auth").token;
    // Validation is handled by canSubmit, but guard here too.
    if (s.inputText.trim().length < MIN_RAW_TEXT) {
      s.errorDetail = `Inserisci almeno ${MIN_RAW_TEXT} caratteri.`;
      return;
    }
    // V2: delegate entirely to the store, which manages status + Poller lifecycle.
    await s.submitLead(token);
  },

  // ── Preventivo condivisibile (email / PDF) ───────────────────────────────────
  _quoteCtx() {
    const q = store("qualify");
    const recipient = {
      name: q.recipientName || "",
      company: q.recipientCompany || "",
      email: q.recipientEmail || quote.extractEmail(q.inputText),
    };
    return { result: q.result, settings: store("settings"), recipient };
  },

  /** Copia negli appunti oggetto + corpo dell'email di preventivo. */
  async copyQuoteEmail() {
    const { result, settings, recipient } = App._quoteCtx();
    if (!result) return false;
    const { subject, body } = quote.buildEmailQuote(result, settings, recipient);
    const text = `Oggetto: ${subject}\n\n${body}`;
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false; // clipboard non disponibile (es. contesto non sicuro)
    }
  },

  /** Apre il client email con destinatario/oggetto/corpo precompilati. */
  openQuoteMail() {
    const { result, settings, recipient } = App._quoteCtx();
    if (!result) return;
    const { subject, body } = quote.buildEmailQuote(result, settings, recipient);
    window.location.href = quote.buildMailto(recipient.email, subject, body);
  },

  /** Genera e scarica il PDF del preventivo (jsPDF, import lazy). */
  async downloadQuotePdf() {
    const { result, settings, recipient } = App._quoteCtx();
    if (!result) return;
    try {
      await quote.generatePdf(result, settings, recipient);
    } catch (e) {
      console.error("[downloadQuotePdf]", e);
      store("qualify").error = "Generazione PDF non riuscita: " + e.message;
    }
  },

  // ── Profilo azienda per-tenant ───────────────────────────────────────────────
  /** Carica il profilo del tenant dal backend nello store settings. */
  async loadProfile(tenantId) {
    const st = store("settings");
    st.loading = true;
    st.error = null;
    try {
      const p = await api.getTenantProfile(tenantId);
      st.applyProfile(p);
    } catch (e) {
      st.error = "Profilo non caricato: " + e.message;
    } finally {
      st.loading = false;
    }
  },

  /** Salva il profilo corrente sul backend per il tenant attivo. */
  async saveProfile() {
    const tenantId = store("auth").tenantId || store("connection").tenantId;
    const st = store("settings");
    st.saving = true;
    st.error = null;
    try {
      const saved = await api.saveTenantProfile(tenantId, st.toPayload());
      st.applyProfile(saved);
      st.savedAt = Date.now();
    } catch (e) {
      st.error = "Salvataggio non riuscito: " + e.message;
    } finally {
      st.saving = false;
    }
  },

  /** Legge il file logo, lo ridimensiona via canvas e lo salva come data URL. */
  onLogoPicked(event) {
    const input = event.target;
    const file = input.files && input.files[0];
    input.value = "";
    if (!file) return;
    const st = store("settings");
    if (!file.type.startsWith("image/")) {
      st.error = "Il logo deve essere un'immagine (PNG/JPG).";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const maxW = 320; // cap larghezza per contenere il base64
        const scale = Math.min(1, maxW / img.width);
        const w = Math.round(img.width * scale);
        const h = Math.round(img.height * scale);
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        st.logoDataUrl = canvas.toDataURL("image/png");
      };
      img.onerror = () => (st.error = "Immagine non valida.");
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  },

  /** Rimuove il logo dal profilo (da salvare poi con Salva). */
  removeLogo() {
    store("settings").logoDataUrl = "";
  },

  // ── FASE 3: onboarding cataloghi (HITL) ──────────────────────────────────────
  initDropzone(el) {
    ui.initDropzone(el, (files) => App.handleFiles(files));
  },

  onFilePicked(event) {
    const input = event.target;
    if (input.files && input.files.length) App.handleFiles(input.files);
    input.value = ""; // permette di ri-selezionare lo stesso file
  },

  handleFiles(files) {
    const s = store("ingest");
    const file = files[0];
    if (!file) return;

    const ext = ui.fileExtension(file.name);
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      s.error = `Estensione non supportata: .${ext || "∅"}. Usa csv, json o xlsx.`;
      s.phase = "error";
      return;
    }
    if (file.size === 0) {
      s.error = "Il file è vuoto.";
      s.phase = "error";
      return;
    }
    if (file.size > UPLOAD_MAX_BYTES) {
      s.error = `File troppo grande (max ${ui.formatBytes(UPLOAD_MAX_BYTES)}).`;
      s.phase = "error";
      return;
    }

    s.error = null;
    s.file = file;
    s.fileName = file.name;
    s.fileSize = file.size;
    s.fileFormat = ext;
    App.uploadAndIngest(file);
  },

  async uploadAndIngest(file) {
    const conn = store("connection");
    const s = store("ingest");
    s.phase = "uploading";
    s.error = null;
    s.logs = [];
    s.reviewPayload = null;
    s.result = null;
    try {
      const { object_key, file_format } = await api.uploadCatalogue(file);
      s.filePath = object_key;
      s.fileFormat = file_format;
      await App.startIngestion({ object_key, file_format });
    } catch (e) {
      s.error = e.message;
      s.phase = "error";
    }
  },

  async startIngestion(payload) {
    const s = store("ingest");
    s.phase = "processing";
    s.logs = [];
    s.reviewPayload = null;
    s.result = null;
    s.error = null;
    try {
      const { threadId } = await api.ingestStream(payload, {
        onMeta: ({ threadId }) => {
          if (threadId) s.threadId = threadId;
        },
        onEvent: (f) => {
          if (f.event === "log") {
            s.logs.push(f.data);
          } else if (f.event === "interrupt") {
            const d = safeParse(f.data) || {};
            if (d.thread_id) s.threadId = d.thread_id;
            s.reviewPayload = d.review_payload || {};
            s.phase = "review";
          } else if (f.event === "done") {
            const d = safeParse(f.data) || {};
            if (d.thread_id) s.threadId = d.thread_id;
            s.result = d;
            s.phase = "done";
          } else if (f.event === "error") {
            s.error = safeParse(f.data)?.error || f.data || "Errore ingestion.";
            s.phase = "error";
          }
        },
      });
      if (threadId && !s.threadId) s.threadId = threadId;
    } catch (e) {
      s.error = e.message;
      s.phase = "error";
    }
  },

  // Approva → scrive su ChromaDB (FinalizeNode) e chiude con riepilogo.
  async approve() {
    const s = store("ingest");
    if (!s.threadId) {
      s.error = "thread_id mancante: impossibile inviare la decisione.";
      s.phase = "error";
      return;
    }
    s.phase = "processing";
    s.error = null;
    try {
      const resp = await api.approveIngestion(s.threadId, {
        approved: true,
        feedback: s.feedback || null,
      });
      s.result = resp;
      s.phase = "done";
    } catch (e) {
      s.error = e.message;
      s.phase = "error";
    }
  },

  // Rifiuto secco → chiude la run (routing a END), nessun re-processing.
  async reject() {
    const s = store("ingest");
    if (!s.threadId) {
      s.error = "thread_id mancante: impossibile inviare la decisione.";
      s.phase = "error";
      return;
    }
    s.phase = "processing";
    s.error = null;
    try {
      const resp = await api.approveIngestion(s.threadId, {
        approved: false,
        feedback: s.feedback || null,
      });
      s.result = resp;
      s.phase = "done";
    } catch (e) {
      s.error = e.message;
      s.phase = "error";
    }
  },

  // Correzione (flusso a due passi, §0.2 punto 4):
  //  1) /approve approved:false → chiude la run sospesa,
  //  2) nuova /ingest/stream con review_feedback sullo stesso file.
  async retryWithFeedback() {
    const conn = store("connection");
    const s = store("ingest");
    const fb = (s.feedback || "").trim();
    if (!fb) {
      s.error = "Inserisci un feedback per la correzione.";
      return;
    }
    if (!s.filePath || !s.fileFormat) {
      s.error = "File originale non disponibile per il re-processing.";
      s.phase = "error";
      return;
    }
    s.phase = "processing";
    s.error = null;
    // Passo 1: chiudi la run sospesa (best-effort: se 404 proseguiamo).
    try {
      if (s.threadId) {
        await api.approveIngestion(s.threadId, { approved: false, feedback: fb });
      }
    } catch (e) {
      console.warn("[retryWithFeedback] approve(false) ignorato:", e.message);
    }
    // Passo 2: nuova ingestion con review_feedback.
    const feedbackToSend = fb;
    s.feedback = "";
    await App.startIngestion({
      object_key: s.filePath,
      file_format: s.fileFormat,
      review_feedback: feedbackToSend,
    });
  },

  resetIngest() {
    store("ingest").reset();
  },

  async wipeCatalogue() {
    const tenantId =
      Alpine.store("auth").tenantId || Alpine.store("connection").tenantId;

    const confirmed = window.confirm(
      `Sei sicuro di voler eliminare tutti i dati del catalogo per "${tenantId}"?\n\nQuesta operazione è irreversibile. Dopo il reset potrai caricare un nuovo catalogo.`
    );
    if (!confirmed) return;

    const s = store("ingest");
    s.phase = "uploading"; // riusa lo stato di caricamento come "in progress"

    try {
      const result = await api.wipeVectorData(tenantId);
      s.phase = "idle";
      const msg = result.dropped
        ? `✓ Catalogo resettato. Puoi caricare il nuovo file.`
        : `Nessun catalogo trovato per "${tenantId}". Puoi caricare un nuovo file.`;
      window.alert(msg);
    } catch (err) {
      s.phase = "idle";
      window.alert(`Errore durante il reset: ${err.message}`);
    }
  },
};

// ── Registrazione store ─────────────────────────────────────────────────────────
Alpine.store("auth", auth);
Alpine.store("connection", connection);
Alpine.store("qualify", qualify);
Alpine.store("ingest", ingest);
Alpine.store("settings", settings);

// ── Esposizione globale per il markup (x-on / x-effect / x-init) ────────────────
window.Alpine = Alpine;
window.ui = ui;
window.App = App;
window.quote = quote;
window.APP_CONFIG = {
  apiBaseUrl: API_BASE_URL,
  allowedExtensions: ALLOWED_EXTENSIONS,
  uploadMaxBytes: UPLOAD_MAX_BYTES,
};

// ── Avvio ────────────────────────────────────────────────────────────────────────
Alpine.start();
App.startHealthPolling();

// Carica il profilo del tenant autenticato e lo ricarica se cambia sessione.
Alpine.effect(() => {
  const tenantId = Alpine.store("auth").tenantId;
  if (tenantId) App.loadProfile(tenantId);
});

// Listener globale per il logout forzato da 401: resetta la sessione
// e riporta l'utente alla login screen senza bisogno di refresh pagina.
window.addEventListener("auth:unauthorized", () => {
  App.logout();
});
