import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api.js";
import { useProfiles } from "./useProfiles.js";

const P1 = { id: "p1", name: "Shorts", settings: { aspect: "9:16" }, publishing: { mode: "auto" } };
const P2 = { id: "p2", name: "Podcast", settings: { aspect: "1:1" } };

function setup({ profiles = [P1, P2], default_id = null } = {}) {
  vi.spyOn(api, "profiles").mockResolvedValue({ profiles, default_id });
  vi.spyOn(api, "saveProfile").mockResolvedValue({ id: "saved" });
  vi.spyOn(api, "setDefaultProfile").mockResolvedValue({});
  vi.spyOn(api, "deleteProfile").mockResolvedValue({});
  const onApply = vi.fn();
  const view = renderHook(() =>
    useProfiles({ settings: { aspect: "16:9" }, publishing: { mode: "review" }, onApply }),
  );
  return { onApply, ...view };
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("loading the list", () => {
  it("reads the profiles and the default id", async () => {
    const { result } = setup({ default_id: "p2" });
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    expect(result.current.defaultId).toBe("p2");
  });

  it("stays empty and silent when the read fails", async () => {
    // Profiles are a convenience; a failure must not become an error the user dismisses before
    // they can create a clip.
    vi.spyOn(api, "profiles").mockRejectedValue(new Error("500"));
    const onApply = vi.fn();
    const { result } = renderHook(() => useProfiles({ settings: {}, publishing: {}, onApply }));
    await waitFor(() => expect(api.profiles).toHaveBeenCalled());
    expect(result.current.profiles).toEqual([]);
    expect(result.current.defaultId).toBeNull();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("tolerates a response with neither field", async () => {
    vi.spyOn(api, "profiles").mockResolvedValue({});
    const { result } = renderHook(() =>
      useProfiles({ settings: {}, publishing: {}, onApply: vi.fn() }),
    );
    await waitFor(() => expect(api.profiles).toHaveBeenCalled());
    expect(result.current.profiles).toEqual([]);
  });
});

describe("the default profile", () => {
  it("pre-fills the form at startup", async () => {
    const { result, onApply } = setup({ default_id: "p1" });
    await waitFor(() => expect(onApply).toHaveBeenCalledWith(P1));
    expect(result.current.activeId).toBe("p1");
  });

  it("applies nothing when there is no default", async () => {
    const { result, onApply } = setup({ default_id: null });
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    expect(onApply).not.toHaveBeenCalled();
    expect(result.current.activeId).toBe("");
  });

  it("ignores a default id that is not in the list", async () => {
    const { result, onApply } = setup({ default_id: "gone" });
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    expect(onApply).not.toHaveBeenCalled();
  });

  it("re-applies the default only once, even after the list reloads", async () => {
    // The guard that matters: `setDefault` and `save` both reload the list, and re-applying the
    // default then would silently discard everything the user had adjusted since startup.
    const { result, onApply } = setup({ default_id: "p1" });
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.setDefault("p2");
    });
    expect(onApply).toHaveBeenCalledTimes(1);
  });
});

describe("applying a profile", () => {
  it("selects it and hands it to the form", async () => {
    const { result, onApply } = setup();
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    act(() => result.current.apply("p2"));
    expect(result.current.activeId).toBe("p2");
    expect(onApply).toHaveBeenCalledWith(P2);
  });

  it("clears the selection without touching the form", async () => {
    // "None" means "stop tracking a profile", not "reset my settings".
    const { result, onApply } = setup();
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    act(() => result.current.apply(""));
    expect(result.current.activeId).toBe("");
    expect(onApply).not.toHaveBeenCalled();
  });

  it("selects nothing for an id that no longer exists", async () => {
    // A profile deleted in another tab leaves a dangling id; this must not throw.
    const { result, onApply } = setup();
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    act(() => result.current.apply("gone"));
    expect(onApply).not.toHaveBeenCalled();
  });
});

describe("saving", () => {
  it("sends the current form state under the given name and id", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    await act(async () => {
      await result.current.save("Reels", "");
    });
    expect(api.saveProfile).toHaveBeenCalledWith({
      name: "Reels",
      id: "",
      settings: { aspect: "16:9" },
      publishing: { mode: "review" },
    });
  });

  it("reloads the list and selects what was saved", async () => {
    const { result } = setup();
    await waitFor(() => expect(api.profiles).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.save("Reels", "");
    });
    expect(api.profiles).toHaveBeenCalledTimes(2);
    expect(result.current.activeId).toBe("saved");
  });

  it("propagates a failure so the bar can re-enable itself", async () => {
    // ProfilesBar re-enables its controls in a `finally`; swallowing the rejection here would
    // leave it thinking the save succeeded.
    const { result } = setup();
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    // After setup(), which installs its own happy-path spy.
    vi.spyOn(api, "saveProfile").mockRejectedValue(new Error("duplicate"));
    await expect(result.current.save("Reels", "")).rejects.toThrow("duplicate");
  });
});

describe("setting the default", () => {
  it("sets it and reloads, so the star moves", async () => {
    const { result } = setup();
    await waitFor(() => expect(api.profiles).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.setDefault("p2");
    });
    expect(api.setDefaultProfile).toHaveBeenCalledWith("p2");
    expect(api.profiles).toHaveBeenCalledTimes(2);
  });
});

describe("deleting", () => {
  it("deletes and reloads", async () => {
    const { result } = setup();
    await waitFor(() => expect(api.profiles).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.remove("p2");
    });
    expect(api.deleteProfile).toHaveBeenCalledWith("p2");
    expect(api.profiles).toHaveBeenCalledTimes(2);
  });

  it("clears the selection when the active profile is the one deleted", async () => {
    // Otherwise the bar shows a profile that no longer exists, with its Delete button still armed.
    const { result } = setup();
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    act(() => result.current.apply("p1"));
    await act(async () => {
      await result.current.remove("p1");
    });
    expect(result.current.activeId).toBe("");
  });

  it("keeps the selection when a different profile is deleted", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.profiles).toHaveLength(2));
    act(() => result.current.apply("p1"));
    await act(async () => {
      await result.current.remove("p2");
    });
    expect(result.current.activeId).toBe("p1");
  });
});
