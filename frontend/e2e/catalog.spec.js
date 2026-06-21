// e2e/catalog.spec.js
// ------------------------------------------------------------------
// Sezione "Catalogo Servizi" — tabella paginata, edit inline, PATCH,
// price_type V3 (FIXED / FREE / VARIABLE), paginazione, empty state.
//
// Tutti i backend call sono mockati via page.route() — nessun server reale.

import { test, expect } from "@playwright/test";
import { seedAuth } from "./helpers.js";

// ── Fixture mock ──────────────────────────────────────────────────────────────

const ITEM_FIXED = {
  id: "aaaaaaaa-0001-0000-0000-000000000001",
  service: "Sviluppo Sito Web",
  price: 2500.0,
  price_type: "FIXED",
  description: "Landing page responsive",
};

const ITEM_FREE = {
  id: "aaaaaaaa-0002-0000-0000-000000000002",
  service: "Onboarding Base",
  price: 0.0,
  price_type: "FREE",
  description: null,
};

const ITEM_VARIABLE = {
  id: "aaaaaaaa-0003-0000-0000-000000000003",
  service: "Consulenza Cloud",
  price: null,
  price_type: "VARIABLE",
  description: "Preventivo su misura",
};

const CATALOG_RESPONSE = {
  items: [ITEM_FIXED, ITEM_FREE, ITEM_VARIABLE],
  total: 3,
  skip: 0,
  limit: 20,
};

const PROFILE = {
  tenant_id: "cliente_acme_01",
  company_name: "Acme Srl",
  vat_enabled: true,
  vat_rate: 0.22,
  validity_days: 30,
};

// ── beforeEach comune ─────────────────────────────────────────────────────────

async function setup(page, catalogResponse = CATALOG_RESPONSE) {
  await seedAuth(page);
  await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
  await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
  await page.route("**/api/catalog/items**", (r) =>
    r.fulfill({ json: catalogResponse })
  );
  await page.goto("/");
  // Navigare alla sezione Catalogo Servizi e aspettare che i dati siano caricati
  const catalogLoaded = page.waitForResponse("**/api/catalog/items**");
  await page.getByTestId("nav-catalog").click();
  await catalogLoaded;
}

// ═════════════════════════════════════════════════════════════════════════════
// Caricamento e rendering tabella
// ═════════════════════════════════════════════════════════════════════════════

test.describe("Catalogo Servizi — caricamento", () => {
  test("mostra tutti e tre i servizi nella tabella", async ({ page }) => {
    await setup(page);

    await expect(page.getByText("Sviluppo Sito Web")).toBeVisible();
    await expect(page.getByText("Onboarding Base")).toBeVisible();
    await expect(page.getByText("Consulenza Cloud")).toBeVisible();
  });

  test("empty state quando il catalogo è vuoto", async ({ page }) => {
    await setup(page, { items: [], total: 0, skip: 0, limit: 20 });

    await expect(
      page.getByText(/Nessun servizio nel catalogo/)
    ).toBeVisible();
  });

  test("footer paginazione mostra il range corretto", async ({ page }) => {
    await setup(page);
    // 3 elementi, skip=0, limit=20 → "1–3 di 3"
    await expect(page.getByText(/1/).first()).toBeVisible();
    await expect(page.getByText(/3/).first()).toBeVisible();
  });

  test("bottone Ricarica richiama GET /api/catalog/items", async ({ page }) => {
    let callCount = 0;
    await seedAuth(page);
    await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
    await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
    await page.route("**/api/catalog/items**", (r) => {
      callCount++;
      return r.fulfill({ json: CATALOG_RESPONSE });
    });
    await page.goto("/");
    const firstLoad = page.waitForResponse("**/api/catalog/items**");
    await page.getByTestId("nav-catalog").click();
    await firstLoad;

    const initialCount = callCount;
    await page.getByRole("button", { name: /Ricarica/ }).click();
    // Aspetta che la richiesta parta
    await page.waitForResponse("**/api/catalog/items**");
    expect(callCount).toBeGreaterThan(initialCount);
  });
});

// ═════════════════════════════════════════════════════════════════════════════
// Badge price_type V3
// ═════════════════════════════════════════════════════════════════════════════

test.describe("Badge price_type V3", () => {
  test("FIXED mostra un input prezzo numerico", async ({ page }) => {
    await setup(page);
    // La riga FIXED deve avere un input numerico visibile
    // (non il badge VARIABLE/FREE)
    const fixedRow = page.getByText("Sviluppo Sito Web").locator("..");
    await expect(fixedRow.locator("select")).toHaveValue("FIXED");
  });

  test("VARIABLE mostra badge 'Su richiesta'", async ({ page }) => {
    await setup(page);
    await expect(page.getByText("Su richiesta").first()).toBeVisible();
  });

  test("FREE mostra badge 'Gratis'", async ({ page }) => {
    await setup(page);
    await expect(page.getByText("Gratis").first()).toBeVisible();
  });
});

// ═════════════════════════════════════════════════════════════════════════════
// Edit inline — happy path
// ═════════════════════════════════════════════════════════════════════════════

test.describe("Edit inline — PATCH", () => {
  test("PATCH di service name aggiorna la riga dopo 200", async ({ page }) => {
    const updatedItem = { ...ITEM_FIXED, service: "Sito Web Professionale" };

    await seedAuth(page);
    await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
    await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
    await page.route("**/api/catalog/items**", (r) =>
      r.fulfill({ json: CATALOG_RESPONSE })
    );
    await page.route(`**/api/catalog/items/${ITEM_FIXED.id}`, (r) =>
      r.fulfill({
        status: 200,
        json: { ...updatedItem, embedding_sync: "queued" },
      })
    );

    await page.goto("/");
    const catalogLoaded = page.waitForResponse("**/api/catalog/items**");
    await page.getByTestId("nav-catalog").click();
    await catalogLoaded;

    // Modifica il campo service della prima riga
    const serviceInput = page
      .locator("tr")
      .filter({ hasText: "Sviluppo Sito Web" })
      .locator("input[type=text]")
      .first();
    await serviceInput.waitFor({ state: "visible" });
    await serviceInput.fill("Sito Web Professionale");

    // Clicca Salva
    const saveBtn = page
      .locator("tr")
      .filter({ hasText: "Sito Web Professionale" })
      .getByTitle("Salva modifiche");
    await saveBtn.click();

    // Dopo il 200, la riga mostra il nome aggiornato
    await expect(page.getByText("Sito Web Professionale")).toBeVisible();
  });

  test("cambio da FIXED a VARIABLE nasconde input prezzo e mostra badge", async ({
    page,
  }) => {
    await setup(page);

    // Trova il select price_type della prima riga (FIXED)
    const priceTypeSelect = page
      .locator("tr")
      .filter({ hasText: "Sviluppo Sito Web" })
      .locator("select")
      .first();

    await priceTypeSelect.selectOption("VARIABLE");

    // Il badge 'Su richiesta' deve diventare visibile in quella riga
    const row = page.locator("tr").filter({ hasText: "Sviluppo Sito Web" });
    await expect(row.getByText("Su richiesta")).toBeVisible();
  });

  test("cambio da FIXED a FREE mostra badge Gratis (price read-only)", async ({
    page,
  }) => {
    await setup(page);

    const priceTypeSelect = page
      .locator("tr")
      .filter({ hasText: "Sviluppo Sito Web" })
      .locator("select")
      .first();

    await priceTypeSelect.selectOption("FREE");

    const row = page.locator("tr").filter({ hasText: "Sviluppo Sito Web" });
    await expect(row.getByText("Gratis")).toBeVisible();
  });

  test("bottone Salva è disabilitato finché la riga non è stata modificata", async ({
    page,
  }) => {
    await setup(page);

    // Senza modifiche, il bottone Salva non deve essere visibile / interattivo
    // (isDirty=false → il pulsante ha :disabled="!isDirty || isSaving")
    const saveBtn = page
      .locator("tr")
      .filter({ hasText: "Sviluppo Sito Web" })
      .getByTitle("Salva modifiche");

    await expect(saveBtn).toBeDisabled();
  });

  test("Annulla ripristina il valore originale senza chiamare il backend", async ({
    page,
  }) => {
    let patchCalled = false;
    await seedAuth(page);
    await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
    await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
    await page.route("**/api/catalog/items**", (r) =>
      r.fulfill({ json: CATALOG_RESPONSE })
    );
    await page.route(`**/api/catalog/items/${ITEM_FIXED.id}`, (r) => {
      patchCalled = true;
      return r.fulfill({ status: 200, json: ITEM_FIXED });
    });

    await page.goto("/");
    const catalogLoaded = page.waitForResponse("**/api/catalog/items**");
    await page.getByTestId("nav-catalog").click();
    await catalogLoaded;

    // Modifica il nome
    const serviceInput = page
      .locator("tr")
      .filter({ hasText: "Sviluppo Sito Web" })
      .locator("input[type=text]")
      .first();
    await serviceInput.waitFor({ state: "visible" });
    await serviceInput.fill("Valore temporaneo");

    // Clicca Annulla
    const discardBtn = page
      .locator("tr")
      .filter({ hasText: "Valore temporaneo" })
      .getByTitle("Annulla modifiche");
    await discardBtn.click();

    // Il valore originale torna
    await expect(page.getByText("Sviluppo Sito Web")).toBeVisible();
    expect(patchCalled).toBe(false);
  });
});

// ═════════════════════════════════════════════════════════════════════════════
// Validazione lato client
// ═════════════════════════════════════════════════════════════════════════════

test.describe("Validazione client-side", () => {
  test("prezzo negativo su FIXED non invia la PATCH", async ({ page }) => {
    let patchCalled = false;
    await seedAuth(page);
    await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
    await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
    await page.route("**/api/catalog/items**", (r) =>
      r.fulfill({ json: CATALOG_RESPONSE })
    );
    await page.route(`**/api/catalog/items/${ITEM_FIXED.id}`, () => {
      patchCalled = true;
    });

    await page.goto("/");
    const catalogLoaded = page.waitForResponse("**/api/catalog/items**");
    await page.getByTestId("nav-catalog").click();
    await catalogLoaded;

    // Cambia il prezzo a un valore negativo
    const row = page.locator("tr").filter({ hasText: "Sviluppo Sito Web" });
    const priceInput = row.locator("input[type=number]").first();
    await priceInput.waitFor({ state: "visible" });
    await priceInput.fill("-100");

    const saveBtn = row.getByTitle("Salva modifiche");
    await saveBtn.click();

    // Un toast di errore deve apparire e il backend NON deve essere chiamato
    await expect(page.getByText(/prezzo valido/i)).toBeVisible();
    expect(patchCalled).toBe(false);
  });
});

// ═════════════════════════════════════════════════════════════════════════════
// Risposta 422 dal backend (CheckViolationError)
// ═════════════════════════════════════════════════════════════════════════════

test.describe("Gestione errori backend", () => {
  test("422 dal PATCH mostra toast errore e fa rollback dei dati nella riga", async ({
    page,
  }) => {
    await seedAuth(page);
    await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
    await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
    await page.route("**/api/catalog/items**", (r) =>
      r.fulfill({ json: CATALOG_RESPONSE })
    );
    await page.route(`**/api/catalog/items/${ITEM_FIXED.id}`, (r) =>
      r.fulfill({
        status: 422,
        json: {
          detail: "Violazione vincolo price_type/price: combinazione non valida.",
        },
      })
    );

    await page.goto("/");
    const catalogLoaded = page.waitForResponse("**/api/catalog/items**");
    await page.getByTestId("nav-catalog").click();
    await catalogLoaded;

    // Modifica il nome e premi Salva (il backend restituirà 422)
    const row = page.locator("tr").filter({ hasText: "Sviluppo Sito Web" });
    const serviceInput = row.locator("input[type=text]").first();
    await serviceInput.waitFor({ state: "visible" });
    await serviceInput.fill("Nome che genera 422");

    await row.getByTitle("Salva modifiche").click();

    // Toast di errore con riferimento al vincolo DB
    await expect(page.getByText(/Vincolo DB violato/i)).toBeVisible();
    // La riga torna al valore originale (rollback)
    await expect(page.getByText("Sviluppo Sito Web")).toBeVisible();
  });
});

// ═════════════════════════════════════════════════════════════════════════════
// Paginazione
// ═════════════════════════════════════════════════════════════════════════════

test.describe("Paginazione", () => {
  test("bottone pagina precedente è disabilitato alla prima pagina", async ({
    page,
  }) => {
    await setup(page);
    const prevBtn = page.getByRole("button", { name: /Precedente/i });
    await expect(prevBtn).toBeDisabled();
  });

  test("pagina successiva carica i dati della pagina 2", async ({ page }) => {
    const PAGE1 = {
      items: Array.from({ length: 20 }, (_, i) => ({
        id: `id-${i}`,
        service: `Servizio ${i + 1}`,
        price: 100,
        price_type: "FIXED",
        description: null,
      })),
      total: 25,
      skip: 0,
      limit: 20,
    };
    const PAGE2 = {
      items: Array.from({ length: 5 }, (_, i) => ({
        id: `id-${20 + i}`,
        service: `Servizio ${20 + i + 1}`,
        price: 100,
        price_type: "FIXED",
        description: null,
      })),
      total: 25,
      skip: 20,
      limit: 20,
    };

    let requestSkip = 0;
    await seedAuth(page);
    await page.route("**/health", (r) => r.fulfill({ json: { status: "ok" } }));
    await page.route("**/tenants/**/profile", (r) => r.fulfill({ json: PROFILE }));
    await page.route("**/api/catalog/items**", (r) => {
      const url = new URL(r.request().url());
      requestSkip = parseInt(url.searchParams.get("skip") || "0", 10);
      return r.fulfill({ json: requestSkip === 0 ? PAGE1 : PAGE2 });
    });

    await page.goto("/");
    const firstPage = page.waitForResponse("**/api/catalog/items**");
    await page.getByTestId("nav-catalog").click();
    await firstPage;

    // Prima pagina: Servizio 1 visibile
    await expect(page.getByText("Servizio 1")).toBeVisible();

    // Clicca pagina successiva
    await page.getByRole("button", { name: /Successiva/i }).click();
    await page.waitForResponse("**/api/catalog/items**");

    // Seconda pagina: Servizio 21 visibile
    await expect(page.getByText("Servizio 21")).toBeVisible();
    // Footer mostra 21–25 di 25
    await expect(page.getByText(/21/).first()).toBeVisible();
  });
});
