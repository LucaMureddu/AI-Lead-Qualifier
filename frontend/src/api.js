// src/api.js
// ------------------------------------------------------------------
// Livello di comunicazione col backend. NON tocca mai il DOM:
// ritorna dati / invoca callback su evento (vedi §3 del piano).
//
// Sicurezza JWT (multi-tenant)
// ----------------------------
// Ogni chiamata autenticata include l'header:
//   Authorization: Bearer <token>
// Il token è letto da localStorage tramite getToken().
// Se il backend risponde 401, viene dispatch-ato l'evento DOM
// "auth:unauthorized" che app.js gestisce eseguendo App.logout().
//
// Il tenant_id NON viene più inviato nei body / FormData: il backend
// lo estrae direttamente dal claim "sub" del JWT.

import { API_BASE_URL } from "./config.js";

const BASE = API_BASE_URL;
const TOKEN_KEY = "jwt_token";

// ── Auth helpers ──────────────────────────────────────────────────────────────

/** Legge il token JWT dal localStorage. */
function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

/**
 * Costruisce gli header di autenticazione da fondere con quelli specifici
 * della chiamata. Se non c'è token l'header Authorization è omesso
 * (utile per /health e /token che sono endpoint pubblici).
 *
 * @param {Record<string,string>} extra  Header aggiuntivi (es. Content-Type).
 * @returns {Record<string,string>}
 */
function authHeaders(extra = {}) {
  const token = getToken();
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra,
  };
}

/**
 * Dispatch-a l'evento "auth:unauthorized" sul window.
 * app.js ascolta questo evento e chiama App.logout() → la login screen torna visibile.
 */
function handle401() {
  window.dispatchEvent(new CustomEvent("auth:unauthorized"));
}

// ── Auth endpoint ─────────────────────────────────────────────────────────────

/**
 * POST /token — mock auth.
 * Invia l'username (= tenant_id) e riceve un JWT firmato con la SECRET_KEY.
 *
 * @param {string} username  Sarà il claim "sub" del token.
 * @returns {Promise<string>} Il JWT grezzo (access_token).
 */
export async function login(username) {
  const res = await fetch(`${BASE}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
  });

  if (!res.ok) {
    let detail = `Login fallito: HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch { /* noop */ }
    throw new Error(detail);
  }

  const { access_token } = await res.json();
  return access_token;
}

// ── SSE helpers ───────────────────────────────────────────────────────────────

/**
 * Consuma uno stream SSE da un endpoint POST.
 *
 * Bufferizza i chunk e splitta sui doppi newline ("\n\n"), che il backend usa
 * come delimitatore di frame. Cattura l'header X-Thread-Id (necessario per
 * /approve) e lo restituisce; lo espone anche subito via onMeta, appena gli
 * header arrivano (utile perché lo stream ingest può chiudersi sull'interrupt).
 *
 * Il token JWT è iniettato nell'header Authorization: Bearer <token>.
 * Un 401 dispatch-a "auth:unauthorized" e interrompe lo stream.
 *
 * @param {string} path     es. "/qualify/stream"
 * @param {object} body     payload JSON (senza tenant_id)
 * @param {object} handlers { onEvent(frame), onMeta({threadId}), signal }
 * @returns {Promise<{threadId: string|null}>}
 */
export async function streamSSE(path, body, { onEvent, onMeta, signal } = {}) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: authHeaders({
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    }),
    body: JSON.stringify(body),
    signal,
  });

  if (res.status === 401) {
    handle401();
    throw new Error("Sessione scaduta. Effettua nuovamente il login.");
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch { /* corpo non-JSON: tieni il messaggio HTTP */ }
    throw new Error(detail);
  }

  // NB: cross-origin l'header custom è leggibile solo se il backend lo espone
  // (Access-Control-Expose-Headers). In ogni caso il thread_id arriva anche nel
  // payload degli eventi interrupt/done, quindi /approve funziona comunque.
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
      const frame = parseFrame(rawFrame);
      if (frame.data !== "" || frame.event !== "message") onEvent?.(frame);
    }
  }

  // Flush di un eventuale frame residuo senza doppio newline finale.
  if (buffer.trim() !== "") {
    const frame = parseFrame(buffer);
    if (frame.data !== "") onEvent?.(frame);
  }

  return { threadId };
}

/** Decodifica un singolo frame SSE grezzo in { event, data }. */
function parseFrame(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  return { event, data: dataLines.join("\n") };
}

// ── Chiamate concrete ─────────────────────────────────────────────────────────

/** POST /qualify/stream — eventi: log, done, error. */
export const qualifyStream = (payload, handlers) =>
  streamSSE("/qualify/stream", payload, handlers);

/** POST /ingest/stream — eventi: log, interrupt, done, error. Header X-Thread-Id. */
export const ingestStream = (payload, handlers) =>
  streamSSE("/ingest/stream", payload, handlers);

/**
 * Carica un file catalogo (POST /upload).
 * Il tenant_id NON viene più passato come campo FormData: il backend lo
 * ricava dal JWT nell'header Authorization.
 *
 * @param {File} file  File scelto dalla dropzone.
 * @returns {Promise<{file_path: string, file_format: string}>}
 */
export async function uploadCatalogue(file) {
  const form = new FormData();
  form.append("file", file);
  // NB: NON impostare Content-Type manualmente — il browser lo calcola
  // automaticamente includendo il boundary del multipart.
  const res = await fetch(`${BASE}/upload`, {
    method: "POST",
    headers: authHeaders(), // solo Authorization; Content-Type = browser
    body: form,
  });

  if (res.status === 401) {
    handle401();
    throw new Error("Sessione scaduta. Effettua nuovamente il login.");
  }
  if (!res.ok) {
    let detail = `Upload fallito: HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json(); // { file_path, file_format }
}

/**
 * POST /ingest/{thread_id}/approve — riprende una run sospesa.
 *
 * @param {string} threadId
 * @param {{approved: boolean, feedback?: string|null}} decision
 * @returns {Promise<object>} ApprovalResponse
 */
export async function approveIngestion(threadId, decision) {
  const res = await fetch(
    `${BASE}/ingest/${encodeURIComponent(threadId)}/approve`,
    {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(decision),
    }
  );

  if (res.status === 401) {
    handle401();
    throw new Error("Sessione scaduta. Effettua nuovamente il login.");
  }
  if (res.status === 404) {
    throw new Error("Nessuna run sospesa per questo thread_id (404).");
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json(); // ApprovalResponse
}

/**
 * DELETE /api/v1/tenants/{tenantId}/vector-data — cancella la collection ChromaDB del tenant.
 *
 * @param {string} tenantId
 * @returns {Promise<{dropped: boolean, message: string, collection_name: string}>}
 */
export async function wipeVectorData(tenantId) {
  const res = await fetch(
    `${BASE}/api/v1/tenants/${encodeURIComponent(tenantId)}/vector-data`,
    {
      method: "DELETE",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ confirm_wipe: true }),
    }
  );

  if (res.status === 401) {
    handle401();
    throw new Error("Sessione scaduta. Effettua nuovamente il login.");
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}

/** GET /health — true se il backend risponde 200 (endpoint pubblico, no auth). */
export async function checkHealth() {
  try {
    const res = await fetch(`${BASE}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

// ── Profilo tenant (preventivo brandizzato) ───────────────────────────────────

/** GET /tenants/{id}/profile — ritorna il profilo (o i default vuoti). */
export async function getTenantProfile(tenantId) {
  const res = await fetch(
    `${BASE}/tenants/${encodeURIComponent(tenantId)}/profile`,
    {
      cache: "no-store",
      headers: authHeaders(),
    }
  );
  if (res.status === 401) {
    handle401();
    throw new Error("Sessione scaduta.");
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** PUT /tenants/{id}/profile — salva (upsert) il profilo, ritorna quello salvato. */
export async function saveTenantProfile(tenantId, profile) {
  const res = await fetch(
    `${BASE}/tenants/${encodeURIComponent(tenantId)}/profile`,
    {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(profile),
    }
  );
  if (res.status === 401) {
    handle401();
    throw new Error("Sessione scaduta.");
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch { /* noop */ }
    throw new Error(detail);
  }
  return res.json();
}
