// src/stores/ingest.js
// ------------------------------------------------------------------
// Store Alpine: stato della vista "Onboarding Cataloghi" + Human-in-the-Loop.
// phase guida quale schermata mostrare.

export default {
  // idle | uploading | processing | review | done | error
  phase: "idle",
  file: null, // File selezionato dalla dropzone
  fileName: "", // nome leggibile del file
  fileSize: 0, // dimensione (byte) per la UI
  filePath: null, // ritornato da POST /upload
  fileFormat: null, // "csv" | "json" | "xlsx"
  threadId: null, // catturato da X-Thread-Id e/o dal payload interrupt/done
  logs: [],
  reviewPayload: null, // { flagged_items, confidence_score, … } da event: interrupt
  result: null, // ApprovalResponse da /approve, o payload "done"
  feedback: "", // testo per rifiuto/correzione
  error: null,
  isDragging: false,

  /** true se siamo in una fase che mostra la console (processing/review/done/error con log). */
  get hasLogs() {
    return this.logs.length > 0;
  },

  /** Reset completo per ricominciare un nuovo onboarding. */
  reset() {
    this.phase = "idle";
    this.file = null;
    this.fileName = "";
    this.fileSize = 0;
    this.filePath = null;
    this.fileFormat = null;
    this.threadId = null;
    this.logs = [];
    this.reviewPayload = null;
    this.result = null;
    this.feedback = "";
    this.error = null;
    this.isDragging = false;
  },
};
