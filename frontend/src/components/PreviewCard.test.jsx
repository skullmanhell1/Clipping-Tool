import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import PreviewCard from "./PreviewCard.jsx";

/**
 * The preview card renders a payload it does not own: `/api/preview` is yt-dlp's metadata, and
 * yt-dlp omits fields freely — a live stream has no duration, an uploaded file has no uploader,
 * and a site with no thumbnail endpoint returns none.
 *
 * So the tests here are mostly about absence. The card reads `preview.title` unconditionally in
 * the loaded branch, which means the guard at the top (`!loading && !preview`) and the ordering of
 * the loading branch are the only things standing between a missing payload and a blank page: a
 * card that threw would take the whole submit form down with it, and the user's input with it.
 */

const PREVIEW = {
  title: "How to ship on Friday",
  uploader: "Some Channel",
  duration: 95,
  thumbnail: "https://cdn.test/thumb.jpg",
  source: "https://video.test/watch?v=1",
};

describe("PreviewCard", () => {
  it("renders nothing at all when there is no preview and nothing is loading", () => {
    // Not an empty bordered box: the card sits above the submit button, and a permanent
    // placeholder would push the primary action down the page for no information.
    const { container } = render(<PreviewCard preview={null} loading={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the title, uploader and duration of a loaded preview", () => {
    render(<PreviewCard preview={PREVIEW} loading={false} />);
    expect(screen.getByText("How to ship on Friday")).toBeInTheDocument();
    expect(screen.getByText("Some Channel")).toBeInTheDocument();
    // 95 seconds, formatted by the shared helper rather than printed raw.
    expect(screen.getByText("1:35")).toBeInTheDocument();
  });

  it("renders the thumbnail with an empty alt, because the title is already beside it", () => {
    render(<PreviewCard preview={PREVIEW} loading={false} />);
    const image = document.querySelector("img");
    expect(image).toHaveAttribute("src", "https://cdn.test/thumb.jpg");
    expect(image).toHaveAttribute("alt", "");
  });

  it("shows a placeholder instead of a broken image when there is no thumbnail", () => {
    render(<PreviewCard preview={{ ...PREVIEW, thumbnail: "" }} loading={false} />);
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText("No preview")).toBeInTheDocument();
  });

  it("shows the unknown-duration marker rather than 0:00 for a missing duration", () => {
    // A live stream reports no duration. "0:00" would read as a zero-length video, which is a
    // different and wrong claim about the source.
    render(<PreviewCard preview={{ ...PREVIEW, duration: undefined }} loading={false} />);
    expect(screen.getByText("--:--")).toBeInTheDocument();
  });

  it("omits the uploader line when the extractor did not report one", () => {
    render(<PreviewCard preview={{ ...PREVIEW, uploader: "" }} loading={false} />);
    expect(screen.queryByText("Some Channel")).not.toBeInTheDocument();
    expect(screen.getByText("How to ship on Friday")).toBeInTheDocument();
  });

  it("omits the source link when there is no source URL", () => {
    render(<PreviewCard preview={{ ...PREVIEW, source: "" }} loading={false} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("opens the source in a new tab without leaking the referrer", () => {
    render(<PreviewCard preview={PREVIEW} loading={false} />);
    const link = screen.getByRole("link", { name: "source" });
    expect(link).toHaveAttribute("href", "https://video.test/watch?v=1");
    expect(link).toHaveAttribute("target", "_blank");
    // target=_blank without rel=noreferrer hands the opened page a window.opener handle.
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("renders the loading state without a preview payload to read from", () => {
    // This is the ordering that matters: the loaded branch dereferences `preview.title`, so if
    // `loading` were checked second the first render of every paste would throw.
    render(<PreviewCard preview={null} loading={true} />);
    expect(screen.getByText("Fetching video info…")).toBeInTheDocument();
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  it("hides the previous title while a new URL is being fetched", () => {
    // Pasting a second URL must not leave the first video's title on screen next to a spinner,
    // because that combination reads as "this is the video you are about to clip".
    render(<PreviewCard preview={PREVIEW} loading={true} />);
    expect(screen.queryByText("How to ship on Friday")).not.toBeInTheDocument();
    expect(screen.getByText("Fetching video info…")).toBeInTheDocument();
  });

  it("keeps the old thumbnail visible while the next preview loads", () => {
    // The thumbnail is deliberately outside the loading branch, so the card does not collapse to
    // a grey rectangle and back on every keystroke-triggered refetch.
    render(<PreviewCard preview={PREVIEW} loading={true} />);
    expect(document.querySelector("img")).toHaveAttribute("src", "https://cdn.test/thumb.jpg");
  });
});
