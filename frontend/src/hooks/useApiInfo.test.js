import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api.js";
import { useApiInfo } from "./useApiInfo.js";

afterEach(() => vi.restoreAllMocks());

const render = () => renderHook(() => useApiInfo());

describe("useApiInfo", () => {
  it("reports capable-but-unknown before the response arrives", () => {
    vi.spyOn(api, "info").mockReturnValue(new Promise(() => {}));
    const { result } = render();
    expect(result.current).toEqual({
      version: "",
      llmAvailable: false,
      effects: null,
      engines: [],
      capabilities: null,
    });
  });

  it("maps the payload's snake_case onto the UI's names", async () => {
    vi.spyOn(api, "info").mockResolvedValue({
      version: "1.2.3",
      llm_available: true,
      effects: { caption_fonts: ["Anton"] },
      engines: [{ id: "stem_inpainting" }],
      capabilities: { "model:htdemucs": { available: true } },
    });
    const { result } = render();
    await waitFor(() => expect(result.current.version).toBe("1.2.3"));
    expect(result.current.llmAvailable).toBe(true);
    expect(result.current.effects).toEqual({ caption_fonts: ["Anton"] });
    expect(result.current.engines).toEqual([{ id: "stem_inpainting" }]);
    expect(result.current.capabilities).toEqual({ "model:htdemucs": { available: true } });
  });

  it("keeps the unknown defaults when the probe fails", async () => {
    // The app has to stay usable when /api/info is the only broken thing: the panel reads absent
    // capability as available, so this is "could not ask", not "nothing works".
    vi.spyOn(api, "info").mockRejectedValue(new Error("502"));
    const { result } = render();
    await waitFor(() => expect(api.info).toHaveBeenCalled());
    expect(result.current.engines).toEqual([]);
    expect(result.current.capabilities).toBeNull();
    expect(result.current.version).toBe("");
  });

  it("coerces a non-list `engines` to an empty list", async () => {
    // The settings panel maps over this. A malformed optional field would otherwise throw during
    // render and take the whole page down.
    vi.spyOn(api, "info").mockResolvedValue({ engines: { id: "oops" } });
    const { result } = render();
    await waitFor(() => expect(api.info).toHaveBeenCalled());
    expect(result.current.engines).toEqual([]);
  });

  it("normalises missing fields rather than passing undefined through", async () => {
    vi.spyOn(api, "info").mockResolvedValue({});
    const { result } = render();
    await waitFor(() => expect(api.info).toHaveBeenCalled());
    expect(result.current).toEqual({
      version: "",
      llmAvailable: false,
      effects: null,
      engines: [],
      capabilities: null,
    });
  });

  it("treats a truthy non-boolean llm_available as true", async () => {
    vi.spyOn(api, "info").mockResolvedValue({ llm_available: "yes" });
    const { result } = render();
    await waitFor(() => expect(result.current.llmAvailable).toBe(true));
  });

  it("asks once, not once per render", async () => {
    vi.spyOn(api, "info").mockResolvedValue({});
    const { rerender } = render();
    rerender();
    rerender();
    await waitFor(() => expect(api.info).toHaveBeenCalledTimes(1));
  });

  it("does not set state after unmount", async () => {
    // React logs an error for an update on an unmounted component; the `live` flag is what
    // prevents it when a slow probe outlives a view switch.
    let resolve;
    vi.spyOn(api, "info").mockReturnValue(new Promise((r) => (resolve = r)));
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const { unmount } = render();
    unmount();
    resolve({ version: "9" });
    await Promise.resolve();
    expect(errors).not.toHaveBeenCalled();
  });
});
