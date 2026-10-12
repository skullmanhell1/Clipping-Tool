import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite configuration for the React frontend.
// The dev server proxies `/api` and `/healthz` to the FastAPI backend so the
// SPA and API can be developed side by side.
export default defineConfig({
  plugins: [react()],
  // I10: keep Vitest's JSX transform on the automatic runtime. This was added after the Vite 8 /
  // @vitejs/plugin-react 5 / Vitest 3 upgrade made `.jsx` tests use the classic runtime: component
  // tests then failed with `React is not defined` while the production build remained green. The
  // frontend now uses Vitest 4.1.11, and this setting still passes the full 148-test suite.
  //
  // `npm run build` warns that both esbuild and oxc options were set. **That warning is expected.**
  // Vite 8 builds with oxc; Vitest's test transform uses the esbuild setting above. Keep the
  // setting unless `npm run test:run` proves it can be removed or moved — a green build alone
  // will not catch a JSX-transform regression.
  esbuild: { jsx: "automatic" },
  server: {
    host: "0.0.0.0",
    // The preview is served from a per-session *.e2b.app hostname.
    allowedHosts: [".e2b.app"],
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
