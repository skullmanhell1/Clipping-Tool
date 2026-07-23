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
});
