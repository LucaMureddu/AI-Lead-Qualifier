// src/quote.js
// ------------------------------------------------------------------
// Trasforma il risultato della qualificazione in un PREVENTIVO condivisibile:
//  - testo email professionale (copia/incolla + mailto precompilato),
//  - PDF brandizzato scaricabile (jsPDF, importato in modo lazy → code-split).
//
// Usa il PROFILO del tenant (mittente: logo, dati fiscali, IBAN, termini) e il
// DESTINATARIO (Spett.le …). Air-gapped: jsPDF è bundlato; valuta/date via Intl.

// ── Formatter ───────────────────────────────────────────────────────────────────
const EUR = new Intl.NumberFormat("it-IT", { style: "currency", currency: "EUR" });

/** Importo in valuta italiana, es. 1234.5 → "1.234,50 €". */
export function formatMoneyIt(n) {
  const v = Number(n);
  return EUR.format(Number.isFinite(v) ? v : 0);
}

/** Data in formato italiano, es. "16/06/2026". */
export function formatDateIt(d) {
  return new Date(d).toLocaleDateString("it-IT");
}

function addDays(date, days) {
  const d = new Date(date);
  d.setDate(d.getDate() + days);
  return d;
}

// ── Estrazione dati dal risultato ───────────────────────────────────────────────

/** Nome leggibile del servizio mappato. */
export function serviceLabel(svc) {
  return (svc && (svc.matched_name || svc.service)) || "Servizio";
}

/**
 * Descrizione del servizio. Il backend (MapperNode) restituisce in `service` il
 * documento embeddato nel formato "Nome — descrizione [categoria]"; ne estraiamo
 * la sola descrizione, ripulendo la categoria finale tra parentesi quadre.
 */
export function serviceDesc(svc) {
  const raw = (svc && svc.service) || "";
  const dash = raw.indexOf(" — ");
  let desc = dash !== -1 ? raw.slice(dash + 3) : "";
  return desc.replace(/\s*\[[^\]]*\]\s*$/, "").trim();
}

/** Suffisso unità di fatturazione (ignora il simbolo valuta). */
export function unitSuffix(svc) {
  const u = ((svc && svc.unit) || "").trim();
  if (!u || u === "€" || u.toUpperCase() === "EUR") return "";
  const map = {
    hour: "/ora",
    month: "/mese",
    project: "/progetto",
    license: "/licenza",
    user: "/utente",
    day: "/giorno",
    year: "/anno",
  };
  return " " + (map[u.toLowerCase()] || "/" + u);
}

/** Calcola imponibile, IVA e totale dal risultato e dal profilo. */
export function computeTotals(result, settings) {
  const subtotal = Number(result && result.total_quote) || 0;
  const vatEnabled = !!(settings && settings.vatEnabled);
  const vatRate = Number(settings && settings.vatRate) || 0;
  const vat = vatEnabled ? subtotal * vatRate : 0;
  return { subtotal, vat, total: subtotal + vat, vatEnabled, vatRate };
}

/** Estrae la prima email dal testo del lead (per il destinatario del mailto). */
export function extractEmail(text) {
  const m = (text || "").match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  return m ? m[0] : "";
}

/** Riferimento preventivo leggibile, es. "PREV-20260616-A1B2". */
export function quoteRef(result) {
  const d = new Date();
  const ymd = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  const tail = String((result && result.lead_id) || "")
    .replace(/-/g, "")
    .slice(0, 4)
    .toUpperCase();
  return `PREV-${ymd}${tail ? "-" + tail : ""}`;
}

// ── Testo email ───────────────────────────────────────────────────────────────

/**
 * Costruisce oggetto e corpo dell'email di preventivo.
 * @returns {{subject: string, body: string}}
 */
export function buildEmailQuote(result, settings, recipient) {
  settings = settings || {};
  recipient = recipient || {};
  const services = (result && result.mapped_services) || [];
  const { subtotal, vat, total, vatEnabled, vatRate } = computeTotals(result, settings);
  const company = settings.companyName || "";
  const sender = settings.senderName || "";
  const validity = Number(settings.validityDays) || 30;

  const L = [];
  L.push(recipient.name ? `Gentile ${recipient.name},` : "Gentile Cliente,");
  L.push("");
  L.push("grazie per la sua richiesta. Di seguito il preventivo per i servizi richiesti:");
  L.push("");
  if (services.length === 0) {
    L.push("• (nessun servizio mappato)");
  } else {
    for (const svc of services) {
      L.push(`• ${serviceLabel(svc)} — ${formatMoneyIt(svc.price)}${unitSuffix(svc)}`);
      const d = serviceDesc(svc);
      if (d) L.push(`  ${d}`);
    }
  }
  L.push("");
  L.push(`Imponibile: ${formatMoneyIt(subtotal)}`);
  if (vatEnabled) {
    L.push(`IVA (${Math.round(vatRate * 100)}%): ${formatMoneyIt(vat)}`);
    L.push(`Totale: ${formatMoneyIt(total)}`);
  } else {
    L.push(`Totale: ${formatMoneyIt(subtotal)}`);
  }
  L.push("");
  L.push(`Offerta valida ${validity} giorni (fino al ${formatDateIt(addDays(new Date(), validity))}).`);
  if (settings.paymentTerms) L.push(`Condizioni di pagamento: ${settings.paymentTerms}`);
  if (settings.iban) L.push(`IBAN: ${settings.iban}`);
  if (settings.notes) L.push(settings.notes);
  L.push("Restiamo a disposizione per qualsiasi chiarimento.");
  L.push("");
  L.push("Cordiali saluti,");
  L.push(`${sender}${company ? " — " + company : ""}`);

  const subject = `Preventivo servizi${company ? " — " + company : ""} (${quoteRef(result)})`;
  return { subject, body: L.join("\n") };
}

/** Anteprima testuale completa (oggetto + corpo) per la UI. "" se non c'è risultato. */
export function previewEmail(result, settings, recipient) {
  if (!result) return "";
  const { subject, body } = buildEmailQuote(result, settings, recipient);
  return `Oggetto: ${subject}\n\n${body}`;
}

/** Costruisce un URL mailto: precompilato. */
export function buildMailto(to, subject, body) {
  const params = `subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  return `mailto:${to || ""}?${params}`;
}

// ── PDF (jsPDF, import lazy) ─────────────────────────────────────────────────────

/** Rende una stringa sicura per un nome file (solo [A-Za-z0-9_-]). */
function safeFilePart(s) {
  return String(s || "")
    .replace(/[^A-Za-z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/** Dimensioni naturali di un'immagine data URL (per l'aspect ratio del logo). */
function imageSize(dataUrl) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve({ w: img.naturalWidth || 1, h: img.naturalHeight || 1 });
    img.onerror = () => resolve(null);
    img.src = dataUrl;
  });
}

// Palette (RGB) coerente con la UI
const INDIGO = [79, 70, 229]; // indigo-600
const INDIGO_DK = [67, 56, 202]; // indigo-700
const INDIGO_BG = [238, 242, 255]; // indigo-50
const SLATE_800 = [30, 41, 59];
const SLATE_500 = [100, 116, 139];
const SLATE_200 = [226, 232, 240];

/**
 * Genera e scarica un PDF brandizzato del preventivo.
 * jsPDF è importato dinamicamente: il chunk viene servito in locale da Vite.
 */
export async function generatePdf(result, settings, recipient) {
  settings = settings || {};
  recipient = recipient || {};
  const { jsPDF } = await import("jspdf");
  const doc = new jsPDF({ unit: "mm", format: "a4" });

  const company = settings.companyName || "";
  const sender = settings.senderName || "";
  const validity = Number(settings.validityDays) || 30;
  const services = (result && result.mapped_services) || [];
  const { subtotal, vat, total, vatEnabled, vatRate } = computeTotals(result, settings);

  const pageW = doc.internal.pageSize.getWidth();
  const left = 20;
  const right = pageW - 20;
  const setColor = (c) => doc.setTextColor(c[0], c[1], c[2]);

  // Barra brand in cima
  doc.setFillColor(INDIGO[0], INDIGO[1], INDIGO[2]);
  doc.rect(0, 0, pageW, 6, "F");

  let y = 18;

  // Logo (opzionale)
  let logoBottom = y;
  if (settings.logoDataUrl) {
    const size = await imageSize(settings.logoDataUrl);
    if (size) {
      const w = 30;
      const h = Math.min(22, w * (size.h / size.w));
      try {
        doc.addImage(settings.logoDataUrl, "PNG", left, y, w, h);
        logoBottom = y + h;
      } catch {
        /* formato logo non supportato: si prosegue senza */
      }
    }
  }

  // Nome azienda + meta a destra (PREVENTIVO / numero / data)
  const nameX = settings.logoDataUrl ? left + 35 : left;
  const nameY = settings.logoDataUrl ? y + 8 : y + 4;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(16);
  setColor(SLATE_800);
  doc.text(company || "Preventivo", nameX, nameY);

  doc.setFont("helvetica", "normal");
  doc.setFontSize(10);
  setColor(SLATE_500);
  doc.text("PREVENTIVO", right, 14, { align: "right" });
  doc.text(quoteRef(result), right, 19, { align: "right" });
  doc.text(`Data: ${formatDateIt(new Date())}`, right, 24, { align: "right" });

  y = Math.max(logoBottom, nameY) + 7;

  // Dati fiscali + sede del mittente
  doc.setFontSize(9);
  setColor(SLATE_500);
  const fisc = [];
  if (settings.vatNumber) fisc.push(`P.IVA ${settings.vatNumber}`);
  if (settings.taxCode) fisc.push(`C.F. ${settings.taxCode}`);
  if (fisc.length) {
    doc.text(fisc.join("    "), left, y);
    y += 4.5;
  }
  if (settings.address) {
    const al = doc.splitTextToSize(settings.address, 170);
    doc.text(al, left, y);
    y += al.length * 4.5;
  }
  y += 5;

  // Destinatario (Spett.le)
  if (recipient.company || recipient.name || recipient.email) {
    doc.setFont("helvetica", "bold");
    doc.setFontSize(9);
    setColor(SLATE_500);
    doc.text("SPETT.LE", left, y);
    y += 5;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(10);
    setColor(SLATE_800);
    if (recipient.company) {
      doc.text(recipient.company, left, y);
      y += 5;
    }
    if (recipient.name) {
      doc.text(recipient.name, left, y);
      y += 5;
    }
    if (recipient.email) {
      setColor(SLATE_500);
      doc.text(recipient.email, left, y);
      y += 5;
    }
    y += 3;
  }

  // Separatore + intestazioni tabella
  doc.setDrawColor(SLATE_200[0], SLATE_200[1], SLATE_200[2]);
  doc.line(left, y, right, y);
  y += 7;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(10);
  setColor(SLATE_500);
  doc.text("SERVIZIO", left, y);
  doc.text("PREZZO", right, y, { align: "right" });
  y += 6;

  // Righe servizi
  if (services.length === 0) {
    doc.setFont("helvetica", "italic");
    setColor(SLATE_500);
    doc.text("(nessun servizio mappato)", left, y);
    y += 7;
  } else {
    for (const svc of services) {
      if (y > 250) {
        doc.addPage();
        y = 22;
      }
      doc.setFont("helvetica", "bold");
      doc.setFontSize(11);
      setColor(SLATE_800);
      doc.text(serviceLabel(svc), left, y, { maxWidth: 120 });
      doc.text(`${formatMoneyIt(svc.price)}${unitSuffix(svc)}`, right, y, { align: "right" });
      y += 5;
      const d = serviceDesc(svc);
      if (d) {
        doc.setFont("helvetica", "normal");
        doc.setFontSize(9);
        setColor(SLATE_500);
        const lines = doc.splitTextToSize(d, 150);
        doc.text(lines, left, y);
        y += lines.length * 4.5;
      }
      y += 3;
    }
  }

  // Totali
  y += 2;
  doc.setDrawColor(SLATE_200[0], SLATE_200[1], SLATE_200[2]);
  doc.line(left, y, right, y);
  y += 7;
  doc.setFontSize(10);
  const totalsRow = (label, value, opts = {}) => {
    if (opts.highlight) {
      doc.setFillColor(INDIGO_BG[0], INDIGO_BG[1], INDIGO_BG[2]);
      doc.rect(right - 70, y - 5, 70, 8, "F");
    }
    doc.setFont("helvetica", opts.bold ? "bold" : "normal");
    setColor(opts.bold ? INDIGO_DK : SLATE_500);
    doc.text(label, right - 66, y);
    doc.text(value, right - 3, y, { align: "right" });
    y += opts.highlight ? 9 : 6;
  };
  totalsRow("Imponibile", formatMoneyIt(subtotal));
  if (vatEnabled) totalsRow(`IVA (${Math.round(vatRate * 100)}%)`, formatMoneyIt(vat));
  doc.setFontSize(12);
  totalsRow("TOTALE", formatMoneyIt(vatEnabled ? total : subtotal), { bold: true, highlight: true });

  // Validità + condizioni + IBAN + note
  y += 6;
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  setColor(SLATE_500);
  const extra = [
    `Offerta valida ${validity} giorni (fino al ${formatDateIt(addDays(new Date(), validity))}).`,
  ];
  if (settings.paymentTerms) extra.push(`Condizioni di pagamento: ${settings.paymentTerms}`);
  if (settings.iban) extra.push(`IBAN: ${settings.iban}`);
  if (settings.notes) extra.push(settings.notes);
  for (const line of extra) {
    const ls = doc.splitTextToSize(line, right - left);
    doc.text(ls, left, y);
    y += ls.length * 4.5 + 1.5;
  }

  // Firma
  y += 6;
  setColor(SLATE_800);
  doc.setFontSize(10);
  doc.text(`${sender}${company ? " — " + company : ""}`, left, y);

  const fname = `${safeFilePart(quoteRef(result))}_${safeFilePart(company) || "preventivo"}.pdf`;
  doc.save(fname);
}
