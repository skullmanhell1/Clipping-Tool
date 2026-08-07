import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ProfilesBar from "./ProfilesBar.jsx";

/**
 * Four of this bar's five actions take a profile id, and every one of them is destructive or
 * near-destructive: delete removes a saved configuration, set-default changes what every future
 * page load starts from, and save either creates a profile or overwrites an existing one. Which of
 * those two a save does is decided here, from the typed name, and getting it wrong is silent —
 * either a duplicate profile appears with the same name, or someone else's saved settings are
 * replaced by yours.
 *
 * The name resolution is the interesting part: an empty field means "update the profile I have
 * selected", a name that matches an existing profile (in any case) means "overwrite that one", and
 * anything else means "create". These tests pin all three, and pin that no call is made when there
 * is nothing to name the profile after.
 */

const PROFILES = [
  { id: "p1", name: "Shorts" },
  { id: "p2", name: "Long form" },
];

const setup = (props = {}) => {
  const handlers = {
    onApply: vi.fn(),
    onSave: vi.fn(),
    onSetDefault: vi.fn(),
    onDelete: vi.fn(),
  };
  // Props are spread last, so a test can override one handler and still read the others.
  const utils = render(
    <ProfilesBar profiles={PROFILES} defaultId="p2" activeId="p1" {...handlers} {...props} />
  );
  return { ...utils, ...handlers, ...props };
};

const nameField = () => screen.getByPlaceholderText(/new (profile )?name/i);

describe("ProfilesBar selection", () => {
  it("lists every profile and marks the default one", () => {
    setup();
    const select = screen.getByRole("combobox");
    expect([...select.options].map((option) => option.textContent)).toEqual([
      "Select a profile…",
      "Shorts",
      "Long form ★",
    ]);
  });

  it("applies the profile the user picked, by id", async () => {
    // The handler reloads every setting from that profile, so an id from the wrong option would
    // silently replace the user's whole configuration with someone else's.
    const { onApply } = setup();
    await userEvent.selectOptions(screen.getByRole("combobox"), "p2");
    expect(onApply).toHaveBeenCalledWith("p2");
  });

  it("passes an empty id when the user picks the placeholder option", async () => {
    const { onApply } = setup();
    await userEvent.selectOptions(screen.getByRole("combobox"), "");
    expect(onApply).toHaveBeenCalledWith("");
  });

  it("names the active profile, and says when it is also the default", () => {
    setup({ activeId: "p2" });
    expect(screen.getByText(/active: long form \(default\)/i)).toBeInTheDocument();
  });

  it("does not call an active profile the default when it is not", () => {
    setup({ activeId: "p1" });
    expect(screen.getByText(/active: shorts/i)).toBeInTheDocument();
    expect(screen.queryByText(/\(default\)/)).not.toBeInTheDocument();
  });

  it("says nothing about an active profile when none is selected", () => {
    setup({ activeId: "" });
    expect(screen.queryByText(/active:/i)).not.toBeInTheDocument();
  });
});

describe("ProfilesBar default and delete", () => {
  it("sets the selected profile as the default", async () => {
    const { onSetDefault } = setup();
    await userEvent.click(screen.getByRole("button", { name: /default/i }));
    expect(onSetDefault).toHaveBeenCalledWith("p1");
  });

  it("deletes the selected profile", async () => {
    const { onDelete } = setup();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onDelete).toHaveBeenCalledWith("p1");
  });

  it("disables both when nothing is selected, rather than acting on an empty id", async () => {
    // `onDelete("")` would be a DELETE to /api/profiles/ — a request whose meaning is decided by
    // the server's routing rather than by anything the user asked for.
    setup({ activeId: "" });
    expect(screen.getByRole("button", { name: /default/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });
});

describe("ProfilesBar saving", () => {
  it("creates a new profile when the typed name is not one that exists", async () => {
    // The empty id is what tells the API to create rather than overwrite.
    const { onSave } = setup();
    await userEvent.type(nameField(), "Podcast cuts");
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("Podcast cuts", ""));
  });

  it("overwrites an existing profile when the typed name matches it, ignoring case", async () => {
    // Otherwise typing the name of a profile you already have produces a second profile with the
    // same name, and the dropdown then offers two identical-looking options.
    const { onSave } = setup();
    await userEvent.type(nameField(), "long FORM");
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("long FORM", "p2"));
  });

  it("updates the active profile when the name field is left empty", async () => {
    // The field's own placeholder promises this ("Update \"Shorts\" or new name"), and it is the
    // common case: tweak a setting, press save, keep the profile you were already on.
    const { onSave } = setup();
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("Shorts", "p1"));
  });

  it("ignores surrounding whitespace when resolving the name", async () => {
    const { onSave } = setup();
    await userEvent.type(nameField(), "  Shorts  ");
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("Shorts", "p1"));
  });

  it("saves nothing when there is neither a typed name nor an active profile", async () => {
    // A profile with an empty name cannot be selected again afterwards.
    const { onSave } = setup({ activeId: "" });
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("clears the name field once the save has gone through", async () => {
    // A name left behind would be re-used by the next press of the same button, overwriting the
    // profile the user has since moved off.
    setup();
    await userEvent.type(nameField(), "Podcast cuts");
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    await waitFor(() => expect(nameField()).toHaveValue(""));
  });

  it("locks the bar while a save is in flight", async () => {
    // Two saves of the same name race to create two profiles, and the second one wins the list.
    let release;
    const onSave = vi.fn(
      () =>
        new Promise((resolve) => {
          release = resolve;
        })
    );
    setup({ onSave });
    await userEvent.type(nameField(), "Podcast cuts");
    const button = screen.getByRole("button", { name: /save current/i });
    await userEvent.click(button);

    expect(button).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();

    release();
    await waitFor(() => expect(button).toBeEnabled());
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("explains what a profile actually contains", () => {
    // A profile is the entire settings blob, not just the four visible dropdowns; someone who
    // thought otherwise would apply one and be surprised by their publishing routes changing.
    setup();
    expect(screen.getByText(/clip length, aspect, captions, effects, publishing/i)).toBeVisible();
  });
});

describe("ProfilesBar failure reporting", () => {
  // Every action here awaits an API call through a parent handler, and neither this component nor
  // any of those handlers used to catch. A rejected save re-enabled the button, changed nothing and
  // said nothing — indistinguishable from a save that worked — while the rejection surfaced only as
  // an unhandled promise rejection in the console. These pin that each action now reports.

  it("reports a failed save instead of clearing the spinner silently", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("A profile named 'Shorts' already exists"));
    setup({ onSave });
    await userEvent.type(nameField(), "Shorts");
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already exists/i);
  });

  it("keeps the typed name when the save fails, so the attempt is not lost", async () => {
    // The name is only cleared after a successful save. Clearing it on failure would make the
    // retry a retype.
    const onSave = vi.fn().mockRejectedValue(new Error("nope"));
    setup({ onSave });
    await userEvent.type(nameField(), "Podcast cuts");
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    await screen.findByRole("alert");
    expect(nameField()).toHaveValue("Podcast cuts");
  });

  it("reports a failed delete", async () => {
    const onDelete = vi.fn().mockRejectedValue(new Error("Profile is in use"));
    setup({ onDelete });
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/in use/i);
  });

  it("reports a failed set-default", async () => {
    const onSetDefault = vi.fn().mockRejectedValue(new Error("Could not set the default"));
    setup({ onSetDefault });
    await userEvent.click(screen.getByRole("button", { name: /default/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not set the default/i);
  });

  it("reports a failed apply", async () => {
    // Selecting a profile was the one action not wrapped at all, so a parent that threw while
    // applying left the dropdown showing a profile whose settings had not been applied.
    const onApply = vi.fn().mockRejectedValue(new Error("Profile could not be read"));
    setup({ onApply });
    await userEvent.selectOptions(screen.getByRole("combobox"), "p2");
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be read/i);
  });

  it("says what to do when there is nothing to name the profile after", async () => {
    // Previously a bare `return`: with no name typed and no profile selected, the click did
    // nothing and explained nothing.
    const onSave = vi.fn();
    setup({ onSave, activeId: "" });
    await userEvent.click(screen.getByRole("button", { name: /save current/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/type a name/i);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("clears a previous failure when the next action starts", async () => {
    // Otherwise a stale message sits under a control that has since worked, which is worse than
    // no message: it says the current state is broken when it is not.
    const onSetDefault = vi.fn().mockRejectedValueOnce(new Error("transient")).mockResolvedValue();
    setup({ onSetDefault });
    const button = screen.getByRole("button", { name: /default/i });
    await userEvent.click(button);
    await screen.findByRole("alert");
    await userEvent.click(button);
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("re-enables the controls after a failure so the action can be retried", async () => {
    const onDelete = vi.fn().mockRejectedValue(new Error("boom"));
    setup({ onDelete });
    const button = screen.getByRole("button", { name: "Delete" });
    await userEvent.click(button);
    await screen.findByRole("alert");
    expect(button).toBeEnabled();
  });
});
