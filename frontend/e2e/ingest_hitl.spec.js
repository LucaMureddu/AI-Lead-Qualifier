// e2e/ingest_hitl.spec.js
// ------------------------------------------------------------------
// Onboarding catalogo con Human-in-the-Loop:
//   upload (mock) → ingest/stream che emette `event: interrupt` →
//   fase "review" con item flaggati → Approva / Rifiuta (POST /approve mock).
//
// Note:
// - il file input è nascosto: setInputFiles funziona comunque (niente click).
// - lo stream ingest porta X-Thread-Id nell'header E thread_id nel payload
//   interrupt; il frontend usa l'uno o l'altro per chiamare /approve.

import { test, expect } from "@playwright/test";

const TENANT = "cliente_acme_01";
const THREAD = "ingest-cliente_acme_01-abc123";

const PROFILE = { tenant_id: TENANT, vat_enabled: true, vat_rate: 0.22, validity_days: 30 };

const REVIEW_PAYLOAD = {
  tenant_id: TENANT,
  total_items: 3,
  flagged_count: 1,
  confidence_score: 0.42,
  flagged_items: [
    {
      id: "i1",
      name: "Servizio Dubbio",
      price: 0,
      currency: "EUR",
      flag_reason: "zero price with no description",
      raw_data: { Nome: "Servizio Dubbio", Prezzo: "" },
    },
  ],
  validation_errors: ["riga 2: prezzo nullo senza descrizione"],
};

const CSV_FILE = {
  name: "catalogo.csv",
  mimeType: "text/csv",
  buffer: Buffer.from("name,price\nServizio Dubbio,\n"),
};

function sse(...frames) {
  return frames.map((f) => f + "\n\n").join("");
}

test.beforeEach(async ({ page }) => {
  await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
  await page.route("**/upload", (r) =>
    r.fulfill({ json: { file_path: `/uploads/${TENANT}/catalogo.csv`, file_format: "csv" } }),
  );
  // ingest/stream → due log + interrupt (con review_payload)
  await page.route("**/ingest/stream", (r) =>
    r.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "x-thread-id": THREAD },
      body: sse(
        "event: log\ndata: [CHUNKER] rows=3 chunks=1",
        "event: log\ndata: [VALIDATOR] flagged=1",
        "event: interrupt\ndata: " +
          JSON.stringify({ thread_id: THREAD, tenant_id: TENANT, review_payload: REVIEW_PAYLOAD }),
      ),
    }),
  );
});

async function uploadAndReachReview(page) {
  await page.goto("/");
  await page.getByTestId("nav-ingest").click();
  await page.getByTestId("ingest-file-input").setInputFiles(CSV_FILE);
  await expect(page.getByText("Revisione necessaria")).toBeVisible();
  // exact: il nome compare anche nel <pre> dei dati grezzi (raw_data).
  await expect(page.getByText("Servizio Dubbio", { exact: true })).toBeVisible();
}

test("upload → review → Approva e scrivi", async ({ page }) => {
  await page.route("**/ingest/*/approve", (r) =>
    r.fulfill({
      json: { thread_id: THREAD, status: "completed", total_items: 3, flagged_count: 1, validation_errors: [] },
    }),
  );

  await uploadAndReachReview(page);
  await page.getByRole("button", { name: /Approva e scrivi/ }).click();
  await expect(page.getByText("Ingestion completata")).toBeVisible();
});

test("upload → review → Rifiuta", async ({ page }) => {
  await page.route("**/ingest/*/approve", (r) =>
    r.fulfill({
      json: { thread_id: THREAD, status: "rejected", total_items: 3, flagged_count: 1, validation_errors: [] },
    }),
  );

  await uploadAndReachReview(page);
  await page.getByRole("button", { name: /^Rifiuta/ }).click();
  await expect(page.getByText("Ingestion rifiutata")).toBeVisible();
});

test("estensione non supportata → fase error", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("nav-ingest").click();
  await page.getByTestId("ingest-file-input").setInputFiles({
    name: "catalogo.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("non valido"),
  });
  await expect(page.getByText(/Estensione non supportata/)).toBeVisible();
});

test("upload → review → Correggi e riprocessa → completata", async ({ page }) => {
  // /approve è lo step 1 (best-effort) del retry: chiude la run sospesa.
  await page.route("**/ingest/*/approve", (r) =>
    r.fulfill({
      json: { thread_id: THREAD, status: "rejected", total_items: 3, flagged_count: 1, validation_errors: [] },
    }),
  );

  // Sovrascrive il mock del beforeEach: 1ª /ingest/stream → interrupt;
  // 2ª (quella con review_feedback nel body) → done.
  await page.unroute("**/ingest/stream");
  await page.route("**/ingest/stream", (r) => {
    const body = r.request().postDataJSON() || {};
    if (body.review_feedback) {
      r.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "x-thread-id": THREAD + "-2" },
        body: sse(
          "event: log\ndata: [NORMALIZER] reprocess con feedback",
          "event: done\ndata: " +
            JSON.stringify({ thread_id: THREAD + "-2", tenant_id: TENANT, total_items: 3, flagged_count: 0, validation_errors: [] }),
        ),
      });
    } else {
      r.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "x-thread-id": THREAD },
        body: sse(
          "event: log\ndata: [VALIDATOR] flagged=1",
          "event: interrupt\ndata: " +
            JSON.stringify({ thread_id: THREAD, tenant_id: TENANT, review_payload: REVIEW_PAYLOAD }),
        ),
      });
    }
  });

  await uploadAndReachReview(page);
  await page.getByPlaceholder(/Feedback opzionale/).fill("Correggi i prezzi nulli");
  await page.getByRole("button", { name: /Correggi e riprocessa/ }).click();
  await expect(page.getByText("Ingestion completata")).toBeVisible();
});
