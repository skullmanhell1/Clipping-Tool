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

// --------------------------------------------------------------------------
// Timeouts and cancellation.
//
// Every call goes through `apiFetch`, so the header, the timeout and the abort
// plumbing each have one definition rather than 38.
//
// `fetch` has no timeout of its own. Without one, a request to a backend that
// has stopped answering — a container being restarted, a laptop that has
// suspended, a proxy holding the connection open — never settles, so the
// caller's `await` never returns. Every poll in the app was written to survive
// a *failure*; none of them survives a promise that simply never resolves,
// which is the case that presents as the UI freezing rather than erroring.
// --------------------------------------------------------------------------

//: Default request deadline. Generous, because the slowest ordinary call is a
//: yt-dlp metadata fetch on `/api/preview` and that legitimately takes seconds
//: on a cold extractor.
const DEFAULT_TIMEOUT_MS = 30000;

//: Uploads get no timeout at all.
//:
//: A multi-gigabyte file over a slow connection is *supposed* to take minutes,
//: and there is no duration that distinguishes "still uploading" from "stalled"
//: without measuring throughput. Cancelling one on a clock would abort exactly
//: the transfers that were closest to succeeding. Uploads are cancellable
//: instead — pass a `signal`.
const NO_TIMEOUT = 0;

/**
 * A DOMException-compatible error, distinguishable from a network failure.
 *
 * `fetch` rejects with a bare `TypeError` when the network is unreachable and
 * with an `AbortError` when a signal fires, and callers need to tell a timeout
 * apart from a user-initiated cancellation — one is worth reporting and the
 * other is not.
 */
export class TimeoutError extends Error {
  constructor(url, ms) {
    super(`Request to ${url} timed out after ${ms}ms`);
    this.name = "TimeoutError";
  }
}

function apiFetch(url, init = {}) {
  const { timeout = DEFAULT_TIMEOUT_MS, signal, ...rest } = init;

  const headers = { ...(rest.headers || {}), ...authHeaders() };

  // No timeout and no caller signal: nothing to wire up, so do not allocate a
  // controller. This is the upload path.
  if (!timeout && !signal) {
    return fetch(url, { ...rest, headers });
  }

  const controller = new AbortController();
  let timer = null;
  let timedOut = false;

  if (timeout) {
    timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeout);
  }

  // A caller's own signal (a component unmounting, a filter changing) is
  // forwarded rather than replacing ours, so both can abort the same request.
  const onCallerAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", onCallerAbort, { once: true });
  }

  return fetch(url, { ...rest, headers, signal: controller.signal })
    .catch((error) => {
      // Re-label our own abort so a timeout does not look like a cancellation.
      // A caller's abort keeps its AbortError, which callers check for by name.
      if (timedOut && error?.name === "AbortError") {
        throw new TimeoutError(url, timeout);
      }
      throw error;
    })
    .finally(() => {
      if (timer) clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onCallerAbort);
    });
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

  upload: (files, options, { signal } = {}) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    Object.entries(options).forEach(([k, v]) => {
      if (v !== null && v !== undefined) fd.append(k, v);
    });
    // No timeout: see NO_TIMEOUT. `signal` is threaded so an upload can still be
    // cancelled deliberately, which is what makes a slow one recoverable.
    return apiFetch("/api/upload", {
      method: "POST",
      body: fd,
      timeout: NO_TIMEOUT,
      signal,
    }).then(jsonOrThrow);
  },

  listJobs: () => apiFetch("/api/jobs").then(jsonOrThrow),
  getJob: (id) => apiFetch(`/api/jobs/${id}`).then(jsonOrThrow),

  /**
   * Stream job progress from `GET /api/jobs/events` (Phase 5.5).
   *
   * Resolves when the server closes the stream, rejects if it cannot be opened
   * or dies mid-flight. It does not reconnect — the caller decides whether a
   * dropped stream means retry or fall back to polling, because only the caller
   * knows whether it still cares.
   *
   * `fetch` rather than `EventSource`, and that is the whole reason this is
   * hand-rolled instead of three lines. `EventSource` cannot set a request
   * header, so authenticating it would mean putting the API token in the query
   * string — and unlike the `<video src>` case that allowance exists for, this
   * connection stays open for an entire render, so the token would sit in every
   * access log and proxy log for as long as the tab did. `fetch` sends the
   * header normally, at the cost of parsing the frames here.
   *
   * @param {object} handlers
   * @param {(jobs: object[]) => void} handlers.onSnapshot Full authoritative list.
   * @param {(jobs: object[]) => void} handlers.onJobs Only the changed jobs; merge by id.
   * @param {AbortSignal} [handlers.signal] Closes the stream.
   */
  jobEvents: async ({ onSnapshot, onJobs, signal } = {}) => {
    // NO_TIMEOUT for the same reason uploads use it: the request is *supposed*
    // to stay open indefinitely, so the 30s default deadline would abort a
    // perfectly healthy stream mid-render. Liveness is the server's heartbeat's
    // job, not a deadline's.
    const response = await apiFetch("/api/jobs/events", {
      headers: { Accept: "text/event-stream" },
      timeout: NO_TIMEOUT,
      signal,
    });
    if (!response.ok) {
      throw new Error(`Event stream failed (${response.status})`);
    }
    // Guarded rather than assumed: `response.body` is absent in environments
    // without streaming fetch. Throwing here is what lets the caller fall back
    // to polling instead of silently receiving no updates forever.
    if (!response.body?.getReader) {
      throw new Error("Event stream unsupported: no readable response body");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        // `stream: true` matters: a chunk boundary can fall inside a multi-byte
        // UTF-8 sequence, and decoding each chunk independently would corrupt
        // any non-ASCII character that straddled one — clip titles and
        // transcripts are exactly where that shows up.
        buffer += decoder.decode(value, { stream: true });

        // Frames are separated by a blank line. Anything after the last
        // separator is a partial frame and stays in the buffer.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const trimmed = frame.trim();
          // A comment frame — the server's heartbeat. Nothing to deliver, but
          // arriving at all is the signal that the connection is alive.
          if (!trimmed || trimmed.startsWith(":")) continue;
          let name = null;
          let data = null;
          for (const line of trimmed.split("\n")) {
            if (line.startsWith("event:")) name = line.slice(6).trim();
            else if (line.startsWith("data:")) data = line.slice(5).trim();
          }
          if (!name || !data) continue;
          let payload;
          try {
            payload = JSON.parse(data);
          } catch {
            // One malformed frame must not kill a stream that is otherwise
            // fine; the next update supersedes it anyway.
            continue;
          }
          const jobs = payload.jobs || [];
          if (name === "snapshot") onSnapshot?.(jobs);
          else if (name === "jobs") onJobs?.(jobs);
        }
      }
    } finally {
      // Releasing the lock lets the body be cancelled by the abort signal
      // rather than leaving a reader attached to a dead stream.
      reader.releaseLock?.();
    }
  },

  // I4: ask a queued or running job to stop. Answers 409 when the job has already finished,
  // which jsonOrThrow surfaces as an error carrying the API's own explanation.
  cancelJob: (id) => apiFetch(`/api/jobs/${id}/cancel`, { method: "POST" }).then(jsonOrThrow),

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
  downloadUrl: (jobId, filename) => withToken(`/api/clips/${jobId}/${filename}/download`),
  videoDownloadUrl: (jobId, filename) => withToken(`/api/clips/${jobId}/${filename}/video`),

  publisherStatuses: () => apiFetch("/api/publishers").then(jsonOrThrow),
  campaigns: () => apiFetch("/api/campaigns").then(jsonOrThrow),
  saveCampaign: (name, routes, id = "") =>
    apiFetch("/api/campaigns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, routes, id }),
    }).then(jsonOrThrow),
  publishClip: (jobId, clipId, payload) =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}/publish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(jsonOrThrow),
  history: (platform = "") =>
    apiFetch(`/api/history${platform ? `?platform=${encodeURIComponent(platform)}` : ""}`).then(
      jsonOrThrow
    ),

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
        `&days=${days}&per_day=${perDay}`
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
  resumeJob: (jobId) => apiFetch(`/api/jobs/${jobId}/resume`, { method: "POST" }).then(jsonOrThrow),

  // U7: re-render one clip with changed settings, instead of resubmitting the whole source.
  // Resubmitting re-downloads, re-transcribes, re-selects and re-renders every other clip — and
  // because selection is not deterministic with an LLM in it, you get a *different set* of clips
  // rather than the same one restyled.
  //
  // U4: `cuts` are clip-relative ranges to remove, from the transcript editor. Sent as its own
  // key rather than folded into `settings`, because the backend filters `settings` against the
  // options it knows and drops the rest silently — a cut list sent that way would vanish without
  // an error, on a destructive edit the user is watching for.
  //
  // `apiFetch`, not `fetch`: this call and `clipTranscript` were both written before the shared
  // secret existed, and a text-only merge keeps a bare `fetch` quite happily — the calls would
  // then 401 and the editor would report "could not load the transcript" with nothing pointing
  // at auth.
  rerenderClip: (jobId, clipId, settings = {}, cuts = []) =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}/rerender`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings, cuts }),
    }).then(jsonOrThrow),

  // U4: word-level timings for one clip, for the transcript editor. Answers 409 when the
  // transcript is not in the cache the render used — the editor cannot be opened in that case,
  // and jsonOrThrow surfaces the API's own explanation of why.
  clipTranscript: (jobId, clipId) =>
    apiFetch(`/api/jobs/${jobId}/clips/${clipId}/transcript`).then(jsonOrThrow),

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
  deleteProfile: (id) => apiFetch(`/api/profiles/${id}`, { method: "DELETE" }).then(jsonOrThrow),

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
