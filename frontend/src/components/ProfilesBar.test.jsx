import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ProfilesBar from "./ProfilesBar.jsx";

const PROFILES = [
  { id: "p1", name: "Shorts" },
  { id: "p2", name: "Podcast" },
];

function setup(props = {}) {
  const handlers = {
    onApply: vi.fn(),
    onSave: vi.fn().mockResolvedValue(undefined),
    onSetDefault: vi.fn().mockResolvedValue(undefined),
    onDelete: vi.fn().mockResolvedValue(undefined),
  };
  const utils = render(
    <ProfilesBar profiles={PROFILES} defaultId="p2" activeId="" {...handlers} {...props} />,
  );
  return { ...handlers, ...utils };
}

const nameBox = () => screen.getByPlaceholderText(/profile name|Update/);

describe("the profile list", () => {
  it("lists every profile with a placeholder option first", () => {
    setup();
    expect(screen.getByRole("option", { name: "Select a profile…" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Shorts" })).toBeInTheDocument();
  });

  it("stars the default profile in the list", () => {
    setup();
    expect(screen.getByRole("option", { name: "Podcast ★" })).toBeInTheDocument();
  });

  it("applies the profile the user picks", async () => {
    const { onApply } = setup();
    await userEvent.selectOptions(screen.getByRole("combobox"), "p1");
    expect(onApply).toHaveBeenCalledWith("p1");
  });

  it("names the active profile, and says when it is also the default", () => {
    const { unmount } = setup({ activeId: "p1" });
    expect(screen.getByText(/Active: Shorts/)).toBeInTheDocument();
    expect(screen.queryByText(/\(default\)/)).toBeNull();
    unmount();
    setup({ activeId: "p2" });
    expect(screen.getByText(/Active: Podcast/)).toBeInTheDocument();
    expect(screen.getByText(/\(default\)/)).toBeInTheDocument();
  });

  it("shows no active line when nothing is selected", () => {
    setup({ activeId: "" });
    expect(screen.queryByText(/Active:/)).toBeNull();
  });

  it("tolerates an activeId that no longer exists", () => {
    // A profile deleted in another tab leaves a dangling selection; `find` returns undefined and
    // every read of it is optional-chained.
    expect(() => setup({ activeId: "gone" })).not.toThrow();
    expect(screen.queryByText(/Active:/)).toBeNull();
  });
});

describe("default and delete need a selection", () => {
  it("disables both when no profile is active", () => {
    setup({ activeId: "" });
    expect(screen.getByRole("button", { name: /Default/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });

  it("enables both once a profile is active", () => {
    setup({ activeId: "p1" });
    expect(screen.getByRole("button", { name: /Default/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeEnabled();
  });

  it("sets and deletes the active profile", async () => {
    const { onSetDefault, onDelete } = setup({ activeId: "p1" });
    await userEvent.click(screen.getByRole("button", { name: /Default/ }));
    expect(onSetDefault).toHaveBeenCalledWith("p1");
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onDelete).toHaveBeenCalledWith("p1");
  });
});

describe("saving", () => {
  it("creates a new profile from a typed name, with no id", () => {
    // An empty id is what tells the API "insert", so sending one would overwrite something.
    return (async () => {
      const { onSave } = setup();
      await userEvent.type(nameBox(), "Reels");
      await userEvent.click(screen.getByRole("button", { name: "Save current" }));
      expect(onSave).toHaveBeenCalledWith("Reels", "");
    })();
  });

  it("updates in place when the typed name matches an existing profile", async () => {
    const { onSave } = setup();
    await userEvent.type(nameBox(), "Shorts");
    await userEvent.click(screen.getByRole("button", { name: "Save current" }));
    expect(onSave).toHaveBeenCalledWith("Shorts", "p1");
  });

  it("matches an existing name case-insensitively", async () => {
    // Otherwise "shorts" would silently create a second profile that looks like a duplicate.
    const { onSave } = setup();
    await userEvent.type(nameBox(), "sHoRtS");
    await userEvent.click(screen.getByRole("button", { name: "Save current" }));
    expect(onSave).toHaveBeenCalledWith("sHoRtS", "p1");
  });

  it("trims surrounding whitespace before deciding", async () => {
    const { onSave } = setup();
    await userEvent.type(nameBox(), "  Shorts  ");
    await userEvent.click(screen.getByRole("button", { name: "Save current" }));
    expect(onSave).toHaveBeenCalledWith("Shorts", "p1");
  });

  it("falls back to updating the active profile when the box is empty", async () => {
    // "Save current" with a profile selected and nothing typed means "save over this one".
    const { onSave } = setup({ activeId: "p1" });
    await userEvent.click(screen.getByRole("button", { name: "Save current" }));
    expect(onSave).toHaveBeenCalledWith("Shorts", "p1");
  });

  it("does nothing when there is neither a typed name nor an active profile", async () => {
    // Saving would otherwise create a profile called "" that cannot be told apart in the list.
    const { onSave } = setup({ activeId: "" });
    await userEvent.click(screen.getByRole("button", { name: "Save current" }));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("clears the box after a successful save", async () => {
    const { unmount } = setup();
    await userEvent.type(nameBox(), "Reels");
    await userEvent.click(screen.getByRole("button", { name: "Save current" }));
    expect(nameBox()).toHaveValue("");
    unmount();
  });

  it("prompts to update the active profile in the placeholder", () => {
    setup({ activeId: "p1" });
    expect(screen.getByPlaceholderText('Update "Shorts" or new name')).toBeInTheDocument();
  });
});

describe("while a request is in flight", () => {
  it("disables the controls, so a second click cannot race the first", async () => {
    let release;
    const onSave = vi.fn(() => new Promise((resolve) => (release = resolve)));
    setup({ onSave, activeId: "p1" });

    const save = screen.getByRole("button", { name: "Save current" });
    await userEvent.click(save);
    expect(save).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();

    release();
    await vi.waitFor(() => expect(save).toBeEnabled());
  });

  it("re-enables the controls when the request fails", async () => {
    // The `finally` is what guarantees this: without it a single failed save would leave the
    // whole bar dead until reload.
    //
    // No `.catch()` on the click any more. That swallowed a rejection the *component* was
    // letting escape - these are onClick handlers, so React drops the returned promise and it
    // surfaced as an unhandled rejection that failed the whole vitest run while every
    // individual test still reported green.
    const onSave = vi.fn().mockRejectedValue(new Error("nope"));
    setup({ onSave, activeId: "p1" });
    const save = screen.getByRole("button", { name: "Save current" });
    await userEvent.click(save);
    await vi.waitFor(() => expect(save).toBeEnabled());
  });

  it("tells the user when the request fails", async () => {
    // Re-enabling the button is not enough on its own: a bar that looks exactly like it did
    // before the click is indistinguishable from one where the save succeeded.
    const onSave = vi.fn().mockRejectedValue(new Error("nope"));
    setup({ onSave, activeId: "p1" });
    await userEvent.click(screen.getByRole("button", { name: "Save current" }));
    const alert = await vi.waitFor(() => screen.getByRole("alert"));
    expect(alert).toHaveTextContent("nope");
  });
});
