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
  },
});
