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
    expect(api.clipUrl("http://example.com/clip.mp4")).toBe(
      "http://example.com/clip.mp4"
    );
  });

  it("is case-insensitive about the scheme", () => {
    expect(api.clipUrl("HTTPS://cdn.example.com/a.mp4")).toBe(
      "HTTPS://cdn.example.com/a.mp4"
    );
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
    await expect(api.info()).rejects.toThrow(
      "instagram cannot publish directly yet"
    );
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
