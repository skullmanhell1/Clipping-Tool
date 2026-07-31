// Thin API client for the clipping backend.
// In dev, Vite proxies /api, /clips and /healthz to the FastAPI server
// (see vite.config.js). In production the SPA is served by FastAPI itself,
// so relative paths work in both cases.

// --------------------------------------------------------------------------
// The shared secret (API_AUTH_TOKEN on the server).
//
// Read from localStorage first so it can be set without rebuilding the bundle:
//
//     localStorage.setItem("clipper_token", "<the value of API_AUTH_TOKEN>")
//
// VITE_API_TOKEN is the build-time fallback for a self-hosted deployment that
// bakes it in. Both are readable by anyone with the page open, which is fine
// for what this is — a single shared secret for a single-tenant tool, where the
// operator is the only user. It is NOT a per-user credential; per-user auth is
// plan item U12.
//
// When the server has no token configured it ignores the header entirely, so
// sending nothing is the correct default and needs no configuration.
// --------------------------------------------------------------------------
function authToken() {
  try {
    const stored = window.localStorage?.getItem("clipper_token");
    if (stored) return stored;
  } catch {
    // localStorage throws in a sandboxed iframe or with site data blocked.
    // Falling through to the build-time value is better than failing to load.
  }
  return import.meta.env?.VITE_API_TOKEN || "";
}

function authHeaders() {
  const token = authToken();
  return token ? { "X-API-Token": token } : {};
}

// Every call goes through here so the header has one definition rather than 38.
// That single seam is also where Phase 5 adds AbortController and timeouts.
function apiFetch(url, init = {}) {
  return fetch(url, { ...init, headers: { ...(init.headers || {}), ...authHeaders() } });
}

// A URL a *browser* will load directly (<video src>, <a href>) cannot carry a
// header, so the token goes in the query string for those. The server accepts it
// there only for GET requests to read-only media paths — see
// api/security.py::_QUERY_TOKEN_PATHS.
function withToken(url) {
  const token = authToken();
  if (!token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

async function jsonOrThrow(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (${res.status})`);
  }
  return data;
}

export const api = {
  info: () => apiFetch("/api/info").then(jsonOrThrow),

  preview: (url) =>
    apiFetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }).then(jsonOrThrow),

  submitUrl: (url, options) =>
    apiFetch("/api/jobs/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, options }),
    }).then(jsonOrThrow),

  submitBatch: (urls, options) =>
    apiFetch("/api/jobs/batch", {
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
    return apiFetch("/api/upload", { method: "POST", body: fd }).then(jsonOrThrow);
  },

  listJobs: () => apiFetch("/api/jobs").then(jsonOrThrow),
  getJob: (id) => apiFetch(`/api/jobs/${id}`).then(jsonOrThrow),

  // I4: ask a queued or running job to stop. Answers 409 when the job has already finished,
  // which jsonOrThrow surfaces as an error carrying the API's own explanation.
  cancelJob: (id) =>
    apiFetch(`/api/jobs/${id}/cancel`, { method: "POST" }).then(jsonOrThrow),

  // M5: per-stage render timings for a finished (or running) job.
  jobTimings: (id) => apiFetch(`/api/jobs/${id}/timings`).then(jsonOrThrow),

  editClip: (jobId, clipId, fields) =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    }).then(jsonOrThrow),

  regenerateField: (jobId, clipId, field, platform) =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field, platform }),
    }).then(jsonOrThrow),

  watchStatus: () => apiFetch("/api/watch").then(jsonOrThrow),
  watchToggle: (enabled, options) =>
    apiFetch("/api/watch/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, options }),
    }).then(jsonOrThrow),

  // Pass absolute URLs (e.g. S3 presigned) through unchanged; otherwise the
  // value is an app-relative clip path that the server serves under /clips.
  clipUrl: (relativeUrl) => {
    if (!relativeUrl) return "";
    if (/^https?:\/\//i.test(relativeUrl)) return relativeUrl;
    return withToken(relativeUrl.startsWith("/") ? relativeUrl : `/${relativeUrl}`);
  },
  downloadUrl: (jobId, filename) =>
    withToken(`/api/clips/${jobId}/${filename}/download`),
  videoDownloadUrl: (jobId, filename) =>
    withToken(`/api/clips/${jobId}/${filename}/video`),

  publisherStatuses: () => apiFetch("/api/publishers").then(jsonOrThrow),
  campaigns: () => apiFetch("/api/campaigns").then(jsonOrThrow),
  saveCampaign: (name, routes, id = "") =>
    apiFetch("/api/campaigns", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, routes, id }) }).then(jsonOrThrow),
  publishClip: (jobId, clipId, payload) =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}/publish`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(jsonOrThrow),
  history: (platform = "") =>
    apiFetch(`/api/history${platform ? `?platform=${encodeURIComponent(platform)}` : ""}`).then(jsonOrThrow),

  // PB2: the approve/retry endpoints existed but nothing in the UI referenced them, so an
  // attempt that came back `review_required` stopped permanently — three of the five
  // publishers can return that state, and the dashboard offered no way to act on it.
  //
  // Approve escalates a review-mode attempt to a direct publish; retry re-runs it as it was.
  // They are deliberately separate calls rather than one "resume": a retry must never
  // silently turn a submission that was queued for review into a live post.
  approvePublishAttempt: (attemptId) =>
    apiFetch(`/api/publish-attempts/${attemptId}/approve`, { method: "POST" }).then(jsonOrThrow),
  retryPublishAttempt: (attemptId) =>
    apiFetch(`/api/publish-attempts/${attemptId}/retry`, { method: "POST" }).then(jsonOrThrow),

  // PB7: scheduling. A scheduled post could not be seen in context, moved, or cancelled — the
  // time was fixed when the attempt was created and the only recourse was to let it publish.
  schedule: (start, end) => {
    const params = new URLSearchParams();
    if (start != null) params.set("start", String(start));
    if (end != null) params.set("end", String(end));
    const query = params.toString();
    return apiFetch(`/api/schedule${query ? `?${query}` : ""}`).then(jsonOrThrow);
  },
  // Suggestions carry a `basis` string describing where they come from. Render it: they are
  // published heuristics, not measurements of this account's audience, and presenting a guess
  // as an analysis is the actual harm available here.
  scheduleSuggestions: (platform = "", days = 7, perDay = 2) =>
    apiFetch(
      `/api/schedule/suggestions?platform=${encodeURIComponent(platform)}` +
        `&days=${days}&per_day=${perDay}`,
    ).then(jsonOrThrow),
  reschedulePublishAttempt: (attemptId, scheduleAt) =>
    apiFetch(`/api/publish-attempts/${attemptId}/schedule`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schedule_at: scheduleAt }),
    }).then(jsonOrThrow),
  cancelPublishAttempt: (attemptId) =>
    apiFetch(`/api/publish-attempts/${attemptId}/cancel`, { method: "POST" }).then(jsonOrThrow),
  // I5: render a failed job's unfinished clips, keeping the ones it already produced. An
  // interrupted job used to be marked failed wholesale, so the only way forward was to re-submit
  // the source and pay for every clip again — including the ones that had succeeded.
  resumeJob: (jobId) =>
    apiFetch(`/api/jobs/${jobId}/resume`, { method: "POST" }).then(jsonOrThrow),

  // U7: re-render one clip with changed settings, instead of resubmitting the whole source.
  // Resubmitting re-downloads, re-transcribes, re-selects and re-renders every other clip — and
  // because selection is not deterministic with an LLM in it, you get a *different set* of clips
  // rather than the same one restyled.
  rerenderClip: (jobId, clipId, settings = {}) =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}/rerender`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings }),
    }).then(jsonOrThrow),

  // U9: record a verdict on a clip, one at a time or many at once. Without this a review pass
  // over twenty clips left no trace and had to be redone from the top after any interruption.
  reviewClip: (jobId, clipId, reviewState, reviewNote = "") =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_state: reviewState, review_note: reviewNote }),
    }).then(jsonOrThrow),
  reviewClips: (jobId, clipIds, reviewState, reviewNote = "") =>
    apiFetch(`/api/jobs/${jobId}/clips/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clip_ids: clipIds,
        review_state: reviewState,
        review_note: reviewNote,
      }),
    }).then(jsonOrThrow),

  refreshPublisherCredentials: (platform) =>
    apiFetch(`/api/publishers/${encodeURIComponent(platform)}/refresh`, {
      method: "POST",
    }).then(jsonOrThrow),

  // --- Phase 5: storage, profiles, updates ---
  storage: () => apiFetch("/api/storage").then(jsonOrThrow),
  updateStorageSettings: (settings) =>
    apiFetch("/api/storage/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }).then(jsonOrThrow),
  cleanupStorage: (opts = {}) => {
    const params = new URLSearchParams(opts).toString();
    return apiFetch(`/api/storage/cleanup${params ? `?${params}` : ""}`, {
      method: "POST",
    }).then(jsonOrThrow);
  },
  deleteSource: (jobId) =>
    apiFetch(`/api/jobs/${jobId}/source?confirm=true`, { method: "DELETE" }).then(jsonOrThrow),

  profiles: () => apiFetch("/api/profiles").then(jsonOrThrow),
  saveProfile: (payload) =>
    apiFetch("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(jsonOrThrow),
  setDefaultProfile: (id) =>
    apiFetch(`/api/profiles/${id}/default`, { method: "POST" }).then(jsonOrThrow),
  deleteProfile: (id) =>
    apiFetch(`/api/profiles/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  updates: (force = false) =>
    apiFetch(`/api/updates${force ? "?force=true" : ""}`).then(jsonOrThrow),
};

export function formatBytes(bytes) {
  if (!bytes || bytes < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

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
