// src/stores/ingest.js — V2
// ------------------------------------------------------------------
// Alpine store: state for "Catalogue Onboarding" + HITL approval.
//
// V2 changes vs V1
// ----------------
// - Ingest still uses SSE (ingestStream) — unchanged
// - HITL approve: now calls approveLead (POST /lead/{id}/approve) for
//   lead qualification HITL; approveIngestion remains for catalogue HITL
// - phase "pending_review" now also covers lead qualification HITL
// - Added: approveLeadHITL / rejectLeadHITL helpers for lead qualify flow
// - Polling state for lead approval resume

import { uploadCatalogue, ingestStream, approveIngestion, approveLead } from "../api.js";

export default {
  // idle | uploading | processing | review | done | error
  phase: "idle",

  // ── File state ────────────────────────────────────────────────────────────
  file: null,
  fileName: "",
  fileSize: 0,
  filePath: null,
  fileFormat: null,

  // ── Thread / polling ──────────────────────────────────────────────────────
  threadId: null,       // from X-Thread-Id or interrupt payload
  logs: [],
  reviewPayload: null,  // { flagged_items, confidence_score, ... } from interrupt event
  result: null,
  feedback: "",
  error: null,
  isDragging: false,

  // ── Computed ──────────────────────────────────────────────────────────────

  get hasLogs() {
    return this.logs.length > 0;
  },

  // ── Actions ───────────────────────────────────────────────────────────────

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

  /**
   * Upload a catalogue file and start the ingestion SSE stream.
   */
  async uploadAndIngest(token) {
    if (!this.file) return;
    this.phase = "uploading";
    this.logs = [];
    this.error = null;

    try {
      // 1. Upload file → get server path
      const { file_path, file_format } = await uploadCatalogue(this.file);
      this.filePath = file_path;
      this.fileFormat = file_format;

      // 2. Start ingestion SSE stream
      this.phase = "processing";
      await ingestStream(
        { file_path, file_format, review_feedback: null },
        {
          onMeta: ({ threadId }) => { if (threadId) this.threadId = threadId; },
          onEvent: (frame) => {
            if (frame.event === "log") {
              this.logs.push(frame.data);
            } else if (frame.event === "interrupt") {
              const payload = JSON.parse(frame.data);
              this.threadId = payload.thread_id ?? this.threadId;
              this.reviewPayload = payload.review_payload;
              this.phase = "review";
            } else if (frame.event === "done") {
              const payload = JSON.parse(frame.data);
              this.result = payload;
              this.phase = "done";
            } else if (frame.event === "error") {
              const payload = JSON.parse(frame.data);
              this.error = payload.error ?? "Errore sconosciuto.";
              this.phase = "error";
            }
          },
        }
      );
    } catch (err) {
      this.error = err.message;
      this.phase = "error";
    }
  },

  /**
   * Approve a suspended catalogue ingestion run.
   */
  async approve(token) {
    if (!this.threadId) return;
    try {
      const result = await approveIngestion(this.threadId, {
        approved: true,
        feedback: this.feedback || null,
      });
      this.result = result;
      this.phase = "done";
    } catch (err) {
      this.error = err.message;
      this.phase = "error";
    }
  },

  /**
   * Reject a suspended catalogue ingestion run.
   */
  async reject(token) {
    if (!this.threadId) return;
    try {
      const result = await approveIngestion(this.threadId, {
        approved: false,
        feedback: this.feedback || null,
      });
      this.result = result;
      this.phase = "done";
    } catch (err) {
      this.error = err.message;
      this.phase = "error";
    }
  },

  // ── Lead HITL helpers (used when qualify store enters pending_review) ─────

  /**
   * Approve a pending_review lead qualification job.
   * Called from the qualify view when status == "pending_review".
   *
   * @param {string} threadId  From qualify store.
   * @param {string|null} feedback
   * @returns {Promise<{thread_id, status}>}
   */
  async approveLeadHITL(threadId, feedback = null) {
    return approveLead(threadId, true, feedback);
  },

  /**
   * Reject a pending_review lead qualification job.
   */
  async rejectLeadHITL(threadId, feedback = null) {
    return approveLead(threadId, false, feedback);
  },
};
