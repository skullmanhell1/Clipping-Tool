import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import PreviewCard from "./PreviewCard.jsx";

const preview = {
  title: "How to ship faster",
  uploader: "Some Channel",
  duration: 754,
  thumbnail: "https://cdn.test/thumb.jpg",
  source: "https://video.test/watch?v=1",
};

describe("when there is nothing to show", () => {
  it("renders nothing at all, rather than an empty frame", () => {
    // It sits directly above the settings panel, so an empty card would push the whole form
    // down for no reason.
    const { container } = render(<PreviewCard preview={null} loading={false} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("while loading", () => {
  it("shows a placeholder before any metadata has arrived", () => {
    render(<PreviewCard preview={null} loading />);
    expect(screen.getByText("Fetching video info…")).toBeInTheDocument();
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  it("does not try to read fields off a null preview", () => {
    // The guard is `if (!loading && !preview) return null`, so loading with no preview *does*
    // render — and every field access on that path has to tolerate the absence.
    expect(() => render(<PreviewCard preview={null} loading />)).not.toThrow();
  });
});

describe("with metadata", () => {
  it("shows the title, uploader and formatted duration", () => {
    render(<PreviewCard preview={preview} loading={false} />);
    expect(screen.getByText("How to ship faster")).toBeInTheDocument();
    expect(screen.getByText("Some Channel")).toBeInTheDocument();
    expect(screen.getByText("12:34")).toBeInTheDocument();
  });

  it("shows the thumbnail with an empty alt, because it is decorative", () => {
    // The title is right next to it, so alt text would be read out twice by a screen reader.
    render(<PreviewCard preview={preview} loading={false} />);
    const image = document.querySelector("img");
    expect(image).toHaveAttribute("src", preview.thumbnail);
    expect(image).toHaveAttribute("alt", "");
  });

  it("links to the source, and opens it without leaking the referrer", () => {
    render(<PreviewCard preview={preview} loading={false} />);
    const link = screen.getByRole("link", { name: "source" });
    expect(link).toHaveAttribute("href", preview.source);
    expect(link).toHaveAttribute("target", "_blank");
    // `noreferrer` also implies `noopener`, which is what stops the opened page reaching back
    // into this one through window.opener.
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("omits the uploader line when there is no uploader", () => {
    render(<PreviewCard preview={{ ...preview, uploader: "" }} loading={false} />);
    expect(screen.queryByText("Some Channel")).toBeNull();
    expect(screen.getByText("How to ship faster")).toBeInTheDocument();
  });

  it("omits the source link when there is no source", () => {
    render(<PreviewCard preview={{ ...preview, source: "" }} loading={false} />);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it('falls back to "No preview" when there is no thumbnail', () => {
    render(<PreviewCard preview={{ ...preview, thumbnail: "" }} loading={false} />);
    expect(screen.getByText("No preview")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });

  it("renders a zero duration as 0:00, not as unknown", () => {
    // `formatDuration` special-cases 0 against its own falsy check; a live stream reporting 0 is
    // different from a duration that failed to parse.
    render(<PreviewCard preview={{ ...preview, duration: 0 }} loading={false} />);
    expect(screen.getByText("0:00")).toBeInTheDocument();
  });

  it("renders a missing duration as --:--", () => {
    render(<PreviewCard preview={{ ...preview, duration: null }} loading={false} />);
    expect(screen.getByText("--:--")).toBeInTheDocument();
  });
});
