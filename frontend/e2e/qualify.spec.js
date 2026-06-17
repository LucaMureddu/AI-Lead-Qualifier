// e2e/qualify.spec.js
// ------------------------------------------------------------------
// Flusso "Qualifica Lead": testo → stream SSE finto → Card preventivo.
// Il frontend consuma SSE-via-POST (fetch + ReadableStream), quindi il body
// mockato deve simulare i frame: righe "event:"/"data:" separate da "\n\n".

import { test, expect } from "@playwright/test";

const PROFILE = {
  tenant_id: "cliente_acme_01",
  company_name: "",
  vat_enabled: true,
  vat_rate: 0.22,
  validity_days: 30,
};

function sse(...frames) {
  return frames.map((f) => f + "\n\n").join("");
}

test.beforeEach(async ({ page }) => {
  // /health (badge) e /profile (caricato all'avvio) sempre mockati: niente backend reale.
  await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
});

test("testo → preventivo con servizi mappati", async ({ page }) => {
  await page.route("**/qualify/stream", (r) =>
    r.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: sse(
        "event: log\ndata: [SANITIZER] ok",
        "event: log\ndata: [MAPPER] mapped=1",
        'event: done\ndata: {"lead_id":"x","total_quote":2500,"mapped_services":[{"matched_name":"Sviluppo Sito","price":2500,"unit":"€","distance":0.12}]}',
      ),
    }),
  );

  await page.goto("/");
  await page.getByPlaceholder(/Incolla qui/).fill("Ci serve un sito web aziendale completo");
  await page.getByRole("button", { name: /Genera Preventivo AI/ }).click();

  await expect(page.getByTestId("quote-total")).toHaveText("2500.00 €");
  await expect(page.getByText("Sviluppo Sito")).toBeVisible();
  await expect(page.getByText("[MAPPER] mapped=1")).toBeVisible();
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

test("event: error mostra il box di errore", async ({ page }) => {
  await page.route("**/qualify/stream", (r) =>
    r.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: sse('event: error\ndata: {"error":"Boom dal grafo"}'),
    }),
  );

  await page.goto("/");
  await page.getByPlaceholder(/Incolla qui/).fill("Una richiesta valida e lunga abbastanza");
  await page.getByRole("button", { name: /Genera Preventivo AI/ }).click();

  await expect(page.getByText("Boom dal grafo")).toBeVisible();
});
