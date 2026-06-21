// src/api.js — V3
// ------------------------------------------------------------------
// Communication layer with the backend.
//
// V3 changes vs V2
// ----------------
// - Poller constructor now accepts explicit `token` param (falls back to
//   localStorage if omitted — zero breaking change for existing callers)
// - getLeadStatus signature updated to accept optional token
// - patchCatalogueItem updated: accepts price_type field (V3 hybrid pricing)
//
// V2 changes vs V1
// ----------------
// - SSE (streamSSE, qualifyStream) REMOVED from lead qualification
// - Poller class — replaces SSE ReadableStream with setInterval + fetch
// - Kept: ingestStream (SSE for catalogue ingestion), all other endpoints

import { API_BASE_URL } from "./config.js";

const BASE = API_BASE_URL;
const TOKEN_KEY = "jwt_token";

// ── Auth helpers ──────────────────────────────────────────────────────────────

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

/**
 * Build Authorization + optional extra headers.
 * Accepts an explicit token; falls back to localStorage for callers that
 * do not thread the token through (e.g. background Poller ticks).
 * @param {string|null} [token]
 * @param {Object} [extra]
 */
function authHeaders(token = null, extra = {}) {
  const tok = token ?? getToken();
  return {
    ...(tok ? { Authorization: `Bearer ${tok}` } : {}),
    ...extra,
  };
}

function handle401() {
  window.dispatchEvent(new CustomEvent("auth:unauthorized"));
}

// ── Auth endpoint ─────────────────────────────────────────────────────────────

/**
 * POST /token — mock auth (dev only; production uses external IdP with RS256).
 * @param {string} username  Used as tenant_id claim.
 * @returns {Promise<string>} Raw JWT.
 */
export async function login(username) {
  const res = await fetch(`${BASE}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
  });
  if (!res.ok) {
    let detail = `Login fallito: HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  const { access_token } = await res.json();
  return access_token;
}

// ── Lead qualification — async polling (no SSE) ───────────────────────────────

/**
 * POST /lead — enqueue a lead job.
 * @param {string} rawText
 * @param {string|null} [token]  JWT; falls back to localStorage.
 * @param {string|null} [leadId] Optional external CRM identifier.
 * @returns {Promise<{ thread_id: string, status: "queued" }>}
 */
export async function submitLead(rawText, token = null, leadId = null) {
  const res = await fetch(`${BASE}/lead`, {
    method: "POST",
    headers: authHeaders(token, { "Content-Type": "application/json" }),
    body: JSON.stringify({ raw_text: rawText, lead_id: leadId }),
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json(); // { thread_id, status: "queued" }
}

/**
 * GET /status/{threadId} — poll the current state of a lead job.
 *
 * Security: threadId is URL-encoded; the raw lead payload is never logged.
 * @param {string} threadId
 * @param {string|null} [token]  JWT; falls back to localStorage.
 * @returns {Promise<{ thread_id: string, status: string, result: Object|null, error_detail: string|null }>}
 */
export async function getLeadStatus(threadId, token = null) {
  const res = await fetch(`${BASE}/status/${encodeURIComponent(threadId)}`, {
    headers: authHeaders(token),
    cache: "no-store",
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(`[${threadId}] ${detail}`);
  }
  return res.json();
}

/**
 * POST /lead/{threadId}/approve — approve or reject a pending_review job.
 * @param {string} threadId
 * @param {boolean} approved
 * @param {string|null} [feedback]
 * @returns {Promise<{ thread_id: string, status: string }>}
 */
export async function approveLead(threadId, approved, feedback = null) {
  const res = await fetch(`${BASE}/lead/${encodeURIComponent(threadId)}/approve`, {
    method: "POST",
    headers: authHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ approved, feedback: feedback || null }),
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

// ── Poller class ──────────────────────────────────────────────────────────────

/**
 * Polls GET /status/{threadId} at a fixed interval.
 *
 * Contract:
 * - No-overlap: if a tick is still in flight when the next interval fires,
 *   the new tick is skipped entirely. This prevents request pile-up on slow
 *   networks without cancellation complexity.
 * - Auto-stop: stops itself when status reaches a LangGraph terminal state
 *   ("completed" | "error" | "pending_review"). The caller does not need to
 *   track state externally.
 * - Security: never logs the lead payload; only threadId and HTTP status codes
 *   appear in console output.
 *
 * @example
 *   const poller = new Poller({
 *     threadId,
 *     token,                          // optional, falls back to localStorage
 *     intervalMs: 2500,
 *     onUpdate: (data) => { store.status = data.status; },
 *     onDone:   (data) => { store.result = data.result; store.pollingActive = false; },
 *     onError:  (err)  => { console.error("[Poller]", err.message); store.pollingActive = false; },
 *   });
 *   poller.start();
 *   // later: poller.stop() if the user navigates away
 */
export class Poller {
  /** Terminal states that cause the poller to stop automatically. */
  static TERMINAL = new Set(["completed", "error", "pending_review"]);

  /**
   * @param {Object} opts
   * @param {string}        opts.threadId     ARQ job thread ID.
   * @param {string|null}   [opts.token]      JWT Bearer token; falls back to localStorage.
   * @param {number}        [opts.intervalMs] Poll interval in ms (default 2500).
   * @param {Function}      [opts.onUpdate]   Called on every non-terminal tick with full response.
   * @param {Function}      [opts.onDone]     Called once when a terminal state is reached.
   * @param {Function}      [opts.onError]    Called on network/HTTP error; poller stops.
   */
  constructor({ threadId, token = null, intervalMs = 2500, onUpdate, onDone, onError }) {
    this.threadId  = threadId;
    this.token     = token;
    this.intervalMs = intervalMs;
    this.onUpdate  = onUpdate;
    this.onDone    = onDone;
    this.onError   = onError;

    /** @type {number|null} setInterval handle */
    this._timer    = null;
    /** Guards against concurrent in-flight requests (no-overlap invariant). */
    this._inflight = false;
  }

  /**
   * Start polling. Fires an immediate first tick, then repeats at intervalMs.
   * @returns {this} Chainable.
   */
  start() {
    if (this._timer !== null) return this; // idempotent
    this._tick();
    this._timer = setInterval(() => this._tick(), this.intervalMs);
    return this;
  }

  /** Stop polling and release the interval. Idempotent. */
  stop() {
    if (this._timer === null) return;
    clearInterval(this._timer);
    this._timer = null;
  }

  /** @private */
  async _tick() {
    if (this._inflight) return; // no-overlap guard — skip this tick
    this._inflight = true;
    try {
      const data = await getLeadStatus(this.threadId, this.token);
      this.onUpdate?.(data);
      if (Poller.TERMINAL.has(data.status)) {
        this.stop();
        this.onDone?.(data);
      }
    } catch (err) {
      console.error(`[Poller] thread=${this.threadId} error:`, err.message);
      this.stop();
      this.onError?.(err);
    } finally {
      this._inflight = false;
    }
  }
}

// ── Catalogue ingestion (SSE kept — ingest flow only) ─────────────────────────

async function _consumeSSE(res, { onEvent, onMeta } = {}) {
  const threadId = res.headers.get("X-Thread-Id");
  onMeta?.({ threadId });

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const rawFrame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const frame = _parseFrame(rawFrame);
      if (frame.data !== "" || frame.event !== "message") onEvent?.(frame);
    }
  }

  if (buffer.trim() !== "") {
    const frame = _parseFrame(buffer);
    if (frame.data !== "") onEvent?.(frame);
  }

  return { threadId };
}

function _parseFrame(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  return { event, data: dataLines.join("\n") };
}

/**
 * POST /ingest/stream — SSE stream for catalogue ingestion.
 * Events: log, interrupt, done, error.
 */
export async function ingestStream(payload, { onEvent, onMeta, signal } = {}) {
  const res = await fetch(`${BASE}/ingest/stream`, {
    method: "POST",
    headers: authHeaders(null, { "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify(payload),
    signal,
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return _consumeSSE(res, { onEvent, onMeta });
}

/**
 * POST /upload — upload a catalogue file, returns { object_key, file_format }.
 */
export async function uploadCatalogue(file) {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/upload`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `Upload fallito: HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

/**
 * POST /ingest/{threadId}/approve — resume a suspended ingestion run.
 */
export async function approveIngestion(threadId, decision) {
  const res = await fetch(`${BASE}/ingest/${encodeURIComponent(threadId)}/approve`, {
    method: "POST",
    headers: authHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify(decision),
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (res.status === 404) throw new Error("Nessuna run sospesa per questo thread_id (404).");
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

/**
 * DELETE /api/v1/tenants/{tenantId}/vector-data — pgvector hard reset.
 */
export async function wipeVectorData(tenantId) {
  const res = await fetch(`${BASE}/api/v1/tenants/${encodeURIComponent(tenantId)}/vector-data`, {
    method: "DELETE",
    headers: authHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify({ confirm_wipe: true }),
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

/** GET /health — returns true if both Postgres and Redis are reachable. */
export async function checkHealth() {
  try {
    const res = await fetch(`${BASE}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

// ── Tenant profile ────────────────────────────────────────────────────────────

export async function getTenantProfile(tenantId) {
  const res = await fetch(`${BASE}/tenants/${encodeURIComponent(tenantId)}/profile`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function saveTenantProfile(tenantId, profile) {
  const res = await fetch(`${BASE}/tenants/${encodeURIComponent(tenantId)}/profile`, {
    method: "PUT",
    headers: authHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify(profile),
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

// ── Catalogue admin ───────────────────────────────────────────────────────────

/**
 * GET /api/catalog/items?skip=…&limit=…
 * @returns {Promise<{ items: Array, total: number, skip: number, limit: number }>}
 */
export async function listCatalogueItems(skip = 0, limit = 20) {
  const url = `${BASE}/api/catalog/items?skip=${skip}&limit=${limit}`;
  const res = await fetch(url, { cache: "no-store", headers: authHeaders() });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) throw new Error(`Errore caricamento catalogo: HTTP ${res.status}`);
  return res.json();
}

/**
 * PATCH /api/catalog/items/{item_id} — partial update (V3 hybrid pricing).
 *
 * Accepted fields:
 *   service      {string}             rename the service
 *   price        {number|null}        null when price_type = "VARIABLE"
 *   price_type   {"FIXED"|"FREE"|"VARIABLE"}
 *   description  {string|null}
 *
 * Invariant enforced by the DB CHECK constraint:
 *   FIXED    → price must be non-null and ≥ 0
 *   FREE     → price is coerced to 0.0
 *   VARIABLE → price is coerced to null
 *
 * HTTP 422 is returned for incoherent combinations (e.g. FIXED + price=null).
 *
 * @param {string} itemId
 * @param {{ service?: string, price?: number|null, price_type?: string, description?: string|null }} patch
 * @returns {Promise<{ id: string, service: string, price: number|null, price_type: string,
 *                     description: string|null, embedding_sync: string }>}
 */
export async function patchCatalogueItem(itemId, patch) {
  const res = await fetch(`${BASE}/api/catalog/items/${encodeURIComponent(itemId)}`, {
    method: "PATCH",
    headers: authHeaders(null, { "Content-Type": "application/json" }),
    body: JSON.stringify(patch),
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = "";
    try { const j = await res.json(); if (j?.detail) detail = ": " + j.detail; } catch { /* noop */ }
    throw new Error(`HTTP ${res.status}${detail}`);
  }
  return res.json();
}
