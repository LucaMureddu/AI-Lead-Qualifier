// src/ui.js
// ------------------------------------------------------------------
// Effetti imperativi di basso livello che Alpine non copre bene:
// autoscroll del terminale, colorazione dei tag di log, dropzone drag&drop,
// piccoli formatter. Nessuna logica di business qui.

// ── Colorazione dei tag di log (mappa tag → classe Tailwind) ──────────────────
// Copre sia la pipeline di qualificazione che quella di ingestion.
export const TAG_COLORS = {
  // Qualificazione lead
  SANITIZER: "text-sky-400",
  EXTRACTOR: "text-violet-400",
  MAPPER: "text-amber-400",
  CALCULATOR: "text-cyan-400",
  DELIVERY: "text-emerald-400",
  HUMAN_FALLBACK: "text-rose-300",
  // Ingestion cataloghi
  CHUNKER: "text-sky-400",
  NORMALIZER: "text-violet-400",
  VALIDATOR: "text-amber-400",
  APPROVAL: "text-fuchsia-400",
  FINALIZER: "text-emerald-400",
  // Errori
  ERROR: "text-rose-400",
};

/**
 * Restituisce la classe colore per una riga di log in base al suo tag [TAG].
 * Esempi di input: "[MAPPER] tenant=…", "[DELIVERY] SUCCESS", "[ERROR] …".
 */
export function logLineClass(line) {
  const m = /^\s*\[([A-Z_]+)\]/.exec(line || "");
  if (m && TAG_COLORS[m[1]]) return TAG_COLORS[m[1]];
  if (/\bSUCCESS\b/.test(line || "")) return "text-emerald-400";
  if (/\b(ERROR|FAIL|FAILED)\b/i.test(line || "")) return "text-rose-400";
  return "text-slate-300";
}

/**
 * Autoscroll di un contenitore (console SSE) all'ultima riga.
 * Chiamato da x-effect dopo ogni push nei logs. requestAnimationFrame garantisce
 * che il nuovo nodo sia già stato renderizzato prima di scrollare.
 */
export function autoscroll(el) {
  if (!el) return;
  requestAnimationFrame(() => {
    el.scrollTop = el.scrollHeight;
  });
}

/**
 * Inizializza una dropzone drag&drop.
 * @param {HTMLElement} el       il div .dropzone
 * @param {(files: FileList) => void} onFiles  callback al rilascio/selezione file
 * @returns {() => void} funzione di cleanup
 */
export function initDropzone(el, onFiles) {
  if (!el) return () => {};
  const activate = (e) => {
    e.preventDefault();
    e.stopPropagation();
    el.classList.add("dropzone--active");
  };
  const deactivate = (e) => {
    e.preventDefault();
    e.stopPropagation();
    el.classList.remove("dropzone--active");
  };
  const drop = (e) => {
    deactivate(e);
    const files = e.dataTransfer?.files;
    if (files && files.length) onFiles(files);
  };
  el.addEventListener("dragenter", activate);
  el.addEventListener("dragover", activate);
  el.addEventListener("dragleave", deactivate);
  el.addEventListener("drop", drop);
  return () => {
    el.removeEventListener("dragenter", activate);
    el.removeEventListener("dragover", activate);
    el.removeEventListener("dragleave", deactivate);
    el.removeEventListener("drop", drop);
  };
}

// ── Formatter ─────────────────────────────────────────────────────────────────

/** Formatta un importo numerico con due decimali (es. 800 → "800.00"). */
export function formatMoney(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(2) : "0.00";
}

/** Formatta una dimensione in byte in modo leggibile (es. 2048 → "2.0 KB"). */
export function formatBytes(bytes) {
  const b = Number(bytes) || 0;
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}

/** Estrae l'estensione (minuscola, senza punto) da un nome file. */
export function fileExtension(name) {
  const i = (name || "").lastIndexOf(".");
  return i === -1 ? "" : name.slice(i + 1).toLowerCase();
}
