// Tests for the API client's pure helpers and its error handling.
//
// The frontend had no tests at all. These target the logic most likely to be wrong and
// least likely to be noticed: URL normalisation (a wrong result yields a broken video
// player rather than an error) and error surfacing (a swallowed detail leaves the user
// with a generic failure message).
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, formatBytes, formatDuration, resolveLanguage } from "./api.js";

describe("clipUrl", () => {
  it("passes absolute URLs through untouched", () => {
    // S3 presigned URLs must not be rewritten; prefixing a slash would break them and
    // the signature cannot be reconstructed.
    const presigned = "https://bucket.s3.amazonaws.com/clip.mp4?X-Amz-Signature=abc";
    expect(api.clipUrl(presigned)).toBe(presigned);
    expect(api.clipUrl("http://example.com/clip.mp4")).toBe("http://example.com/clip.mp4");
  });

  it("is case-insensitive about the scheme", () => {
    expect(api.clipUrl("HTTPS://cdn.example.com/a.mp4")).toBe("HTTPS://cdn.example.com/a.mp4");
  });

  it("makes a relative clip path root-absolute", () => {
    expect(api.clipUrl("clips/job1/clip.mp4")).toBe("/clips/job1/clip.mp4");
  });

  it("does not double the leading slash", () => {
    expect(api.clipUrl("/clips/job1/clip.mp4")).toBe("/clips/job1/clip.mp4");
  });

  it("returns an empty string for empty input", () => {
    // An <video src=""> is inert, whereas src="/undefined" would fire a failed request.
    expect(api.clipUrl("")).toBe("");
    expect(api.clipUrl(undefined)).toBe("");
    expect(api.clipUrl(null)).toBe("");
  });
});

describe("resolveLanguage", () => {
  it("maps auto to detection with no translation", () => {
    expect(resolveLanguage("auto")).toEqual({ language: null, translate: false });
  });

  it("maps translate to detection *with* translation", () => {
    // The distinction matters: "translate" must not be sent as a language code.
    expect(resolveLanguage("translate")).toEqual({ language: null, translate: true });
  });

  it("passes an explicit language code through", () => {
    expect(resolveLanguage("es")).toEqual({ language: "es", translate: false });
  });
});

describe("formatBytes", () => {
  it("scales through the unit table", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
    expect(formatBytes(5 * 1024 * 1024 * 1024)).toBe("5.0 GB");
  });

  it("drops the decimal at 10 units and above", () => {
    expect(formatBytes(10 * 1024)).toBe("10 KB");
  });

  it("treats missing or negative sizes as zero", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(undefined)).toBe("0 B");
    expect(formatBytes(-5)).toBe("0 B");
  });
});

describe("formatDuration", () => {
  it("formats as m:ss with a padded seconds field", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(9)).toBe("0:09");
    expect(formatDuration(61)).toBe("1:01");
    expect(formatDuration(600)).toBe("10:00");
  });

  it("renders a placeholder when the duration is unknown", () => {
    // A job that has not been probed yet has no duration; "0:00" would be a lie.
    expect(formatDuration(undefined)).toBe("--:--");
    expect(formatDuration(null)).toBe("--:--");
  });
});

describe("response handling", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the parsed body on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ version: "0.9.0" }),
      })
    );
    await expect(api.info()).resolves.toEqual({ version: "0.9.0" });
  });

  it("surfaces the server's detail message on failure", async () => {
    // FastAPI puts the human-readable reason in `detail`. Losing it would replace a
    // message like "Clip file no longer exists" with an opaque status code.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({ detail: "instagram cannot publish directly yet" }),
      })
    );
    await expect(api.info()).rejects.toThrow("instagram cannot publish directly yet");
  });

  it("falls back to the status code when the body is not JSON", async () => {
    // A 502 from a proxy returns HTML, not JSON, so json() rejects. The client must
    // still produce a usable error rather than an unhandled parse failure.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        json: async () => {
          throw new Error("not json");
        },
      })
    );
    await expect(api.info()).rejects.toThrow("Request failed (502)");
  });
});

describe("upload", () => {
  it("omits null and undefined options from the form data", async () => {
    // The backend reads these as form fields; sending the string "null" for an absent
    // language would be parsed as a language code rather than as "auto-detect".
    let captured;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((_url, init) => {
        captured = init.body;
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
      })
    );

    const file = new File([new Uint8Array([1, 2, 3])], "clip.mp4", {
      type: "video/mp4",
    });
    await api.upload([file], { aspect: "9:16", language: null, topic: undefined });

    expect(captured.get("aspect")).toBe("9:16");
    expect(captured.has("language")).toBe(false);
    expect(captured.has("topic")).toBe(false);
    expect(captured.getAll("files")).toHaveLength(1);
  });
});

describe("transcript trimming requests (U4)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  const stubOk = () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  };

  it("sends the cut list as its own key, not inside settings", async () => {
    // `settings` is filtered against the options the backend knows and unknown keys are
    // dropped in silence, so a cut list smuggled in there would vanish without an error.
    const fetchMock = stubOk();
    await api.rerenderClip("job1", "c1", { zoom: true }, [{ start: 1, end: 2 }]);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/job1/clips/c1/rerender");
    expect(JSON.parse(init.body)).toEqual({
      settings: { zoom: true },
      cuts: [{ start: 1, end: 2 }],
    });
  });

  it("sends an empty cut list when none is given", async () => {
    // U7's plain re-render button goes through the same call and must not trim anything.
    const fetchMock = stubOk();
    await api.rerenderClip("job1", "c1", { zoom: true });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      settings: { zoom: true },
      cuts: [],
    });
  });

  it("reads a clip transcript with a plain GET", async () => {
    const fetchMock = stubOk();
    await api.clipTranscript("job1", "c1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs/job1/clips/c1/transcript");
  });

  it("surfaces the reason a transcript is unavailable", async () => {
    // A 409 here is a normal outcome, and the API's own wording is what explains it.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({ detail: "No cached transcript for this clip." }),
      })
    );
    await expect(api.clipTranscript("job1", "c1")).rejects.toThrow(/no cached transcript/i);
  });

  it("sends the auth token on both U4 calls", async () => {
    // Merge guard. Both calls were written before the shared secret existed and used bare
    // `fetch`, which a text merge keeps happily: they would 401, the editor would say it could
    // not load the transcript, and nothing would point at auth.
    window.localStorage.setItem("clipper_token", "s3cret");
    try {
      const fetchMock = stubOk();
      await api.clipTranscript("job1", "c1");
      await api.rerenderClip("job1", "c1", {}, [{ start: 1, end: 2 }]);
      expect(fetchMock.mock.calls).toHaveLength(2);
      for (const call of fetchMock.mock.calls) {
        expect(call[1].headers["X-API-Token"]).toBe("s3cret");
      }
    } finally {
      window.localStorage.clear();
    }
  });
});

// ---------------------------------------------------------------------------
// The shared secret (Phase 1 security)
//
// The server accepts the token as a header everywhere, and as `?token=` only for GET
// requests to read-only media paths. The frontend has to get that split right in both
// directions: a missing header means every call 401s, and a token appended to the wrong
// URL puts a credential somewhere it should never be.
//
// These matter because the failure is silent in the UI. A 401 on a poll loop surfaces as
// "no jobs", and a video that 401s surfaces as a blank player.
// ---------------------------------------------------------------------------
describe("auth token", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("sends no auth header when no token is set", async () => {
    // The default. An unconfigured server ignores the header, so sending nothing must work
    // rather than being an error state.
    const spy = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", spy);
    await api.listJobs();
    const headers = spy.mock.calls[0][1]?.headers || {};
    expect(headers["X-API-Token"]).toBeUndefined();
  });

  it("sends the token from localStorage on API calls", async () => {
    window.localStorage.setItem("clipper_token", "s3cret");
    const spy = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", spy);
    await api.listJobs();
    expect(spy.mock.calls[0][1].headers["X-API-Token"]).toBe("s3cret");
  });

  it("keeps the caller's own headers alongside the token", async () => {
    // Every POST sets Content-Type; dropping it would make the body unparseable and the
    // request fail as a validation error rather than as an auth one.
    window.localStorage.setItem("clipper_token", "s3cret");
    const spy = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", spy);
    await api.submitUrl("https://example.com/a.mp4", {});
    const headers = spy.mock.calls[0][1].headers;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers["X-API-Token"]).toBe("s3cret");
  });

  it("appends the token to browser-navigated media URLs", () => {
    // <video src>, poster and the two <a href> download links cannot carry a header.
    window.localStorage.setItem("clipper_token", "s3cret");
    expect(api.clipUrl("clips/j/c.mp4")).toBe("/clips/j/c.mp4?token=s3cret");
    expect(api.downloadUrl("j", "c.mp4")).toBe("/api/clips/j/c.mp4/download?token=s3cret");
    expect(api.videoDownloadUrl("j", "c.mp4")).toBe("/api/clips/j/c.mp4/video?token=s3cret");
  });

  it("uses & when the media URL already has a query string", () => {
    window.localStorage.setItem("clipper_token", "s3cret");
    expect(api.clipUrl("/clips/j/c.mp4?v=2")).toBe("/clips/j/c.mp4?v=2&token=s3cret");
  });

  it("url-encodes the token", () => {
    // A token with a + or & in it would otherwise be silently truncated or mangled.
    window.localStorage.setItem("clipper_token", "a+b&c=d");
    expect(api.clipUrl("/clips/j/c.mp4")).toBe("/clips/j/c.mp4?token=a%2Bb%26c%3Dd");
  });

  it("still passes absolute URLs through untouched", () => {
    // A presigned S3 URL carries its own signature; appending our token would both break
    // the signature and leak the secret to the storage provider.
    window.localStorage.setItem("clipper_token", "s3cret");
    const presigned = "https://bucket.s3.amazonaws.com/clip.mp4?X-Amz-Signature=abc";
    expect(api.clipUrl(presigned)).toBe(presigned);
  });

  it("adds no token to media URLs when none is configured", () => {
    expect(api.clipUrl("clips/j/c.mp4")).toBe("/clips/j/c.mp4");
    expect(api.downloadUrl("j", "c.mp4")).toBe("/api/clips/j/c.mp4/download");
  });
});

describe("jobEvents", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /**
   * Stub `fetch` with a streaming response that yields `chunks` in order.
   *
   * `chunks` are byte arrays rather than strings so a test can split a frame — or a single
   * multi-byte character — wherever it likes, which is the whole point of several of these.
   */
  const streamOf = (chunks, { ok = true, status = 200 } = {}) => {
    let index = 0;
    const body = {
      getReader: () => ({
        read: async () =>
          index < chunks.length ? { done: false, value: chunks[index++] } : { done: true },
        releaseLock: () => {},
      }),
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok, status, body }));
  };

  const utf8 = (text) => new TextEncoder().encode(text);

  it("delivers a snapshot and an incremental frame to the right handler", async () => {
    streamOf([
      utf8('event: snapshot\ndata: {"jobs":[{"id":"a"}]}\n\n'),
      utf8('event: jobs\ndata: {"jobs":[{"id":"b"}]}\n\n'),
    ]);
    const snapshots = [];
    const updates = [];
    await api.jobEvents({
      onSnapshot: (jobs) => snapshots.push(jobs),
      onJobs: (jobs) => updates.push(jobs),
    });
    expect(snapshots).toEqual([[{ id: "a" }]]);
    expect(updates).toEqual([[{ id: "b" }]]);
  });

  it("reassembles a frame split across chunk boundaries", async () => {
    // A frame is not a chunk. TCP decides where the split lands, so a reader that assumed one
    // frame per read would drop or corrupt updates under exactly the load this endpoint exists
    // for — a large job list is guaranteed to span reads.
    streamOf([
      utf8("event: snap"),
      utf8('shot\ndata: {"jobs":[{"id'),
      utf8('":"a","title":"Split"}]}\n'),
      utf8("\n"),
    ]);
    const snapshots = [];
    await api.jobEvents({ onSnapshot: (jobs) => snapshots.push(jobs) });
    expect(snapshots).toEqual([[{ id: "a", title: "Split" }]]);
  });

  it("does not corrupt a multi-byte character split across chunks", async () => {
    // This is why the decoder is called with `{ stream: true }`. Decoding each chunk
    // independently turns a UTF-8 sequence broken across a read into U+FFFD, and clip titles
    // are where non-ASCII actually appears.
    const payload = utf8('event: snapshot\ndata: {"jobs":[{"id":"a","title":"café ☕"}]}\n\n');
    // Split inside the multi-byte sequence for the coffee cup.
    const cut = payload.indexOf(0xe2) + 1;
    streamOf([payload.slice(0, cut), payload.slice(cut)]);
    const snapshots = [];
    await api.jobEvents({ onSnapshot: (jobs) => snapshots.push(jobs) });
    expect(snapshots[0][0].title).toBe("café ☕");
  });

  it("delivers several frames arriving in one chunk", async () => {
    streamOf([
      utf8(
        'event: jobs\ndata: {"jobs":[{"id":"a"}]}\n\nevent: jobs\ndata: {"jobs":[{"id":"b"}]}\n\n'
      ),
    ]);
    const updates = [];
    await api.jobEvents({ onJobs: (jobs) => updates.push(jobs) });
    expect(updates).toEqual([[{ id: "a" }], [{ id: "b" }]]);
  });

  it("ignores the heartbeat comment frame", async () => {
    // The server sends `: ping` to keep intermediaries from closing an idle stream. It carries
    // no data and must not reach a handler as an empty update, which would blank the job list.
    streamOf([utf8(': ping\n\nevent: jobs\ndata: {"jobs":[{"id":"a"}]}\n\n')]);
    const updates = [];
    await api.jobEvents({ onJobs: (jobs) => updates.push(jobs) });
    expect(updates).toEqual([[{ id: "a" }]]);
  });

  it("skips a malformed frame and keeps reading", async () => {
    // One truncated or non-JSON frame must not end a stream that is otherwise fine; the next
    // update supersedes it anyway.
    streamOf([
      utf8("event: jobs\ndata: {not json\n\n"),
      utf8('event: jobs\ndata: {"jobs":[{"id":"good"}]}\n\n'),
    ]);
    const updates = [];
    await api.jobEvents({ onJobs: (jobs) => updates.push(jobs) });
    expect(updates).toEqual([[{ id: "good" }]]);
  });

  it("rejects on a non-OK status instead of resolving silently", async () => {
    // A 401 must be an error the caller can act on. Resolving would look like a stream that
    // simply never reported anything, and the UI would sit at "queued" forever.
    streamOf([], { ok: false, status: 401 });
    await expect(api.jobEvents({})).rejects.toThrow("Event stream failed (401)");
  });

  it("rejects when the environment cannot stream a response body", async () => {
    // This is the rejection the fallback to polling is built on: without it, an environment
    // with no streaming `fetch` would get a resolved promise and no updates at all.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, body: null }));
    await expect(api.jobEvents({})).rejects.toThrow(/unsupported/i);
  });

  it("asks for no request timeout", async () => {
    // The default 30s deadline would abort a healthy stream mid-render. Asserted on the
    // resolved request options rather than by waiting, because the bug is silent and slow.
    streamOf([]);
    await api.jobEvents({});
    const [, init] = globalThis.fetch.mock.calls[0];
    // `apiFetch` takes the fast path when there is no timeout and no signal, so it forwards
    // neither a signal nor an AbortController — which is itself the evidence no timer was set.
    expect(init.signal).toBeUndefined();
    expect(init.headers.Accept).toBe("text/event-stream");
  });

  it("sends the token as a header, not in the query string", async () => {
    // The reason this is `fetch` and not `EventSource`. The stream stays open for an entire
    // render, so a `?token=` would sit in access and proxy logs for its whole lifetime.
    window.localStorage.setItem("clipper_token", "s3cret");
    streamOf([]);
    await api.jobEvents({});
    const [url, init] = globalThis.fetch.mock.calls[0];
    expect(url).toBe("/api/jobs/events");
    expect(url).not.toContain("token");
    expect(init.headers["X-API-Token"]).toBe("s3cret");
    window.localStorage.removeItem("clipper_token");
  });
});
