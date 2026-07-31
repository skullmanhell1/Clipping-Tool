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

  // I4: ask a queued or running job to stop. Answers 409 when the job has already finished,
  // which jsonOrThrow surfaces as an error carrying the API's own explanation.
  cancelJob: (id) =>
    fetch(`/api/jobs/${id}/cancel`, { method: "POST" }).then(jsonOrThrow),

  // M5: per-stage render timings for a finished (or running) job.
  jobTimings: (id) => fetch(`/api/jobs/${id}/timings`).then(jsonOrThrow),

  editClip: (jobId, clipId, fields) =>
    fetch(`/api/jobs/${jobId}/clips/${clipId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    }).then(jsonOrThrow),

  regenerateField: (jobId, clipId, field, platform) =>
    fetch(`/api/jobs/${jobId}/clips/${clipId}/regenerate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field, platform }),
    }).then(jsonOrThrow),

  watchStatus: () => fetch("/api/watch").then(jsonOrThrow),
  watchToggle: (enabled, options) =>
    fetch("/api/watch/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, options }),
    }).then(jsonOrThrow),

  // Pass absolute URLs (e.g. S3 presigned) through unchanged; otherwise the
  // value is an app-relative clip path that the server serves under /clips.
  clipUrl: (relativeUrl) => {
    if (!relativeUrl) return "";
    if (/^https?:\/\//i.test(relativeUrl)) return relativeUrl;
    return relativeUrl.startsWith("/") ? relativeUrl : `/${relativeUrl}`;
  },
  downloadUrl: (jobId, filename) =>
    `/api/clips/${jobId}/${filename}/download`,
  videoDownloadUrl: (jobId, filename) =>
    `/api/clips/${jobId}/${filename}/video`,

  publisherStatuses: () => fetch("/api/publishers").then(jsonOrThrow),
  campaigns: () => fetch("/api/campaigns").then(jsonOrThrow),
  saveCampaign: (name, routes, id = "") =>
    fetch("/api/campaigns", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, routes, id }) }).then(jsonOrThrow),
  publishClip: (jobId, clipId, payload) =>
    fetch(`/api/jobs/${jobId}/clips/${clipId}/publish`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(jsonOrThrow),
  history: (platform = "") =>
    fetch(`/api/history${platform ? `?platform=${encodeURIComponent(platform)}` : ""}`).then(jsonOrThrow),

  // PB2: the approve/retry endpoints existed but nothing in the UI referenced them, so an
  // attempt that came back `review_required` stopped permanently — three of the five
  // publishers can return that state, and the dashboard offered no way to act on it.
  //
  // Approve escalates a review-mode attempt to a direct publish; retry re-runs it as it was.
  // They are deliberately separate calls rather than one "resume": a retry must never
  // silently turn a submission that was queued for review into a live post.
  approvePublishAttempt: (attemptId) =>
    fetch(`/api/publish-attempts/${attemptId}/approve`, { method: "POST" }).then(jsonOrThrow),
  retryPublishAttempt: (attemptId) =>
    fetch(`/api/publish-attempts/${attemptId}/retry`, { method: "POST" }).then(jsonOrThrow),

  // PB7: scheduling. A scheduled post could not be seen in context, moved, or cancelled — the
  // time was fixed when the attempt was created and the only recourse was to let it publish.
  schedule: (start, end) => {
    const params = new URLSearchParams();
    if (start != null) params.set("start", String(start));
    if (end != null) params.set("end", String(end));
    const query = params.toString();
    return fetch(`/api/schedule${query ? `?${query}` : ""}`).then(jsonOrThrow);
  },
  // Suggestions carry a `basis` string describing where they come from. Render it: they are
  // published heuristics, not measurements of this account's audience, and presenting a guess
  // as an analysis is the actual harm available here.
  scheduleSuggestions: (platform = "", days = 7, perDay = 2) =>
    fetch(
      `/api/schedule/suggestions?platform=${encodeURIComponent(platform)}` +
        `&days=${days}&per_day=${perDay}`,
    ).then(jsonOrThrow),
  reschedulePublishAttempt: (attemptId, scheduleAt) =>
    fetch(`/api/publish-attempts/${attemptId}/schedule`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schedule_at: scheduleAt }),
    }).then(jsonOrThrow),
  cancelPublishAttempt: (attemptId) =>
    fetch(`/api/publish-attempts/${attemptId}/cancel`, { method: "POST" }).then(jsonOrThrow),
  // I5: render a failed job's unfinished clips, keeping the ones it already produced. An
  // interrupted job used to be marked failed wholesale, so the only way forward was to re-submit
  // the source and pay for every clip again — including the ones that had succeeded.
  resumeJob: (jobId) =>
    fetch(`/api/jobs/${jobId}/resume`, { method: "POST" }).then(jsonOrThrow),

  // U7: re-render one clip with changed settings, instead of resubmitting the whole source.
  // Resubmitting re-downloads, re-transcribes, re-selects and re-renders every other clip — and
  // because selection is not deterministic with an LLM in it, you get a *different set* of clips
  // rather than the same one restyled.
  //
  // U4: `cuts` are clip-relative ranges to remove, from the transcript editor. Sent as its own
  // key rather than folded into `settings`, because the backend filters `settings` against the
  // options it knows and drops the rest silently — a cut list sent that way would vanish without
  // an error, on a destructive edit the user is watching for.
  rerenderClip: (jobId, clipId, settings = {}, cuts = []) =>
    fetch(`/api/jobs/${jobId}/clips/${clipId}/rerender`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings, cuts }),
    }).then(jsonOrThrow),

  // U4: word-level timings for one clip, for the transcript editor. Answers 409 when the
  // transcript is not in the cache the render used — the editor cannot be opened in that case,
  // and jsonOrThrow surfaces the API's own explanation of why.
  clipTranscript: (jobId, clipId) =>
    fetch(`/api/jobs/${jobId}/clips/${clipId}/transcript`).then(jsonOrThrow),

  // U9: record a verdict on a clip, one at a time or many at once. Without this a review pass
  // over twenty clips left no trace and had to be redone from the top after any interruption.
  reviewClip: (jobId, clipId, reviewState, reviewNote = "") =>
    fetch(`/api/jobs/${jobId}/clips/${clipId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_state: reviewState, review_note: reviewNote }),
    }).then(jsonOrThrow),
  reviewClips: (jobId, clipIds, reviewState, reviewNote = "") =>
    fetch(`/api/jobs/${jobId}/clips/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clip_ids: clipIds,
        review_state: reviewState,
        review_note: reviewNote,
      }),
    }).then(jsonOrThrow),

  refreshPublisherCredentials: (platform) =>
    fetch(`/api/publishers/${encodeURIComponent(platform)}/refresh`, {
      method: "POST",
    }).then(jsonOrThrow),

  // --- Phase 5: storage, profiles, updates ---
  storage: () => fetch("/api/storage").then(jsonOrThrow),
  updateStorageSettings: (settings) =>
    fetch("/api/storage/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }).then(jsonOrThrow),
  cleanupStorage: (opts = {}) => {
    const params = new URLSearchParams(opts).toString();
    return fetch(`/api/storage/cleanup${params ? `?${params}` : ""}`, {
      method: "POST",
    }).then(jsonOrThrow);
  },
  deleteSource: (jobId) =>
    fetch(`/api/jobs/${jobId}/source?confirm=true`, { method: "DELETE" }).then(jsonOrThrow),

  profiles: () => fetch("/api/profiles").then(jsonOrThrow),
  saveProfile: (payload) =>
    fetch("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(jsonOrThrow),
  setDefaultProfile: (id) =>
    fetch(`/api/profiles/${id}/default`, { method: "POST" }).then(jsonOrThrow),
  deleteProfile: (id) =>
    fetch(`/api/profiles/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  updates: (force = false) =>
    fetch(`/api/updates${force ? "?force=true" : ""}`).then(jsonOrThrow),

  // --- U12: authentication ---
  //
  // No token is stored or sent by this client on purpose. The session lives in an httpOnly
  // cookie the browser attaches automatically, which is what keeps it out of reach of any
  // XSS bug in this bundle. Putting it in localStorage would make every one of these calls
  // one line shorter and the token readable by injected script.
  //
  // `authConfig` is the one call that must work while signed out: without it the SPA cannot
  // tell "this deployment has no accounts" from "I am signed out", and a single-tenant
  // install would show a login form for an account system it does not have.
  authConfig: () => fetch("/api/auth/config").then(jsonOrThrow),
  authSession: () => fetch("/api/auth/session").then(jsonOrThrow),
  login: (username, password) =>
    fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }).then(jsonOrThrow),
  logout: () => fetch("/api/auth/logout", { method: "POST" }).then(jsonOrThrow),
  changePassword: (currentPassword, newPassword) =>
    fetch("/api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    }).then(jsonOrThrow),
  listUsers: () => fetch("/api/users").then(jsonOrThrow),
  createUser: (username, password, isAdmin = false) =>
    fetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, is_admin: isAdmin }),
    }).then(jsonOrThrow),
  setUserDisabled: (userId, disabled) =>
    fetch(`/api/users/${encodeURIComponent(userId)}/disabled`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disabled }),
    }).then(jsonOrThrow),
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
