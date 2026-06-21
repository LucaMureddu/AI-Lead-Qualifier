// playwright.config.js
// ------------------------------------------------------------------
// E2E del frontend (Vite/Alpine) — Livello 3 del TESTING_PLAN (§5).
//
// Strategia: backend MOCKATO a livello di rete dentro ogni test
// (page.route), così niente Ollama/Chroma, deterministico e veloce.
// Il webServer builda e serve il bundle di produzione su :3000
// (vite.config.js forza preview su 3000, strictPort).
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "list" : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: {
    command: "npm run build && npm run preview",
    port: 3000,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
