import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import BrandKitPanel from "./BrandKitPanel.jsx";

/**
 * U6 / I8: the brand kit panel.
 *
 * The property worth testing is that every control is **additive**: an untouched field must leave
 * the caption preset's own value alone rather than writing a default over it. A panel that
 * initialises its colour inputs to white and then sends white would silently override every
 * preset's colours the first time anyone opened the settings.
 */

const setup = (settings = {}) => {
  const onChange = vi.fn();
  const utils = render(
    <BrandKitPanel
      settings={settings}
      onChange={onChange}
      fonts={[{ name: "Anton" }, { name: "Bangers" }]}
    />,
  );
  return { ...utils, onChange, settings };
};

describe("BrandKitPanel", () => {
  it("offers only the vendored fonts, plus 'use the style's font'", () => {
    setup();
    const select = screen.getByRole("combobox", { name: /caption font/i });
    const values = [...select.options].map((option) => option.value);
    // The empty value is what keeps the field additive.
    expect(values).toEqual(["", "Anton", "Bangers"]);
  });

  it("reports a font choice without touching anything else", async () => {
    const { onChange } = setup({ brand_cta: "Follow" });
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /caption font/i }),
      "Anton",
    );
    expect(onChange).toHaveBeenCalledWith({ brand_cta: "Follow", brand_font: "Anton" });
  });

  it("can clear a colour back to the style's default", async () => {
    // Without this the only way out of a chosen colour would be to guess the preset's own value.
    const { onChange } = setup({ brand_primary_color: "#ff0000" });
    await userEvent.click(
      screen.getAllByRole("button", { name: /use style default/i })[0],
    );
    expect(onChange).toHaveBeenCalledWith({ brand_primary_color: "" });
  });

  it("sends nothing for an untouched kit", () => {
    const { onChange } = setup();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("disables the logo controls until a logo is set", () => {
    setup();
    expect(screen.getByRole("combobox", { name: /logo position/i })).toBeDisabled();
    expect(screen.getByLabelText("Logo size")).toBeDisabled();
    expect(screen.getByLabelText("Logo opacity")).toBeDisabled();
  });

  it("enables the logo controls once a logo is set", () => {
    setup({ brand_logo: "./logo.png" });
    expect(screen.getByRole("combobox", { name: /logo position/i })).toBeEnabled();
    expect(screen.getByLabelText("Logo size")).toBeEnabled();
  });

  it("offers the four corners and nothing else", () => {
    setup({ brand_logo: "./logo.png" });
    const select = screen.getByRole("combobox", { name: /logo position/i });
    expect([...select.options].map((option) => option.value)).toEqual([
      "top_left",
      "top_right",
      "bottom_left",
      "bottom_right",
    ]);
  });

  it("says the logo is a server-side path", () => {
    // It is not an upload, and a user who expects a file picker needs to know that here rather
    // than after a render silently produced no watermark.
    setup();
    expect(screen.getByText(/path on the machine running the renderer/i)).toBeInTheDocument();
  });

  it("explains that the CTA also becomes the end card", () => {
    setup();
    expect(screen.getByText(/end card/i)).toBeInTheDocument();
  });

  it("shows the logo size and opacity as percentages", () => {
    setup({ brand_logo: "./logo.png", brand_logo_scale: 0.2, brand_logo_opacity: 0.5 });
    expect(screen.getByText(/20% of width/i)).toBeInTheDocument();
    expect(screen.getByText(/50%/)).toBeInTheDocument();
  });
});
