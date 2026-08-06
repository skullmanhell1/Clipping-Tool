// SettingsPanel's gating: which controls it refuses to offer, and why.
//
// The panel is mostly declarative, and testing that a dropdown renders its options proves little.
// What is worth pinning is every place it *withholds* a control, because each of those exists to
// stop a creator enabling something that would degrade silently at render time — the exact class
// of failure this repo keeps finding. A gate that quietly stops gating leaves a control that looks
// live, accepts a value, and produces a clip that is wrong in a way nobody attributes to it.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_SETTINGS } from "../settingsSchema.js";
import SettingsPanel from "./SettingsPanel.jsx";

const KINETIC = "kinetic_typography";
const STEM = "stem_inpainting";

function setup(props = {}) {
  const onChange = vi.fn();
  const onToggleWatch = vi.fn();
  const utils = render(
    <SettingsPanel
      settings={{ ...DEFAULT_SETTINGS, ...(props.settings || {}) }}
      onChange={onChange}
      watch={{ enabled: false, folder: "/inbox" }}
      onToggleWatch={onToggleWatch}
      effects={props.effects ?? {}}
      engines={props.engines ?? []}
      capabilities={props.capabilities ?? null}
    />,
  );
  return { onChange, onToggleWatch, ...utils };
}

const openSection = async (name) => {
  await userEvent.click(screen.getByRole("button", { name: new RegExp(name, "i") }));
};

describe("core controls", () => {
  it("renders without any /api/info payload at all", () => {
    // The panel loads before /api/info answers, and on an install where it never does. Falling
    // over here would leave the app with no way to configure a job.
    expect(() =>
      render(
        <SettingsPanel
          settings={DEFAULT_SETTINGS}
          onChange={vi.fn()}
          watch={{ enabled: false, folder: "" }}
          onToggleWatch={vi.fn()}
        />,
      ),
    ).not.toThrow();
  });

  it("reports a changed setting without mutating the rest", async () => {
    const { onChange } = setup();
    await userEvent.selectOptions(screen.getByLabelText(/Aspect Ratio/i), "1:1");
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ aspect: "1:1" }));
    // The whole settings object is passed up, so a partial update would drop every other field.
    expect(Object.keys(onChange.mock.calls[0][0]).length).toBe(
      Object.keys(DEFAULT_SETTINGS).length,
    );
  });
});

describe("collapsible sections", () => {
  it("starts with the advanced sections closed", () => {
    setup();
    expect(screen.queryByLabelText(/^Selection$/i)).toBeNull();
  });

  it("opens and closes Advanced settings", async () => {
    setup();
    await openSection("Advanced settings");
    expect(screen.getByLabelText(/^Selection$/i)).toBeInTheDocument();
    await openSection("Advanced settings");
    expect(screen.queryByLabelText(/^Selection$/i)).toBeNull();
  });

  it("opens Visual effects", async () => {
    setup();
    await openSection("Visual effects");
    expect(screen.getByText("Stem repair")).toBeInTheDocument();
  });
});

describe("stem repair gating", () => {
  const openStems = async (props) => {
    const result = setup(props);
    await openSection("Visual effects");
    return result;
  };

  it("disables the whole group when the engine is unavailable", async () => {
    // A `fieldset disabled` rather than per-control flags: the group is unusable as a unit, and
    // leaving one slider live would suggest partial support that does not exist.
    await openStems({ engines: [{ id: STEM, available: false, missing: ["demucs"] }] });
    expect(screen.getByRole("group", { name: /Stem repair/i })).toBeDisabled();
  });

  it("names the missing dependency instead of just saying unavailable", async () => {
    await openStems({ engines: [{ id: STEM, available: false, missing: ["demucs", "torch"] }] });
    expect(screen.getAllByText(/missing demucs, torch/).length).toBeGreaterThan(0);
  });

  it("leaves the group enabled when the install does not advertise the engine at all", async () => {
    // Absent is not the same as unavailable: an older backend that reports no engines must not
    // disable features it in fact supports.
    await openStems({ engines: [] });
    expect(screen.getByRole("group", { name: /Stem repair/i })).not.toBeDisabled();
  });

  it("gates the gain sliders on the custom mix preset", async () => {
    // A named preset overrides the individual gains on the backend, so live sliders would show
    // numbers that do not describe what will happen.
    await openStems({ settings: { stem_mix_preset: "podcast" } });
    const vocals = screen.getByLabelText(/Vocals/i);
    expect(vocals).toBeDisabled();
  });

  it("enables the gain sliders on the custom preset", async () => {
    await openStems({ settings: { stem_mix_preset: "custom" } });
    expect(screen.getByLabelText(/Vocals/i)).toBeEnabled();
  });

  it("explains why the sliders are disabled", async () => {
    // Disabled with no reason reads as broken.
    await openStems({ settings: { stem_mix_preset: "podcast" } });
    expect(screen.getByText(/preset sets the gains/i)).toBeInTheDocument();
  });

  it("treats a missing mix preset as custom", async () => {
    // A profile saved before the field existed has no value for it; the fallback keeps the
    // sliders usable rather than dead.
    await openStems({ settings: { stem_mix_preset: undefined } });
    expect(screen.getByLabelText(/Vocals/i)).toBeEnabled();
  });

  it("marks the spectral backend as needing a local model when none is present", async () => {
    // Selecting it without the checkpoint on disk produces a job that fails at the separation
    // step, minutes in.
    await openStems({ capabilities: { "model:htdemucs": { available: false } } });
    expect(screen.getByRole("option", { name: /needs local model/ })).toBeDisabled();
  });

  it("leaves spectral selectable when the model is present", async () => {
    await openStems({ capabilities: { "model:htdemucs": { available: true } } });
    expect(screen.queryByRole("option", { name: /needs local model/ })).toBeNull();
  });

  it("assumes the model is present when capabilities are unknown", async () => {
    // `!== false` rather than `=== true`: an install that does not report the model must not have
    // a working backend hidden from it.
    await openStems({ capabilities: null });
    expect(screen.queryByRole("option", { name: /needs local model/ })).toBeNull();
  });
});

describe("kinetic typography gating", () => {
  const openKinetic = async (props) => {
    const result = setup(props);
    await openSection("Visual effects");
    return result;
  };

  it("disables the toggle when the engine is unavailable", async () => {
    await openKinetic({ engines: [{ id: KINETIC, available: false, missing: ["fonts"] }] });
    expect(screen.getByRole("checkbox", { name: /Kinetic typography captions/i })).toBeDisabled();
  });

  it("leaves the toggle enabled when the engine is available", async () => {
    await openKinetic({ engines: [{ id: KINETIC, available: true }] });
    expect(screen.getByRole("checkbox", { name: /Kinetic typography captions/i })).toBeEnabled();
  });

  it("offers the styles /api/info advertises, not the hardcoded fallback", async () => {
    // The fallback list exists for an unreachable /api/info; when the backend does answer, its
    // vocabulary wins, so a style the backend dropped is not offered.
    await openKinetic({
      settings: { kinetic_typography_enabled: true },
      capabilities: { [KINETIC]: { styles: ["pop"] } },
    });
    const select = screen.getByLabelText(/Kinetic style/i);
    expect(select.querySelectorAll("option")).toHaveLength(1);
  });
});

describe("engine rows", () => {
  it("renders no engines section when /api/info advertises none", () => {
    // An empty "Engines" heading on an install with no optional engines is noise that suggests
    // something failed to load.
    setup({ engines: [] });
    expect(screen.queryByRole("button", { name: /engines/i })).toBeNull();
  });

  it("lists an advertised engine under a friendly name", async () => {
    setup({ engines: [{ id: STEM, available: true }] });
    await openSection("engines");
    expect(screen.getByText("Stem Inpainting")).toBeInTheDocument();
  });

  it("shows why an advertised engine cannot be used", async () => {
    setup({ engines: [{ id: STEM, available: false, missing: ["demucs"] }] });
    await openSection("engines");
    expect(screen.getByText(/missing demucs/)).toBeInTheDocument();
  });

  it("warns that a network engine is blocked in permissibility mode", async () => {
    setup({ engines: [{ id: "broll_fetch", available: true, requires_network: true }] });
    await openSection("engines");
    expect(screen.getByText(/blocked in permissibility mode/)).toBeInTheDocument();
  });
});

describe("the watch folder", () => {
  it("shows the folder once watching is on, so the toggle is not a blind switch", () => {
    // Only when enabled: naming a folder that is not being watched would read as a live setting.
    render(
      <SettingsPanel
        settings={DEFAULT_SETTINGS}
        onChange={vi.fn()}
        watch={{ enabled: true, folder: "/inbox" }}
        onToggleWatch={vi.fn()}
        effects={{}}
      />,
    );
    expect(screen.getByText("(/inbox)")).toBeInTheDocument();
  });

  it("hides the folder while watching is off", () => {
    setup();
    expect(screen.queryByText("(/inbox)")).toBeNull();
  });

  it("reports a toggle rather than changing settings", async () => {
    // The watch folder is server state, not part of the settings blob, so it goes up its own
    // channel.
    const { onToggleWatch, onChange } = setup();
    await userEvent.click(screen.getByRole("checkbox", { name: /Watch-folder mode/i }));
    expect(onToggleWatch).toHaveBeenCalledWith(true);
    expect(onChange).not.toHaveBeenCalled();
  });
});
