/** @type {import('tailwindcss').Config} */
// tailwind.config.js
// Palette "Modern Enterprise / Minimalista" (§5 del piano).
// content: scansiona index.html e tutti i moduli src per il tree-shaking del CSS.
export default {
  content: ["./index.html", "./src/**/*.{js,html}"],
  theme: {
    extend: {
      fontFamily: {
        // UI: Inter — Log/terminale: JetBrains Mono (entrambi self-hosted via @fontsource)
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};
