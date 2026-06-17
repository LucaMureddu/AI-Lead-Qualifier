// e2e/settings.spec.js
// ------------------------------------------------------------------
// Impostazioni: salvataggio profilo (PUT mockato) e badge connessione.

import { test, expect } from "@playwright/test";

const TENANT = "cliente_acme_01";
const EMPTY_PROFILE = { tenant_id: TENANT, company_name: "", vat_enabled: true, vat_rate: 0.22, validity_days: 30 };

test("salva profilo → conferma 'Salvato' e payload corretto", async ({ page }) => {
  await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));

  let putBody = null;
  await page.route("**/tenants/**/profile", async (route) => {
    const req = route.request();
    if (req.method() === "PUT") {
      putBody = JSON.parse(req.postData() || "{}");
      await route.fulfill({ json: { ...putBody, tenant_id: TENANT } });
    } else {
      await route.fulfill({ json: EMPTY_PROFILE });
    }
  });

  // Attendi il GET profilo iniziale PRIMA di compilare, così applyProfile
  // (che gira all'avvio) non sovrascrive il valore che inseriamo.
  const initialProfileLoaded = page.waitForResponse(
    (r) => /\/tenants\/.*\/profile/.test(r.url()) && r.request().method() === "GET",
  );
  await page.goto("/");
  await initialProfileLoaded;

  await page.getByTestId("nav-settings").click();
  await page.getByTestId("profile-company").fill("ACME Test S.r.l.");
  await page.getByRole("button", { name: /Salva profilo/ }).click();

  await expect(page.getByText("Salvato", { exact: true })).toBeVisible();
  expect(putBody?.company_name).toBe("ACME Test S.r.l.");
});

test("badge 'API Offline' quando /health fallisce", async ({ page }) => {
  await page.route("**/health", (r) => r.fulfill({ status: 500, body: "" }));
  await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: EMPTY_PROFILE }));

  await page.goto("/");
  await expect(page.getByText("API Offline").first()).toBeVisible();
});
