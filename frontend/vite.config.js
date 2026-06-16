// vite.config.js
// ------------------------------------------------------------------
// Dev server su :3000. Bundle completamente self-hosted (air-gapped):
// nessun asset viene caricato da CDN a runtime — Vite impacchetta tutto.
//
// Proxy opzionale: di default il frontend chiama il backend in CORS diretto
// (BASE = http://localhost:8000, vedi src/config.js). Il backend espone
// CORS "*", quindi funziona senza proxy. Se in futuro vuoi servire tutto
// dallo stesso origin (utile per leggere header custom come X-Thread-Id),
// scommenta il blocco `proxy` e imposta VITE_API_BASE_URL="" nel .env.
import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  server: {
    port: 3000,
    strictPort: true,
    host: true,
    // proxy: {
    //   "/qualify": "http://localhost:8000",
    //   "/ingest":  "http://localhost:8000",
    //   "/upload":  "http://localhost:8000",
    //   "/health":  "http://localhost:8000",
    // },
  },
  preview: {
    port: 3000,
    strictPort: true,
  },
});
