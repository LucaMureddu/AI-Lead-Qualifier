// src/stores/qualify.js — V2
// ------------------------------------------------------------------
// Alpine store: state for the "Lead Qualification" view.
//
// V2 changes vs V1
// ----------------
// - REMOVED: logs (SSE-based), sseActive, sseReader
// - ADDED:   threadId, pollingActive, status (polling lifecycle)
// - result and error updated to match V2 LeadStatusResponse shape
// - Poller lifecycle managed here (start on submit, auto-stop on terminal status)

import { MIN_RAW_TEXT } from "../config.js";
import { submitLead, Poller } from "../api.js";

export default {
  // ── Input ────────────────────────────────────────────────────────────────
  inputText: "",       // bound to the textarea (x-model)

  // ── Polling state (V2) ────────────────────────────────────────────────────
  threadId: null,      // set after POST /lead 202
  pollingActive: false,
  status: "idle",      // idle | queued | processing | pending_review | completed | error

  // ── Result & error ────────────────────────────────────────────────────────
  result: null,        // { total_quote, mapped_services, on_request_services } on completed
                       // { review_payload: { confidence_score, extracted_services, ... } } on pending_review
  errorDetail: null,

  // ── Recipient (for PDF/email) ─────────────────────────────────────────────
  recipientName: "",
  recipientCompany: "",
  recipientEmail: "",

  // ── Computed ──────────────────────────────────────────────────────────────

  get isLoading() {
    return ["queued", "processing"].includes(this.status);
  },

  get canSubmit() {
    return this.inputText.trim().length >= MIN_RAW_TEXT && !this.isLoading;
  },

  get charCount() {
    return this.inputText.trim().length;
  },

  // ── Actions ───────────────────────────────────────────────────────────────

  reset() {
    this.threadId = null;
    this.pollingActive = false;
    this.status = "idle";
    this.result = null;
    this.errorDetail = null;
    this.recipientName = "";
    this.recipientCompany = "";
    this.recipientEmail = "";
  },

  /**
   * Submit a lead for qualification.
   * Enqueues the job (POST /lead → 202) and starts polling.
   *
   * @param {string} token  JWT for Authorization header.
   */
  async submitLead(token) {
    this.reset();
    this.status = "queued";

    try {
      const { thread_id } = await submitLead(this.inputText, token);
      this.threadId = thread_id;
      this.pollingActive = true;

      new Poller({
        threadId: thread_id,
        intervalMs: 2500,
        onUpdate: (data) => {
          this.status = data.status;
        },
        onDone: (data) => {
          this.pollingActive = false;
          this.status = data.status;
          this.result = data.result;
          this.errorDetail = data.error_detail ?? null;
        },
        onError: (err) => {
          this.pollingActive = false;
          this.status = "error";
          this.errorDetail = err.message;
          console.error("[qualify] polling error", err);
        },
      }).start();
    } catch (err) {
      this.status = "error";
      this.errorDetail = err.message;
    }
  },
};
