// src/api.js — V2
// ------------------------------------------------------------------
// Communication layer with the backend.
//
// V2 changes vs V1
// ----------------
// - SSE (streamSSE, qualifyStream) REMOVED from lead qualification
// - NEW: submitLead (POST /lead → 202), getLeadStatus (GET /status/{id}),
//   approveLead (POST /lead/{id}/approve)
// - NEW: Poller class — replaces SSE ReadableStream with interval polling
// - JWT: now RS256 (same localStorage key, transparent to this layer)
// - Kept: ingestStream (SSE for catalogue ingestion), uploadCatalogue,
//         approveIngestion, wipeVectorData, checkHealth, profile endpoints

import { API_BASE_URL } from "./config.js";

const BASE = API_BASE_URL;
const TOKEN_KEY = "jwt_token";

// ── Auth helpers ──────────────────────────────────────────────────────────────

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function authHeaders(extra = {}) {
  const token = getToken();
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
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

// ── Lead qualification — V2 async polling (no SSE) ───────────────────────────

/**
 * POST /lead — enqueue a lead job, returns { thread_id, status: "queued" }.
 * @param {string} rawText
 * @param {string} token
 * @param {string|null} leadId  Optional external identifier.
 */
export async function submitLead(rawText, token, leadId = null) {
  const res = await fetch(`${BASE}/lead`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
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
 * @param {string} threadId
 * @returns {Promise<{thread_id, status, result, error_detail}>}
 */
export async function getLeadStatus(threadId) {
  const res = await fetch(`${BASE}/status/${encodeURIComponent(threadId)}`, {
    headers: authHeaders(),
    cache: "no-store",
  });
  if (res.status === 401) { handle401(); throw new Error("Sessione scaduta."); }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = j.detail; } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

/**
 * POST /lead/{threadId}/approve — approve or reject a pending_review job.
 * @param {string} threadId
 * @param {boolean} approved
 * @param {string|null} feedback
 * @returns {Promise<{thread_id, status}>}
 */
export async function approveLead(threadId, approved, feedback = null) {
  const res = await fetch(`${BASE}/lead/${encodeURIComponent(threadId)}/approve`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
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
 * Features:
 * - No-overlap: skips a tick if the previous request is still in flight.
 * - Auto-stops when status reaches a terminal state (completed|error|pending_review).
 * - Exposes start() / stop() for lifecycle management.
 *
 * @example
 *   new Poller({
 *     threadId,
 *     intervalMs: 2500,
 *     onUpdate: (data) => { store.status = data.status; },
 *     onDone:   (data) => { store.result = data.result; },
 *     onError:  (err)  => { console.error(err); },
 *   }).start();
 */
export class Poller {
  constructor({ threadId, intervalMs = 2500, onUpdate, onDone, onError }) {
    this.threadId = threadId;
    this.intervalMs = intervalMs;
    this.onUpdate = onUpdate;
    this.onDone = onDone;
    this.onError = onError;
    this._timer = null;
    this._inflight = false;
  }

  start() {
    this._tick(); // immediate first poll
    this._timer = setInterval(() => this._tick(), this.intervalMs);
    return this;
  }

  stop() {
    clearInterval(this._timer);
    this._timer = null;
  }

  async _tick() {
    if (this._inflight) return; // no-overlap guard
    this._inflight = true;
    try {
      const data = await getLeadStatus(this.threadId);
      this.onUpdate?.(data);
      if (["completed", "error", "pending_review"].includes(data.status)) {
        this.stop();
        this.onDone?.(data);
      }
    } catch (err) {
      this.stop();
      this.onError?.(err);
    } finally {
      this._inflight = false;
    }
  }
}

// ── Catalogue ingestion (SSE kept for ingest flow) ────────────────────────────

function _formatSseHelper(data, { onEvent, onMeta, signal } = {}) {
  // shared SSE stream consumer (used by ingestStream only in V2)
  return { data, onEvent, onMeta, signal };
}

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
    headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
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
    headers: authHeaders({ "Content-Type": "application/json" }),
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
    headers: authHeaders({ "Content-Type": "application/json" }),
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

/** GET /health */
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
    headers: authHeaders({ "Content-Type": "application/json" }),
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
