import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite configuration for the React frontend.
// The dev server proxies `/api` and `/healthz` to the FastAPI backend so the
// SPA and API can be developed side by side.
export default defineConfig({
  plugins: [react()],
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
