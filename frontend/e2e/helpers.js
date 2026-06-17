// e2e/helpers.js
// ------------------------------------------------------------------
// Utilità condivise per i test E2E Playwright.

// JWT pre-firmato per "cliente_acme_01":
//   header : {"alg":"HS256","typ":"JWT"}
//   payload: {"sub":"cliente_acme_01","iat":1700000000}
//   firma  : stringa fittizia — il client NON verifica la firma,
//            si limita a decodificare il payload con atob().
// Tutti i backend mock via page.route() ignorano l'header Authorization.
const TEST_JWT =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" +
  ".eyJzdWIiOiJjbGllbnRlX2FjbWVfMDEiLCJpYXQiOjE3MDAwMDAwMDB9" +
  ".test_signature_not_verified_on_client";

/**
 * Pre-popola localStorage con un JWT valido PRIMA che la pagina carichi
 * Alpine.js e i suoi store.  Deve essere chiamata in beforeEach (o all'inizio
 * del test) prima di page.goto("/").
 *
 * Effetto: $store.auth.isAuthenticated === true al primo render →
 * la login screen è nascosta e l'UI principale è visibile.
 *
 * @param {import("@playwright/test").Page} page
 */
export async function seedAuth(page) {
  await page.addInitScript((token) => {
    localStorage.setItem("jwt_token", token);
  }, TEST_JWT);
}
