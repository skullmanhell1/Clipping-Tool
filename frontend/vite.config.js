import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite configuration for the React frontend.
// The dev server proxies `/api` and `/healthz` to the FastAPI backend so the
// SPA and API can be developed side by side.
export default defineConfig({
  plugins: [react()],
  // I10: required since the vite 8 / @vitejs/plugin-react 5 / vitest 3 upgrade.
  //
  // Under that combination vitest transformed `.jsx` test files with the *classic* JSX runtime,
  // which needs `React` in lexical scope - so all 81 component tests failed with
  // "ReferenceError: React is not defined" while `npm run build` was completely fine, because the
  // build goes through the plugin's own transform and the tests do not. Stating it explicitly
  // makes both pipelines emit `jsx-runtime` imports, and means the answer does not depend on
  // which of the two transforms happens to see a file first.
  //
  // `npm run build` warns "Both esbuild and oxc options were set... esbuild options will be
  // ignored" on every run. **That warning is expected — do not act on it.** Vite 8 builds with oxc
  // and the plugin sets `oxc.jsx` itself, which is the "both" being reported, but *vitest* still
  // reads `esbuild`, so this key is the only thing keeping the test transform on the automatic
  // runtime. Both tidy-ups the warning invites were measured on this revision and both take the
  // suite from 141 passing to **100 failed / 41 passed**: deleting the key, and moving it to `oxc`.
  // Silencing a cosmetic build warning by breaking 100 tests is the wrong trade. If you change
  // anything here, `npm run test:run` is the check that matters — `npm run build` stays green
  // through the failure and will not tell you.
  esbuild: { jsx: "automatic" },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
  test: {
    // jsdom, because these are component tests that assert on rendered DOM.
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.js"],
    // Only our own tests; without this vitest would also try to collect from
    // node_modules and from the build output.
    include: ["src/**/*.test.{js,jsx}"],
    // `@vitest/coverage-v8` was already a dependency and had never been configured, so the
    // number it exists to produce was not available. Reported, not gated: a threshold picked
    // before anyone has seen the figure is either met by accident or blocks the first honest
    // measurement, and the untested files here are known (App.jsx and six components).
    coverage: {
      provider: "v8",
      reporter: ["text-summary", "json-summary", "html"],
      reportsDirectory: "coverage",
      // The measured surface is our source. Config, the entry point and the test setup are
      // excluded because covering them says nothing: main.jsx is three lines of mount code and
      // a config file executes on import whether or not anything asserts on it.
      include: ["src/**/*.{js,jsx}"],
      exclude: [
        "src/**/*.test.{js,jsx}",
        "src/test/**",
        "src/main.jsx",
      ],
    },
  },
});
