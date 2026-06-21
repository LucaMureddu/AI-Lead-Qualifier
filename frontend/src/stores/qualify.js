// src/stores/qualify.js — V3
// ------------------------------------------------------------------
// Alpine store: state for the "Lead Qualification" view.
//
// V3 changes vs V2
// ----------------
// - result shape updated: mapped_services items now carry price_type
//   ("FIXED" | "FREE" | "VARIABLE") instead of the removed is_on_request flag.
//   VARIABLE items have price: null; consumers must branch on price_type before
//   rendering (see quote.js formatPrice helper).
// - _poller ref stored on instance for cleanup on re-submit / unmount.
// - onError logs only err.message (never raw lead payload — security invariant).
//
// V2 changes vs V1
// ----------------
// - REMOVED: logs (SSE-based), sseActive, sseReader
// - ADDED:   threadId, pollingActive, status (polling lifecycle)
// - Poller lifecycle managed here (start on submit, auto-stop on terminal status)
//
// ANTI-PATTERN: zero SSE / EventSource / ReadableStream in this file or api.js
// for the lead qualification flow. Polling is the only transport.

import { MIN_RAW_TEXT } from "../config.js";
import { submitLead as apiSubmitLead, Poller } from "../api.js";

export default {
  // ── Input ─────────────────────────────────────────────────────────────────
  inputText: "",       // bound to the textarea (x-model)

  // ── Polling lifecycle ─────────────────────────────────────────────────────
  threadId: null,      // set after POST /lead → 202
  pollingActive: false,
  status: "idle",      // idle | queued | processing | pending_review | completed | error

  // ── Result & error ────────────────────────────────────────────────────────
  //
  // On "completed":
  //   {
  //     total_quote: number,                  // sum of FIXED + FREE prices
  //     mapped_services: Array<{
  //       matched_name: string,
  //       price:        number | null,         // null when price_type = "VARIABLE"
  //       price_type:   "FIXED"|"FREE"|"VARIABLE",
  //       unit:         string,
  //     }>,
  //     variable_services: string[],           // display names of VARIABLE items
  //   }
  // On "pending_review":
  //   { review_payload: { confidence_score, extracted_services, ... } }
  result: null,
  errorDetail: null,

  // ── Recipient (for PDF / email) ───────────────────────────────────────────
  recipientName: "",
  recipientCompany: "",
  recipientEmail: "",

  // ── Internal Poller handle ────────────────────────────────────────────────
  _poller: null,       // stopped & cleared on reset() and onDone/onError

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

  /** Stop any running poller and wipe all transient state. */
  reset() {
    if (this._poller) {
      this._poller.stop();
      this._poller = null;
    }
    this.threadId     = null;
    this.pollingActive = false;
    this.status       = "idle";
    this.result       = null;
    this.errorDetail  = null;
    this.recipientName    = "";
    this.recipientCompany = "";
    this.recipientEmail   = "";
  },

  /**
   * Submit a lead for qualification.
   *
   * Flow:
   *   1. reset() → status = "queued"
   *   2. POST /lead → 202 { thread_id }
   *   3. Poller polls GET /status/{thread_id} every 2500 ms (no-overlap)
   *   4. onUpdate  → keep status reactive while graph processes nodes
   *   5. onDone    → terminal reached: store result, pollingActive = false
   *   6. onError   → network/HTTP failure: store error message, pollingActive = false
   *
   * @param {string} token  JWT Bearer token from the auth store.
   */
  async submitLead(token) {
    this.reset();
    this.status = "queued";

    try {
      const { thread_id } = await apiSubmitLead(this.inputText, token);
      this.threadId      = thread_id;
      this.pollingActive = true;

      this._poller = new Poller({
        threadId:   thread_id,
        token,
        intervalMs: 2500,

        onUpdate: (data) => {
          this.status = data.status;
        },

        onDone: (data) => {
          this.pollingActive = false;
          this.status        = data.status;
          this.result        = data.result       ?? null;
          this.errorDetail   = data.error_detail ?? null;
          this._poller       = null;
        },

        onError: (err) => {
          this.pollingActive = false;
          this.status        = "error";
          // SECURITY: err.message is "[threadId] HTTP 5xx" or similar — safe.
          // Raw lead payload is never included (enforced in api.js getLeadStatus).
          this.errorDetail   = err.message;
          console.error("[qualify] polling failed:", err.message);
          this._poller       = null;
        },
      }).start();

    } catch (err) {
      // POST /lead itself failed (network down, 401, 422, …)
      this.status      = "error";
      this.errorDetail = err.message;
    }
  },
};
