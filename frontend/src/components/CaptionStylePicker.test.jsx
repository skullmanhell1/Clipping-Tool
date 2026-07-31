import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import CaptionStylePicker from "./CaptionStylePicker.jsx";

/**
 * U5: the caption style picker.
 *
 * The claim being tested is narrow on purpose: that each preset is offered, that picking one
 * reports it, that the brand kit's overrides are what the preview shows, and that the preview is
 * *labelled as an approximation*. That last one matters as much as the others - a preview that
 * silently implies it is exact is trusted once and then disbelieved permanently.
 */

const PRESETS = [
  {
    name: "karaoke",
    font: "Poppins ExtraBold",
    font_weight: 800,
    position: "bottom",
    uppercase: false,
    border_style: 1,
    spacing: 0,
    scale_x: 100,
    colors_hex: { primary: "#ffffff", highlight: "#ffe500" },
  },
  {
    name: "hormozi",
    font: "Anton",
    font_weight: 900,
    position: "center",
    uppercase: true,
    border_style: 3,
    spacing: 1,
    scale_x: 96,
    colors_hex: { primary: "#ffffff", highlight: "#00ff88" },
  },
];

describe("CaptionStylePicker", () => {
  it("offers every preset the server reported", () => {
    render(<CaptionStylePicker presets={PRESETS} value="karaoke" onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: /caption style karaoke/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /caption style hormozi/i })).toBeInTheDocument();
  });

  it("marks the active preset, so the current choice is visible", () => {
    render(<CaptionStylePicker presets={PRESETS} value="hormozi" onChange={vi.fn()} />);
    expect(screen.getByRole("button", { name: /caption style hormozi/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /caption style karaoke/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("reports the chosen preset by name", async () => {
    const onChange = vi.fn();
    render(<CaptionStylePicker presets={PRESETS} value="karaoke" onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: /caption style hormozi/i }));
    expect(onChange).toHaveBeenCalledWith("hormozi");
  });

  it("applies the preset's own case to its sample text", () => {
    render(<CaptionStylePicker presets={PRESETS} value="karaoke" onChange={vi.fn()} />);
    // hormozi uppercases; karaoke does not. Both samples are present, differing only in case.
    expect(screen.getByRole("button", { name: /caption style hormozi/i })).toHaveTextContent(
      "THIS CHANGED EVERYTHING",
    );
    expect(screen.getByRole("button", { name: /caption style karaoke/i })).toHaveTextContent(
      "This changed everything",
    );
  });

  it("previews the brand kit's font rather than the preset's", () => {
    // The kit overrides the preset in the renderer, so a preview showing the preset's font would
    // be showing a combination that will never be produced.
    const { container } = render(
      <CaptionStylePicker
        presets={PRESETS}
        value="karaoke"
        onChange={vi.fn()}
        brand={{ brand_font: "Bangers" }}
      />,
    );
    const styled = container.querySelectorAll('[style*="Bangers"]');
    expect(styled.length).toBe(PRESETS.length);
  });

  it("previews the brand kit's colours rather than the preset's", () => {
    const { container } = render(
      <CaptionStylePicker
        presets={PRESETS}
        value="karaoke"
        onChange={vi.fn()}
        brand={{ brand_primary_color: "#ff0000" }}
      />,
    );
    expect(container.innerHTML).toContain("rgb(255, 0, 0)");
  });

  it("says the preview is an approximation", () => {
    render(<CaptionStylePicker presets={PRESETS} value="karaoke" onChange={vi.fn()} />);
    expect(screen.getByText(/approximate preview/i)).toBeInTheDocument();
    // And names what it cannot show, rather than only hedging.
    expect(screen.getByText(/libass/i)).toBeInTheDocument();
  });

  it("says so when the server reported no presets, instead of rendering an empty grid", () => {
    render(<CaptionStylePicker presets={[]} value="karaoke" onChange={vi.fn()} />);
    expect(screen.getByText(/unavailable/i)).toBeInTheDocument();
  });
});
