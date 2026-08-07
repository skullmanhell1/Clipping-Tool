import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PropTypes from "prop-types";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import SettingsPanel from "./SettingsPanel.jsx";

/**
 * The settings panel is a hundred-key object edited through about seventy controls, and it owns no
 * state of its own: every control's job is to hand the parent one key with one value. That makes the
 * whole component a mapping, and a mapping is exactly the kind of code that fails silently. A
 * control wired to the wrong key writes a setting nobody asked for and leaves the intended one at
 * its default; a value passed as a string where the backend wants a number is dropped by the
 * options filter without an error; and `{ ...settings, key: value }` written as `{ key: value }`
 * would discard every other setting on the first interaction.
 *
 * So the assertions below are mostly about the payload rather than the pixels, and several of them
 * pin a label that does not match its key — "Punch-in intro" writes `transitions`, "Diarisation"
 * writes `diarization` with an American spelling. Those are the pairs a rename would quietly break.
 *
 * The second theme is availability. `/api/info` reports which engines this install can actually run,
 * and the panel's contract is that an unavailable engine cannot be switched on: the group is
 * disabled, its toggle reads unchecked even when the stored setting says otherwise, and the reason
 * is on screen. Enabling one anyway would not fail loudly — it would render a clip with the feature
 * silently absent, which is the one outcome nobody can see in a settings screen.
 */

const SETTINGS = {
  language: "auto",
  clip_length: "auto",
  aspect: "9:16",
  num_clips: "auto",
  strategy: "ai",
  platform: "generic",
  vibe: "energetic",
  hashtag_count: 8,
  topic: "",
  vocabulary: "",
  range_start: "",
  range_end: "",
  selection_prompt: "",
  visual_selection: false,
  subtitle_sidecar: false,
  metadata: true,
  captions: true,
  caption_preset: "karaoke",
  caption_position: "bottom",
  caption_template: "karaoke",
  caption_keyword_highlight: true,
  caption_keyword_ai: false,
  caption_emoji: false,
  kinetic_typography_enabled: false,
  kinetic_style: "pop",
  kinetic_reveal: "word_by_word",
  stem_inpainting_enabled: false,
  stem_mix_preset: "custom",
  stem_repair_mode: "crossfade",
  stem_gain_music: 1,
  stem_gain_other: 1,
  stem_gain_vocals: 1,
  stem_repair_window_ms: 12,
  stem_backend: "auto",
  stem_declick: false,
  stem_retain_stems: false,
  broll: false,
  broll_intensity: "off",
  asset_sourcing_mode: "off",
  broll_provider: "",
  color: "",
  music: "",
  music_volume: 0.1,
  permissibility_mode: false,
  emoji: "off",
  emoji_mode: "keyword",
  emoji_animate: false,
  reframe: false,
  speaker_reframe: false,
  diarization: false,
  zoom: false,
  transitions: false,
  hook_title: false,
  fades: false,
  progress_bar: false,
  filler_removal: false,
  reframe_layout: "follow_active",
  reframe_intensity: "standard",
};

/** What `/api/info` advertises under `effects`. */
const EFFECTS = {
  caption_fonts: [{ name: "Anton" }, { name: "Bangers" }],
  reframe_layouts: ["follow_active", "split_screen"],
  reframe_intensities: ["subtle", "standard", "heavy"],
  caption_preset_details: [
    {
      name: "karaoke",
      font: "Anton",
      colors_hex: { primary: "#ffffff", highlight: "#ffe500" },
      position: "bottom",
      font_weight: 700,
      uppercase: true,
      spacing: 0,
      scale_x: 100,
      border_style: 1,
    },
    {
      name: "hormozi",
      font: "Bangers",
      colors_hex: { primary: "#ffffff", highlight: "#00ff88" },
      position: "center",
      font_weight: 900,
      uppercase: true,
      spacing: 1,
      scale_x: 105,
      border_style: 3,
    },
  ],
};

/** What `/api/info` advertises under `capabilities`: option domains and numeric bounds. */
const CAPABILITIES = {
  kinetic_typography: {
    styles: ["pop", "bounce"],
    reveal_modes: ["word_by_word", "cumulative"],
  },
  stem_inpainting: {
    mix_presets: ["custom", "speech_focus"],
    repair_modes: ["off", "crossfade", "spectral"],
    backends: ["auto", "ml"],
    gain: { min: 0, max: 3, default: 1 },
    repair_window_ms: { min: 5, max: 60, default: 20 },
  },
  "model:htdemucs": { available: true },
};

const setup = (props = {}) => {
  const onChange = vi.fn();
  const onToggleWatch = vi.fn();
  const utils = render(
    <SettingsPanel
      settings={{ ...SETTINGS, ...(props.settings || {}) }}
      onChange={onChange}
      watch={props.watch || { enabled: false, folder: "" }}
      onToggleWatch={onToggleWatch}
      effects={props.effects === undefined ? EFFECTS : props.effects}
      engines={props.engines}
      capabilities={props.capabilities === undefined ? CAPABILITIES : props.capabilities}
    />
  );
  return { ...utils, onChange, onToggleWatch };
};

/**
 * A stateful parent, for the text and number fields. The panel is controlled, so with a bare spy
 * every keystroke is applied to the same original value and only the first one is observable.
 */
function Harness({ initial, onChange }) {
  const [settings, setSettings] = useState(initial);
  return (
    <SettingsPanel
      settings={settings}
      onChange={(next) => {
        setSettings(next);
        onChange(next);
      }}
      watch={{ enabled: false, folder: "" }}
      onToggleWatch={vi.fn()}
      effects={EFFECTS}
      capabilities={CAPABILITIES}
    />
  );
}

// Declared like the panel it wraps: the settings object is opaque here for the same reason it is
// there — `SETTINGS_SCHEMA` in App.jsx is the one place that list is written down.
Harness.propTypes = {
  initial: PropTypes.object.isRequired,
  onChange: PropTypes.func.isRequired,
};

const setupHarness = (settings = {}) => {
  const onChange = vi.fn();
  const utils = render(<Harness initial={{ ...SETTINGS, ...settings }} onChange={onChange} />);
  return { ...utils, onChange };
};

const lastSettings = (onChange) => onChange.mock.calls.at(-1)[0];

/** The settings object the panel should have produced: the original plus exactly one change. */
const changed = (extra) => ({ ...SETTINGS, ...extra });

const openAdvanced = () =>
  userEvent.click(screen.getByRole("button", { name: /advanced settings/i }));
const openEffects = () => userEvent.click(screen.getByRole("button", { name: /visual effects/i }));
const openEngines = () =>
  userEvent.click(screen.getByRole("button", { name: /advanced engines/i }));

describe("SettingsPanel core controls", () => {
  it("shows the four everyday dropdowns without anything being expanded", () => {
    // These four decide the shape of every clip, so they are not behind a disclosure.
    setup();
    expect(screen.getByRole("combobox", { name: "Language" })).toHaveValue("auto");
    expect(screen.getByRole("combobox", { name: "Clip Length" })).toHaveValue("auto");
    expect(screen.getByRole("combobox", { name: "Aspect Ratio" })).toHaveValue("9:16");
    expect(screen.getByRole("combobox", { name: "Number of Clips" })).toHaveValue("auto");
  });

  it("changes the language without dropping any other setting", () => {
    // The whole settings object is rebuilt on every change, so a missing spread here would reset
    // ninety-odd keys to nothing the moment a user touched one dropdown.
    const { onChange } = setup();
    fireEvent.change(screen.getByRole("combobox", { name: "Language" }), {
      target: { value: "es" },
    });
    expect(onChange).toHaveBeenCalledWith(changed({ language: "es" }));
  });

  it("keeps translate as its own language choice", async () => {
    // "Translate to English" is not a language; it is a separate flag the API resolves. Collapsing
    // it into `language: "en"` would transcribe in English instead of translating into it.
    const { onChange } = setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Language" }), "translate");
    expect(onChange).toHaveBeenCalledWith(changed({ language: "translate" }));
  });

  it("reports the clip length band as the backend's own token", async () => {
    const { onChange } = setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Clip Length" }), "30-60s");
    expect(onChange).toHaveBeenCalledWith(changed({ clip_length: "30-60s" }));
  });

  it("reports the aspect ratio", async () => {
    const { onChange } = setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Aspect Ratio" }), "1:1");
    expect(onChange).toHaveBeenCalledWith(changed({ aspect: "1:1" }));
  });

  it("reports a clip count as a string, including the non-numeric ones", async () => {
    // "auto" and "max" share the field with "10", so the value stays a string all the way to the
    // API rather than being half-coerced here.
    const { onChange } = setup();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Number of Clips" }), "10");
    expect(onChange).toHaveBeenCalledWith(changed({ num_clips: "10" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Number of Clips" }), "max");
    expect(onChange).toHaveBeenCalledWith(changed({ num_clips: "max" }));
  });
});

describe("SettingsPanel advanced settings", () => {
  it("keeps the advanced group closed until it is asked for", async () => {
    setup();
    expect(screen.queryByRole("combobox", { name: "Selection" })).not.toBeInTheDocument();
    await openAdvanced();
    expect(screen.getByRole("combobox", { name: "Selection" })).toBeInTheDocument();
  });

  it("reports the selection strategy", async () => {
    const { onChange } = setup();
    await openAdvanced();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Selection" }), "silence");
    expect(onChange).toHaveBeenCalledWith(changed({ strategy: "silence" }));
  });

  it("reports the target platform", async () => {
    const { onChange } = setup();
    await openAdvanced();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Platform" }), "tiktok");
    expect(onChange).toHaveBeenCalledWith(changed({ platform: "tiktok" }));
  });

  it("sends an empty vibe for 'Auto' rather than the word auto", async () => {
    // The backend reads an empty vibe as "decide for yourself"; the literal string "auto" would be
    // an unknown vibe and get dropped, which looks identical on screen.
    const { onChange } = setup();
    await openAdvanced();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Vibe / Tone" }), "");
    expect(onChange).toHaveBeenCalledWith(changed({ vibe: "" }));
  });

  it("passes the hashtag count through as typed", async () => {
    // The number input does not coerce: `hashtag_count` leaves here as the field's string, and the
    // backend parses it. A test that expected a number would be asserting a coercion that is not
    // there — and that asymmetry is worth having written down.
    const { onChange } = setupHarness({ hashtag_count: "" });
    await openAdvanced();
    await userEvent.type(screen.getByRole("spinbutton", { name: /hashtag count/i }), "12");
    expect(lastSettings(onChange).hashtag_count).toBe("12");
  });

  it("lets the hashtag count be emptied rather than snapping to zero", async () => {
    // An empty numeric field means "use the default". Substituting 0 would mean "no hashtags",
    // which is a different instruction that nobody typed.
    const { onChange } = setupHarness({ hashtag_count: 8 });
    await openAdvanced();
    await userEvent.clear(screen.getByRole("spinbutton", { name: /hashtag count/i }));
    expect(lastSettings(onChange).hashtag_count).toBe("");
  });

  it("reports the topic keywords", async () => {
    const { onChange } = setupHarness();
    await openAdvanced();
    await userEvent.type(screen.getByPlaceholderText(/startup advice/i), "growth");
    expect(lastSettings(onChange).topic).toBe("growth");
  });

  it("reports the custom vocabulary, which is what stops a name being mis-heard", async () => {
    const { onChange } = setupHarness();
    await openAdvanced();
    await userEvent.type(screen.getByPlaceholderText(/Kubernetes, Anthropic/i), "Siobhan");
    expect(lastSettings(onChange).vocabulary).toBe("Siobhan");
  });

  it("explains why the vocabulary field exists", async () => {
    // Without the explanation it looks optional; the consequence of skipping it is a mis-heard name
    // burned into every clip's captions, which cannot be fixed after the render.
    setup();
    await openAdvanced();
    expect(screen.getByText(/burned into every clip/i)).toBeInTheDocument();
  });

  it("reports the process range as two separate bounds", async () => {
    // One field per bound, and each one has to write its own key: swapping them would clip from
    // the end of the video to its start and select nothing.
    const { onChange } = setupHarness();
    await openAdvanced();
    await userEvent.type(screen.getByRole("spinbutton", { name: /process from/i }), "30");
    expect(lastSettings(onChange).range_start).toBe("30");
    await userEvent.type(screen.getByRole("spinbutton", { name: /process to/i }), "90");
    expect(lastSettings(onChange).range_end).toBe("90");
  });

  it("reports the selection prompt", async () => {
    const { onChange } = setupHarness();
    await openAdvanced();
    await userEvent.type(screen.getByPlaceholderText(/describe the moments to find/i), "laughs");
    expect(lastSettings(onChange).selection_prompt).toBe("laughs");
  });

  it("reports the advanced flags under their own keys", async () => {
    const { onChange } = setup();
    await openAdvanced();
    await userEvent.click(screen.getByRole("checkbox", { name: /visual selection/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ visual_selection: true }));
    await userEvent.click(screen.getByRole("checkbox", { name: /subtitle files/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ subtitle_sidecar: true }));
    await userEvent.click(screen.getByRole("checkbox", { name: /generate ai titles/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ metadata: false }));
  });

  it("reflects the flags it was given rather than defaulting them", async () => {
    setup({ settings: { visual_selection: true, subtitle_sidecar: true, metadata: false } });
    await openAdvanced();
    expect(screen.getByRole("checkbox", { name: /visual selection/i })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /subtitle files/i })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /generate ai titles/i })).not.toBeChecked();
  });
});

describe("SettingsPanel caption styling", () => {
  it("keeps the effects group closed until it is asked for", async () => {
    setup();
    expect(screen.queryByRole("combobox", { name: "Caption Position" })).not.toBeInTheDocument();
    await openEffects();
    expect(screen.getByRole("combobox", { name: "Caption Position" })).toBeInTheDocument();
  });

  it("shows the visual style picker when the server reports the presets' real values", async () => {
    // Choosing between six preset *names* meant rendering a clip to find out what you had picked.
    setup();
    await openEffects();
    expect(screen.getByRole("button", { name: "Caption style hormozi" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Caption preset" })).not.toBeInTheDocument();
  });

  it("falls back to the name-only dropdown when the server reports no preset details", async () => {
    // An older backend does not send them, and losing the control entirely would make the preset
    // unchangeable rather than merely unpreviewable.
    setup({ effects: { caption_preset_details: [] } });
    await openEffects();
    expect(screen.getByRole("combobox", { name: "Caption preset" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /caption style/i })).not.toBeInTheDocument();
  });

  it("records a preset chosen from the picker", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.click(screen.getByRole("button", { name: "Caption style hormozi" }));
    expect(onChange).toHaveBeenCalledWith(changed({ caption_preset: "hormozi" }));
  });

  it("marks the current preset as the selected one", async () => {
    setup({ settings: { caption_preset: "hormozi" } });
    await openEffects();
    expect(screen.getByRole("button", { name: "Caption style hormozi" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
  });

  it("keeps the legacy template on its own key, and says which one wins", async () => {
    // Preset and template are two generations of the same setting; writing the template to
    // `caption_preset` would silently change what the newer renderer does.
    const { onChange } = setup();
    await openEffects();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Caption Template (legacy)" }),
      "boxed"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ caption_template: "boxed" }));
    expect(screen.getByText(/preset supersedes the legacy template/i)).toBeInTheDocument();
  });

  it("reports the caption position", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Caption Position" }),
      "center"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ caption_position: "center" }));
  });

  it("reports each caption toggle under its own key", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.click(screen.getByRole("checkbox", { name: /highlight keywords/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ caption_keyword_highlight: false }));
    await userEvent.click(screen.getByRole("checkbox", { name: /ai keyword highlight/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ caption_keyword_ai: true }));
    await userEvent.click(screen.getByRole("checkbox", { name: /emoji in captions/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ caption_emoji: true }));
  });

  it("puts the brand kit with the caption settings it overrides", async () => {
    // The kit's font and colours replace the preset's, so the two belong on screen together rather
    // than in separate sections that look independent.
    setup();
    await openEffects();
    expect(screen.getByRole("heading", { name: "Brand kit" })).toBeInTheDocument();
    const fonts = screen.getByRole("combobox", { name: /caption font/i });
    expect([...fonts.options].map((option) => option.value)).toEqual(["", "Anton", "Bangers"]);
  });
});

describe("SettingsPanel kinetic typography", () => {
  it("offers the styles and reveal modes the install advertises", async () => {
    // The domains are the server's; a hard-coded list would offer a style this build cannot render.
    setup();
    await openEffects();
    expect(
      [...screen.getByRole("combobox", { name: "Kinetic style" }).options].map((o) => o.value)
    ).toEqual(["pop", "bounce"]);
    expect(
      [...screen.getByRole("combobox", { name: "Reveal mode" }).options].map((o) => o.value)
    ).toEqual(["word_by_word", "cumulative"]);
  });

  it("falls back to the known styles when the install advertises none", async () => {
    // Rendering an empty dropdown would make the setting unchangeable on an older backend.
    setup({ capabilities: null });
    await openEffects();
    const styles = [...screen.getByRole("combobox", { name: "Kinetic style" }).options];
    expect(styles.length).toBeGreaterThan(1);
    expect(styles.map((option) => option.value)).toContain("typewriter");
  });

  it("labels a raw value it has no friendly name for", async () => {
    setup({ capabilities: { kinetic_typography: { styles: ["glitch"], reveal_modes: [] } } });
    await openEffects();
    expect(screen.getByRole("option", { name: "glitch" })).toBeInTheDocument();
  });

  it("reports the style and reveal mode under their own keys", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Kinetic style" }),
      "bounce"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ kinetic_style: "bounce" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Reveal mode" }),
      "cumulative"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ kinetic_reveal: "cumulative" }));
  });

  it("disables the whole group when the engine is unavailable, and says what is missing", async () => {
    // Enabling it anyway would render captions with the standard engine and report success, so the
    // reason has to be visible at the control rather than in a log.
    setup({
      engines: [
        { id: "kinetic_typography", available: false, missing: ["model:kinetic", "ffmpeg:libass"] },
      ],
    });
    await openEffects();
    const toggle = screen.getByRole("checkbox", { name: /kinetic typography captions/i });
    expect(toggle).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Kinetic style" })).toBeDisabled();
    expect(toggle.closest("fieldset")).toHaveTextContent(
      "Unavailable — missing model:kinetic, ffmpeg:libass"
    );
  });

  it("reads unchecked for an unavailable engine even when the setting says enabled", async () => {
    // A profile saved on a machine that had the engine must not present as enabled on one that does
    // not — the checkbox would be claiming something the render will not do.
    setup({
      settings: { kinetic_typography_enabled: true },
      engines: [{ id: "kinetic_typography", available: false, missing: [] }],
    });
    await openEffects();
    expect(
      screen.getByRole("checkbox", { name: /kinetic typography captions/i })
    ).not.toBeChecked();
  });

  it("says so without a list when the engine reports no missing pieces", async () => {
    setup({ engines: [{ id: "kinetic_typography", available: false }] });
    await openEffects();
    expect(
      screen.getByRole("checkbox", { name: /kinetic typography captions/i }).closest("fieldset")
    ).toHaveTextContent("Unavailable on this install");
  });

  it("leaves the group usable on an install that does not mention the engine at all", async () => {
    // Absence of a row is not evidence of absence of the engine; treating it as unavailable would
    // switch the feature off for every older backend.
    setup({ engines: [] });
    await openEffects();
    expect(screen.getByRole("checkbox", { name: /kinetic typography captions/i })).toBeEnabled();
    expect(screen.getByRole("combobox", { name: "Kinetic style" })).toBeEnabled();
  });

  it("records the toggle when the engine is available", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.click(screen.getByRole("checkbox", { name: /kinetic typography captions/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ kinetic_typography_enabled: true }));
  });
});

describe("SettingsPanel stem repair", () => {
  it("offers the mix presets, repair modes and backends the install advertises", async () => {
    setup();
    await openEffects();
    expect(
      [...screen.getByRole("combobox", { name: "Mix preset" }).options].map((o) => o.value)
    ).toEqual(["custom", "speech_focus"]);
    expect(
      [...screen.getByRole("combobox", { name: "Seam repair" }).options].map((o) => o.value)
    ).toEqual(["off", "crossfade", "spectral"]);
    expect(
      [...screen.getByRole("combobox", { name: "Separation backend" }).options].map((o) => o.value)
    ).toEqual(["auto", "ml"]);
  });

  it("shows spectral repair as unavailable with its reason, rather than hiding it", async () => {
    // A creator who has configured a model directory needs to see that the mode exists and why it
    // is not on offer; a hidden option reads as a feature that was never built.
    setup({
      capabilities: { ...CAPABILITIES, "model:htdemucs": { available: false } },
    });
    await openEffects();
    const option = screen.getByRole("option", { name: /spectral.*needs local model/i });
    expect(option).toBeDisabled();
    expect(screen.getByRole("option", { name: /crossfade/i })).toBeEnabled();
  });

  it("offers spectral repair normally when the local model is there", async () => {
    setup();
    await openEffects();
    expect(screen.getByRole("option", { name: /^spectral/i })).toBeEnabled();
    expect(screen.queryByText(/no local separation model found/i)).not.toBeInTheDocument();
  });

  it("warns that a missing model means the approximation will be used", async () => {
    setup({ capabilities: { ...CAPABILITIES, "model:htdemucs": { available: false } } });
    await openEffects();
    expect(screen.getByText(/ffmpeg approximation will be used and reported/i)).toBeInTheDocument();
  });

  it("locks the gain sliders under a named preset, and names the preset doing it", async () => {
    // The backend applies a named preset's gains over the individual fields, so live sliders would
    // display numbers that do not describe what the render will do.
    setup({ settings: { stem_mix_preset: "speech_focus" } });
    await openEffects();
    expect(screen.getByRole("slider", { name: /vocals gain/i })).toBeDisabled();
    expect(screen.getByText(/“Speech focus” preset sets the gains/)).toBeInTheDocument();
  });

  it("unlocks the gain sliders under the custom preset", async () => {
    setup();
    await openEffects();
    expect(screen.getByRole("slider", { name: /music gain/i })).toBeEnabled();
    expect(screen.queryByText(/preset sets the gains/)).not.toBeInTheDocument();
  });

  it("treats an unset preset as custom", async () => {
    // A settings blob saved before the preset existed has no key at all, and defaulting that to a
    // named preset would silently ignore gains the user had set.
    const { stem_mix_preset: _preset, ...withoutPreset } = SETTINGS;
    render(
      <SettingsPanel
        settings={withoutPreset}
        onChange={vi.fn()}
        watch={{ enabled: false, folder: "" }}
        onToggleWatch={vi.fn()}
        effects={EFFECTS}
        capabilities={CAPABILITIES}
      />
    );
    await openEffects();
    expect(screen.getByRole("slider", { name: /music gain/i })).toBeEnabled();
  });

  it("sends a gain as a number, on the key for that stem", async () => {
    // Three sliders write three keys and the labels are one word apart; a shared key would move the
    // wrong stem, and a string would be rejected by the numeric bound check.
    const { onChange } = setup();
    await openEffects();
    // A range input cannot be dragged in jsdom, so the value is set the way the browser delivers it.
    fireEvent.change(screen.getByRole("slider", { name: /vocals gain/i }), {
      target: { value: "1.5" },
    });
    expect(onChange).toHaveBeenCalledWith(changed({ stem_gain_vocals: 1.5 }));
  });

  it("takes the gain bounds and default from the install", async () => {
    const { stem_gain_music: _music, ...withoutGain } = SETTINGS;
    render(
      <SettingsPanel
        settings={withoutGain}
        onChange={vi.fn()}
        watch={{ enabled: false, folder: "" }}
        onToggleWatch={vi.fn()}
        effects={EFFECTS}
        capabilities={CAPABILITIES}
      />
    );
    await openEffects();
    const slider = screen.getByRole("slider", { name: /music gain/i });
    expect(slider).toHaveAttribute("min", "0");
    expect(slider).toHaveAttribute("max", "3");
    // The advertised default stands in for the absent setting, rather than 0 — which would mute it.
    expect(slider).toHaveValue("1");
    expect(screen.getByText(/music gain \(1\.00×\)/i)).toBeInTheDocument();
  });

  it("shows the gain to two decimals, so a small change is visible", async () => {
    setup({ settings: { stem_gain_other: 0.75 } });
    await openEffects();
    expect(screen.getByText(/other gain \(0\.75×\)/i)).toBeInTheDocument();
  });

  it("disables the repair window when seam repair is off", async () => {
    // With no repair there is no window to size, and a live slider would imply otherwise.
    setup({ settings: { stem_repair_mode: "off" } });
    await openEffects();
    expect(screen.getByRole("slider", { name: /repair window/i })).toBeDisabled();
  });

  it("sends the repair window as a number of milliseconds", async () => {
    const { onChange } = setup();
    await openEffects();
    fireEvent.change(screen.getByRole("slider", { name: /repair window/i }), {
      target: { value: "40" },
    });
    expect(onChange).toHaveBeenCalledWith(changed({ stem_repair_window_ms: 40 }));
  });

  it("takes the window bounds from the install and shows the value in ms", async () => {
    const { stem_repair_window_ms: _window, ...withoutWindow } = SETTINGS;
    render(
      <SettingsPanel
        settings={withoutWindow}
        onChange={vi.fn()}
        watch={{ enabled: false, folder: "" }}
        onToggleWatch={vi.fn()}
        effects={EFFECTS}
        capabilities={CAPABILITIES}
      />
    );
    await openEffects();
    const slider = screen.getByRole("slider", { name: /repair window/i });
    expect(slider).toHaveAttribute("min", "5");
    expect(slider).toHaveAttribute("max", "60");
    expect(screen.getByText(/repair window \(20 ms\)/i)).toBeInTheDocument();
  });

  it("reports the mix preset, seam repair mode, backend and both flags", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Mix preset" }),
      "speech_focus"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ stem_mix_preset: "speech_focus" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Seam repair" }), "off");
    expect(onChange).toHaveBeenCalledWith(changed({ stem_repair_mode: "off" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Separation backend" }),
      "ml"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ stem_backend: "ml" }));
    await userEvent.click(screen.getByRole("checkbox", { name: /declick clip edges/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ stem_declick: true }));
    await userEvent.click(screen.getByRole("checkbox", { name: /keep separated stems/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ stem_retain_stems: true }));
  });

  it("disables the whole group when the engine is unavailable", async () => {
    setup({
      settings: { stem_inpainting_enabled: true },
      engines: [{ id: "stem_inpainting", available: false, missing: ["python:demucs"] }],
    });
    await openEffects();
    const toggle = screen.getByRole("checkbox", { name: /stem-aware audio repair/i });
    expect(toggle).toBeDisabled();
    expect(toggle).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "Mix preset" })).toBeDisabled();
    expect(toggle.closest("fieldset")).toHaveTextContent("Unavailable — missing python:demucs");
  });
});

describe("SettingsPanel b-roll, look and sound", () => {
  it("reports the b-roll flag, intensity, sourcing mode and provider", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.click(screen.getByRole("checkbox", { name: /b-roll overlays/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ broll: true }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "B-roll intensity" }),
      "heavy"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ broll_intensity: "heavy" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Asset sourcing" }),
      "local_then_external"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ asset_sourcing_mode: "local_then_external" }));
  });

  it("reports the provider as typed", async () => {
    const { onChange } = setupHarness();
    await openEffects();
    await userEvent.type(screen.getByPlaceholderText(/openverse, pexels/i), "pexels");
    expect(lastSettings(onChange).broll_provider).toBe("pexels");
  });

  it("says that external sourcing degrades to local without a key", async () => {
    // Otherwise a user selects external sourcing, gets local assets, and has no way to tell why.
    setup();
    await openEffects();
    expect(screen.getByText(/requires a configured provider API key/i)).toBeInTheDocument();
  });

  it("reports the colour grade and music mood", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Color grade" }),
      "cinematic"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ color: "cinematic" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Background music" }),
      "chill"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ music: "chill" }));
  });

  it("disables the music volume until a mood is chosen", async () => {
    // There is nothing to set the volume of, and a live slider would suggest music was coming.
    setup();
    await openEffects();
    expect(screen.getByRole("slider", { name: /music volume/i })).toBeDisabled();
  });

  it("enables the music volume once there is music", async () => {
    setup({ settings: { music: "upbeat" } });
    await openEffects();
    expect(screen.getByRole("slider", { name: /music volume/i })).toBeEnabled();
  });

  it("sends the music volume as a number and shows it as a percentage", async () => {
    // The slider's own units are 0 to 0.5; the label converts, and the value does not.
    const { onChange } = setup({ settings: { music: "upbeat", music_volume: 0.2 } });
    await openEffects();
    expect(screen.getByText(/music volume \(20%\)/i)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("slider", { name: /music volume/i }), {
      target: { value: "0.3" },
    });
    expect(onChange).toHaveBeenCalledWith({
      ...SETTINGS,
      music: "upbeat",
      music_volume: 0.3,
    });
  });

  it("reports permissibility mode", async () => {
    // It is the switch that forbids added audio and external assets outright, so it has to be one
    // key rather than a set of derived ones.
    const { onChange } = setup();
    await openEffects();
    await userEvent.click(screen.getByRole("checkbox", { name: /permissibility mode/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ permissibility_mode: true }));
  });

  it("reports the emoji intensity, mode and animation", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Intensity" }), "heavy");
    expect(onChange).toHaveBeenCalledWith(changed({ emoji: "heavy" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Mode" }), "ai");
    expect(onChange).toHaveBeenCalledWith(changed({ emoji_mode: "ai" }));
    await userEvent.click(screen.getByRole("checkbox", { name: /pop animation/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ emoji_animate: true }));
  });
});

describe("SettingsPanel frame effects", () => {
  // Label and key differ for several of these, which is exactly why each pair is written down:
  // "Punch-in intro" writes `transitions`, and "Diarisation" writes the American `diarization`.
  // Anchored, because a toggle's accessible name is its label followed by its hint — and the
  // diarisation hint mentions the speaker-aware reframe by name.
  const TOGGLES = [
    [/^face-tracking reframe/i, "reframe"],
    [/^speaker-aware reframe/i, "speaker_reframe"],
    [/^diarisation/i, "diarization"],
    [/^zoom \/ ken burns/i, "zoom"],
    [/^punch-in intro/i, "transitions"],
    [/^hook title overlay/i, "hook_title"],
    [/^fade in \/ out/i, "fades"],
    [/^progress bar/i, "progress_bar"],
    [/^filler-word removal/i, "filler_removal"],
  ];

  it("writes each frame effect to its own key", async () => {
    const { onChange } = setup();
    await openEffects();
    for (const [label, key] of TOGGLES) {
      await userEvent.click(screen.getByRole("checkbox", { name: label }));
      expect(onChange).toHaveBeenCalledWith(changed({ [key]: true }));
    }
    expect(onChange).toHaveBeenCalledTimes(TOGGLES.length);
  });

  it("reflects the effects that are already on", async () => {
    setup({ settings: { reframe: true, filler_removal: true } });
    await openEffects();
    expect(screen.getByRole("checkbox", { name: /face-tracking reframe/i })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /filler-word removal/i })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /zoom \/ ken burns/i })).not.toBeChecked();
  });

  it("offers the reframe layouts and intensities the install advertises, with friendly labels", async () => {
    setup();
    await openEffects();
    const layouts = [...screen.getByRole("combobox", { name: "Reframe layout" }).options];
    expect(layouts.map((option) => option.value)).toEqual(["follow_active", "split_screen"]);
    expect(layouts.map((option) => option.textContent)).toEqual([
      "Follow active speaker",
      "Split screen",
    ]);
  });

  it("falls back to the known reframe layouts when the install advertises none", async () => {
    setup({ effects: { caption_preset_details: [] } });
    await openEffects();
    expect(
      [...screen.getByRole("combobox", { name: "Reframe layout" }).options].map((o) => o.value)
    ).toEqual(["follow_active", "split_screen"]);
    expect(
      [...screen.getByRole("combobox", { name: "Reframe intensity" }).options].map((o) => o.value)
    ).toEqual(["subtle", "standard", "heavy"]);
  });

  it("reports the reframe layout and intensity", async () => {
    const { onChange } = setup();
    await openEffects();
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Reframe layout" }),
      "split_screen"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ reframe_layout: "split_screen" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Reframe intensity" }),
      "heavy"
    );
    expect(onChange).toHaveBeenCalledWith(changed({ reframe_intensity: "heavy" }));
  });
});

describe("SettingsPanel advanced engines", () => {
  it("says nothing about engines on an install that advertises none", () => {
    // The section is the only part of this panel that appears and disappears with the backend, and
    // an empty "Advanced engines" heading would read as a broken feature.
    setup({ engines: [] });
    expect(screen.queryByRole("button", { name: /advanced engines/i })).not.toBeInTheDocument();
  });

  it("humanises the engine id into a name", async () => {
    setup({
      engines: [{ id: "stem_separation", available: true, flag: "stem_separation_enabled" }],
    });
    await openEngines();
    expect(screen.getByRole("checkbox", { name: "Stem Separation" })).toBeInTheDocument();
  });

  it("writes to the flag the engine names", async () => {
    // The flag is the engine's own contract with the backend; deriving it locally would silently
    // stop working for any engine whose flag is not id + _enabled.
    const { onChange } = setup({
      engines: [{ id: "voice_clone", available: true, flag: "use_voice_clone" }],
    });
    await openEngines();
    await userEvent.click(screen.getByRole("checkbox", { name: "Voice Clone" }));
    expect(onChange).toHaveBeenCalledWith(changed({ use_voice_clone: true }));
  });

  it("derives the flag from the id when the engine does not name one", async () => {
    const { onChange } = setup({ engines: [{ id: "beat_sync", available: true }] });
    await openEngines();
    await userEvent.click(screen.getByRole("checkbox", { name: "Beat Sync" }));
    expect(onChange).toHaveBeenCalledWith(changed({ beat_sync_enabled: true }));
  });

  it("cannot enable an unavailable engine, and says what it needs", async () => {
    setup({
      settings: { beat_sync_enabled: true },
      engines: [{ id: "beat_sync", available: false, missing: ["ffmpeg:aubio"] }],
    });
    await openEngines();
    const toggle = screen.getByRole("checkbox", { name: /beat sync/i });
    expect(toggle).toBeDisabled();
    // Unchecked despite the stored flag: the label must not claim something the render will skip.
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/unavailable — missing ffmpeg:aubio/i)).toBeInTheDocument();
  });

  it("warns when an available engine would need the network", async () => {
    // Permissibility mode blocks it, so an engine that works on the developer's machine can be a
    // no-op on a locked-down install.
    setup({ engines: [{ id: "asset_search", available: true, requires_network: true }] });
    await openEngines();
    expect(screen.getByRole("checkbox", { name: /asset search/i })).toBeEnabled();
    expect(screen.getByText(/requires network access/i)).toBeInTheDocument();
  });

  it("names an engine with no id at all rather than rendering a blank row", async () => {
    setup({ engines: [{ available: true }] });
    await openEngines();
    expect(screen.getByRole("checkbox", { name: "Engine" })).toBeInTheDocument();
  });

  it("ignores an engines value that is not a list", () => {
    // `/api/info` is not validated here, and a scalar would otherwise throw inside `.map`.
    setup({ engines: "kinetic_typography" });
    expect(screen.queryByRole("button", { name: /advanced engines/i })).not.toBeInTheDocument();
  });
});

describe("SettingsPanel captions and watch folder", () => {
  it("reports the burn-captions flag", async () => {
    const { onChange } = setup();
    await userEvent.click(screen.getByRole("checkbox", { name: /burn captions/i }));
    expect(onChange).toHaveBeenCalledWith(changed({ captions: false }));
  });

  it("keeps watch-folder mode out of the settings object", async () => {
    // It is server state, not a per-run setting: it is toggled through its own endpoint, and
    // folding it into `settings` would save it into every profile.
    const { onChange, onToggleWatch } = setup();
    await userEvent.click(screen.getByRole("checkbox", { name: /watch-folder mode/i }));
    expect(onToggleWatch).toHaveBeenCalledWith(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("turns watch-folder mode back off", async () => {
    const { onToggleWatch } = setup({ watch: { enabled: true, folder: "/srv/incoming" } });
    await userEvent.click(screen.getByRole("checkbox", { name: /watch-folder mode/i }));
    expect(onToggleWatch).toHaveBeenCalledWith(false);
  });

  it("shows which folder is being watched", async () => {
    // Without it the mode is on and there is no way to tell where files are expected.
    setup({ watch: { enabled: true, folder: "/srv/incoming" } });
    expect(screen.getByText("(/srv/incoming)")).toBeInTheDocument();
  });

  it("shows no folder when the server has not reported one", () => {
    setup({ watch: { enabled: true, folder: "" } });
    expect(screen.getByRole("checkbox", { name: /watch-folder mode/i })).toBeChecked();
    expect(screen.queryByText(/^\(/)).not.toBeInTheDocument();
  });
});
