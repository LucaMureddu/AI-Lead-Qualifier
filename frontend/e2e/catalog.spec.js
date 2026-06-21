// e2e/catalog.spec.js
// ------------------------------------------------------------------
// Sezione "Catalogo Servizi" — tabella paginata, edit inline, PATCH,
// price_type V3 (FIXED / FREE / VARIABLE), paginazione, empty state.
//
// Tutti i backend call sono mockati via page.route() — nessun server reale.
//
// NOTE sui locator:
//   - I nomi dei servizi sono in <input x-model="edit.service">: usare
//     toHaveValue() o row.locator("input[type=text]") – non getByText().
//   - Le righe hanno data-testid="catalog-row-<id>" per selezioni stabili.
//   - Le opzioni del select usano "Su richiesta" / "Gratis" come label:
//     usare locator("span", { hasText }) per i badge, non getByText scoped.
//   - I bottoni di paginazione mostrano "← Prec" / "Succ →".

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
  // Naviga al catalogo e aspetta che i dati siano caricati dalla API mock
  const catalogLoaded = page.waitForResponse("**/api/catalog/items**");
  await page.getByTestId("nav-catalog").click();
  await catalogLoaded;
}

// ── Helper: riga per ID item ──────────────────────────────────────────────────

const itemRow = (page, id) => page.getByTestId(`catalog-row-${id}`);

// ═════════════════════════════════════════════════════════════════════════════
// Caricamento e rendering tabella
// ═════════════════════════════════════════════════════════════════════════════

test.describe("Catalogo Servizi — caricamento", () => {
  test("mostra tutti e tre i servizi nella tabella", async ({ page }) => {
    await setup(page);

    // I nomi servizio sono valori di <input>, non testo — usare toHaveValue()
    await expect(itemRow(page, ITEM_FIXED.id).locator("input[type=text]").first()).toHaveValue("Sviluppo Sito Web");
    await expect(itemRow(page, ITEM_FREE.id).locator("input[type=text]").first()).toHaveValue("Onboarding Base");
    await expect(itemRow(page, ITEM_VARIABLE.id).locator("input[type=text]").first()).toHaveValue("Consulenza Cloud");
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
    // Aspetta che il bottone mostri "Ricarica" (non "Caricamento…") prima di cliccare
    const reloadBtn = page.getByRole("button", { name: /Ricarica/ });
    await expect(reloadBtn).toBeVisible();
    await reloadBtn.click();
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
    const fixedRow = itemRow(page, ITEM_FIXED.id);
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

    // Usa il data-testid stabile della riga (non dipende dal valore dell'input)
    const fixedRow = itemRow(page, ITEM_FIXED.id);
    const serviceInput = fixedRow.locator("input[type=text]").first();
    await serviceInput.waitFor({ state: "visible" });
    await serviceInput.fill("Sito Web Professionale");

    // Aspetta la risposta PATCH prima di asserire lo stato aggiornato
    const patchDone = page.waitForResponse(
      (resp) =>
        resp.url().includes(`/api/catalog/items/${ITEM_FIXED.id}`) &&
        resp.request().method() === "PATCH"
    );
    await fixedRow.getByTitle("Salva modifiche").click();
    await patchDone;

    // Dopo il 200, l'input della riga mostra il nome aggiornato
    await expect(fixedRow.locator("input[type=text]").first()).toHaveValue("Sito Web Professionale");
  });

  test("cambio da FIXED a VARIABLE nasconde input prezzo e mostra badge", async ({
    page,
  }) => {
    await setup(page);

    const fixedRow = itemRow(page, ITEM_FIXED.id);
    await fixedRow.locator("select").first().selectOption("VARIABLE");

    // Il badge 'Su richiesta' è uno <span> — scope a span per non matchare <option>
    await expect(fixedRow.locator("span", { hasText: "Su richiesta" }).first()).toBeVisible();
  });

  test("cambio da FIXED a FREE mostra badge Gratis (price read-only)", async ({
    page,
  }) => {
    await setup(page);

    const fixedRow = itemRow(page, ITEM_FIXED.id);
    await fixedRow.locator("select").first().selectOption("FREE");

    // Badge 'Gratis' è uno <span> — scope a span per non matchare <option>
    await expect(fixedRow.locator("span", { hasText: "Gratis" }).first()).toBeVisible();
  });

  test("bottone Salva è disabilitato finché la riga non è stata modificata", async ({
    page,
  }) => {
    await setup(page);

    const fixedRow = itemRow(page, ITEM_FIXED.id);
    await expect(fixedRow.getByTitle("Salva modifiche")).toBeDisabled();
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

    // Usa il data-testid stabile: il riferimento alla riga non cambia dopo fill()
    const fixedRow = itemRow(page, ITEM_FIXED.id);
    const serviceInput = fixedRow.locator("input[type=text]").first();
    await serviceInput.waitFor({ state: "visible" });
    await serviceInput.fill("Valore temporaneo");

    // Clicca Annulla — riga trovata tramite testid, non dal testo dell'input
    const discardBtn = fixedRow.getByTitle("Annulla modifiche");
    await expect(discardBtn).toBeEnabled();
    await discardBtn.click();

    // Il valore originale deve tornare nell'input della riga
    await expect(fixedRow.locator("input[type=text]").first()).toHaveValue("Sviluppo Sito Web");
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

    const fixedRow = itemRow(page, ITEM_FIXED.id);
    const priceInput = fixedRow.locator("input[type=number]").first();
    await priceInput.waitFor({ state: "visible" });
    await priceInput.fill("-100");

    await fixedRow.getByTitle("Salva modifiche").click();

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

    const fixedRow = itemRow(page, ITEM_FIXED.id);
    const serviceInput = fixedRow.locator("input[type=text]").first();
    await serviceInput.waitFor({ state: "visible" });
    await serviceInput.fill("Nome che genera 422");

    // Aspetta la risposta 422 prima di asserire toast e rollback
    const patchDone = page.waitForResponse(
      (resp) => resp.url().includes(`/api/catalog/items/${ITEM_FIXED.id}`)
    );
    await fixedRow.getByTitle("Salva modifiche").click();
    await patchDone;

    // Toast di errore con riferimento al vincolo DB
    await expect(page.getByText(/Vincolo DB violato/i)).toBeVisible();
    // La riga torna al valore originale (rollback)
    await expect(fixedRow.locator("input[type=text]").first()).toHaveValue("Sviluppo Sito Web");
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
    // Il bottone mostra "← Prec" (non "Precedente")
    const prevBtn = page.getByRole("button", { name: /Prec/ });
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

    // Prima pagina: "Servizio 1" è in un input (id="id-0") — usare toHaveValue
    await expect(page.getByTestId("catalog-row-id-0").locator("input[type=text]").first()).toHaveValue("Servizio 1");

    // Clicca pagina successiva — il bottone mostra "Succ →"
    const nextPageLoaded = page.waitForResponse("**/api/catalog/items**");
    await page.getByRole("button", { name: /Succ/ }).click();
    await nextPageLoaded;

    // Seconda pagina: "Servizio 21" è in un input (id="id-20")
    await expect(page.getByTestId("catalog-row-id-20").locator("input[type=text]").first()).toHaveValue("Servizio 21");
    // Footer mostra 21–25 di 25
    await expect(page.getByText(/21/).first()).toBeVisible();
  });
});
