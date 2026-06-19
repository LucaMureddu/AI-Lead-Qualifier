// e2e/qualify.spec.js
// ------------------------------------------------------------------
// Flusso "Qualifica Lead" — V2 polling (no SSE).
//
// Il frontend invia POST /lead → riceve { thread_id }, poi fa polling
// su GET /status/{threadId} finché lo status raggiunge uno stato terminale.

import { test, expect } from "@playwright/test";
import { seedAuth } from "./helpers.js";

const PROFILE = {
  tenant_id: "cliente_acme_01",
  company_name: "",
  vat_enabled: true,
  vat_rate: 0.22,
  validity_days: 30,
};

test.beforeEach(async ({ page }) => {
  // Autentica pre-seeding localStorage PRIMA che Alpine carichi gli store.
  await seedAuth(page);
  // /health (badge) e /profile (caricato all'avvio) sempre mockati: niente backend reale.
  await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
});

test("testo → preventivo con servizi mappati", async ({ page }) => {
  // V2: POST /lead → 202 { thread_id }
  await page.route("**/lead", (r) =>
    r.fulfill({
      status: 202,
      json: { thread_id: "test-thread-1", status: "queued" },
    }),
  );

  // V2: GET /status/{threadId} → completed con risultato
  await page.route("**/status/test-thread-1", (r) =>
    r.fulfill({
      json: {
        thread_id: "test-thread-1",
        status: "completed",
        result: {
          total_quote: 2500,
          mapped_services: [
            { matched_name: "Sviluppo Sito", price: 2500, unit: "€", distance: 0.12 },
          ],
        },
        error_detail: null,
      },
    }),
  );

  await page.goto("/");
  await page.getByPlaceholder(/Incolla qui/).fill("Ci serve un sito web aziendale completo");
  await page.getByRole("button", { name: /Genera Preventivo AI/ }).click();

  await expect(page.getByTestId("quote-total")).toHaveText("2500.00 €");
  await expect(page.getByText("Sviluppo Sito")).toBeVisible();
  // Il badge diventa "API Connected" dopo il primo /health ok.
  await expect(page.getByText("API Connected").first()).toBeVisible();
});

test("bottone disabilitato sotto i 10 caratteri", async ({ page }) => {
  await page.goto("/");
  const submit = page.getByRole("button", { name: /Genera Preventivo AI/ });
  const input = page.getByPlaceholder(/Incolla qui/);

  await input.fill("ciao"); // 4 caratteri
  await expect(submit).toBeDisabled();

  await input.fill("Testo del lead abbastanza lungo");
  await expect(submit).toBeEnabled();
});

test("status error mostra il box di errore", async ({ page }) => {
  // V2: POST /lead → 202 { thread_id }
  await page.route("**/lead", (r) =>
    r.fulfill({
      status: 202,
      json: { thread_id: "test-thread-err", status: "queued" },
    }),
  );

  // V2: GET /status/{threadId} → error con error_detail
  await page.route("**/status/test-thread-err", (r) =>
    r.fulfill({
      json: {
        thread_id: "test-thread-err",
        status: "error",
        result: null,
        error_detail: "Boom dal grafo",
      },
    }),
  );

  await page.goto("/");
  await page.getByPlaceholder(/Incolla qui/).fill("Una richiesta valida e lunga abbastanza");
  await page.getByRole("button", { name: /Genera Preventivo AI/ }).click();

  await expect(page.getByText("Boom dal grafo")).toBeVisible();
});
