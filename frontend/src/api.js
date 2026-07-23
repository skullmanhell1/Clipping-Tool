// Thin API client for the clipping backend.
// In dev, Vite proxies /api, /clips and /healthz to the FastAPI server
// (see vite.config.js). In production the SPA is served by FastAPI itself,
// so relative paths work in both cases.

async function jsonOrThrow(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (${res.status})`);
  }
  return data;
}

export const api = {
  info: () => fetch("/api/info").then(jsonOrThrow),

  preview: (url) =>
    fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }).then(jsonOrThrow),

  submitUrl: (url, options) =>
    fetch("/api/jobs/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, options }),
    }).then(jsonOrThrow),

  submitBatch: (urls, options) =>
    fetch("/api/jobs/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, options }),
    }).then(jsonOrThrow),

  upload: (files, options) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    Object.entries(options).forEach(([k, v]) => {
      if (v !== null && v !== undefined) fd.append(k, v);
    });
    return fetch("/api/upload", { method: "POST", body: fd }).then(jsonOrThrow);
  },

  listJobs: () => fetch("/api/jobs").then(jsonOrThrow),
  getJob: (id) => fetch(`/api/jobs/${id}`).then(jsonOrThrow),

  watchStatus: () => fetch("/api/watch").then(jsonOrThrow),
  watchToggle: (enabled, options) =>
    fetch("/api/watch/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, options }),
    }).then(jsonOrThrow),

  clipUrl: (relativeUrl) => `/${relativeUrl}`,
  downloadUrl: (jobId, filename) =>
    `/api/clips/${jobId}/${filename}/download`,
};

// Map the UI Language dropdown value to backend {language, translate}.
export function resolveLanguage(value) {
  if (value === "auto") return { language: null, translate: false };
  if (value === "translate") return { language: null, translate: true };
  return { language: value, translate: false };
}

export function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "--:--";
  const s = Math.round(seconds);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}
