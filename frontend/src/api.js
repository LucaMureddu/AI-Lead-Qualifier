// src/api.js
// ------------------------------------------------------------------
// Livello di comunicazione col backend. NON tocca mai il DOM:
// ritorna dati / invoca callback su evento (vedi §3 del piano).
//
// Contiene:
//   - streamSSE()  → parser SSE-via-POST (fetch + ReadableStream)  §4.1
//   - qualifyStream / ingestStream → chiamate concrete agli stream
//   - uploadCatalogue() → POST /upload (multipart)  (B1)
//   - approveIngestion() → POST /ingest/{thread_id}/approve
//   - checkHealth() → GET /health (per il badge)
//
// Nota SSE-via-POST: gli endpoint stream sono POST con body JSON; EventSource
// nativo supporta solo GET, quindi serve un parser custom basato su fetch.

import { API_BASE_URL } from "./config.js";

const BASE = API_BASE_URL;

/**
 * Consuma uno stream SSE da un endpoint POST.
 *
 * Bufferizza i chunk e splitta sui doppi newline ("\n\n"), che il backend usa
 * come delimitatore di frame. Cattura l'header X-Thread-Id (necessario per
 * /approve) e lo restituisce; lo espone anche subito via onMeta, appena gli
 * header arrivano (utile perché lo stream ingest può chiudersi sull'interrupt).
 *
 * @param {string} path     es. "/qualify/stream"
 * @param {object} body     payload JSON
 * @param {object} handlers { onEvent(frame), onMeta({threadId}), signal }
 * @returns {Promise<{threadId: string|null}>}
 */
export async function streamSSE(path, body, { onEvent, onMeta, signal } = {}) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    // Prova a estrarre un dettaglio JSON dal corpo dell'errore (FastAPI: {detail}).
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch {
      /* corpo non-JSON: tieni il messaggio HTTP */
    }
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
 * Carica un file catalogo (B1). Ritorna { file_path, file_format }.
 * @param {File} file        file scelto dalla dropzone
 * @param {string} tenantId  tenant proprietario (scopa la cartella di salvataggio)
 */
export async function uploadCatalogue(file, tenantId) {
  const form = new FormData();
  form.append("file", file);
  form.append("tenant_id", tenantId);
  const res = await fetch(`${BASE}/upload`, { method: "POST", body: form });
  if (!res.ok) {
    let detail = `Upload fallito: HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch {
      /* noop */
    }
    throw new Error(detail);
  }
  return res.json(); // { file_path, file_format }
}

/**
 * POST /ingest/{thread_id}/approve — riprende una run sospesa.
 * @param {string} threadId
 * @param {{approved: boolean, feedback?: string|null}} decision
 * @returns {Promise<object>} ApprovalResponse
 */
export async function approveIngestion(threadId, decision) {
  const res = await fetch(
    `${BASE}/ingest/${encodeURIComponent(threadId)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(decision),
    }
  );
  if (res.status === 404) {
    throw new Error("Nessuna run sospesa per questo thread_id (404).");
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch {
      /* noop */
    }
    throw new Error(detail);
  }
  return res.json(); // ApprovalResponse
}

/** GET /health — true se il backend risponde 200. */
export async function checkHealth() {
  try {
    const res = await fetch(`${BASE}/health`, { cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

// ── Profilo tenant (preventivo brandizzato) ──────────────────────────────────

/** GET /tenants/{id}/profile — ritorna il profilo (o i default vuoti). */
export async function getTenantProfile(tenantId) {
  const res = await fetch(
    `${BASE}/tenants/${encodeURIComponent(tenantId)}/profile`,
    { cache: "no-store" }
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** PUT /tenants/{id}/profile — salva (upsert) il profilo, ritorna quello salvato. */
export async function saveTenantProfile(tenantId, profile) {
  const res = await fetch(
    `${BASE}/tenants/${encodeURIComponent(tenantId)}/profile`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    }
  );
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = j.detail;
    } catch {
      /* noop */
    }
    throw new Error(detail);
  }
  return res.json();
}
