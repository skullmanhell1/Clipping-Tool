// Tests for Dropdown, in particular the per-option `disabled` support.
//
// That flag exists so a feature which is present but unavailable on this install (for
// example the stem `spectral` repair mode with no local model) can be shown with its
// reason instead of being hidden — a hidden option looks like a missing feature, which
// generates support questions. A regression would silently make such options selectable
// or invisible, so it is worth pinning.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import Dropdown from "./Dropdown.jsx";

const OPTIONS = [
  { value: "off", label: "Off" },
  { value: "crossfade", label: "Crossfade" },
  { value: "spectral", label: "Spectral (needs local model)", disabled: true },
];

function renderDropdown(props = {}) {
  const onChange = vi.fn();
  render(
    <Dropdown
      label="Repair mode"
      value="crossfade"
      onChange={onChange}
      options={OPTIONS}
      {...props}
    />
  );
  return { onChange };
}

describe("Dropdown", () => {
  it("renders its label and current value", () => {
    renderDropdown();
    const select = screen.getByLabelText("Repair mode");
    expect(select).toBeInTheDocument();
    expect(select).toHaveValue("crossfade");
  });

  it("renders every option, including disabled ones", () => {
    renderDropdown();
    // The unavailable option must still be *visible*, carrying its explanation.
    expect(screen.getAllByRole("option")).toHaveLength(3);
    expect(
      screen.getByRole("option", { name: "Spectral (needs local model)" })
    ).toBeInTheDocument();
  });

  it("marks an unavailable option as disabled rather than hiding it", () => {
    renderDropdown();
    expect(screen.getByRole("option", { name: "Spectral (needs local model)" })).toBeDisabled();
    expect(screen.getByRole("option", { name: "Crossfade" })).not.toBeDisabled();
  });

  it("reports the selected value, not the event object", async () => {
    // onChange receives e.target.value; passing the event would make every caller
    // store an object where a string is expected.
    const { onChange } = renderDropdown();
    await userEvent.selectOptions(screen.getByLabelText("Repair mode"), "off");
    expect(onChange).toHaveBeenCalledWith("off");
  });

  it("can disable the whole control", () => {
    renderDropdown({ disabled: true });
    expect(screen.getByLabelText("Repair mode")).toBeDisabled();
  });

  it("is enabled by default", () => {
    renderDropdown();
    expect(screen.getByLabelText("Repair mode")).toBeEnabled();
  });

  it("treats a missing disabled flag as enabled", () => {
    // `disabled={!!o.disabled}` — an option without the key must not become disabled.
    renderDropdown({ options: [{ value: "a", label: "A" }] });
    expect(screen.getByRole("option", { name: "A" })).not.toBeDisabled();
  });
});
