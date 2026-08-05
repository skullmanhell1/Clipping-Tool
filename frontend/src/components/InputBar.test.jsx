import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import InputBar from "./InputBar.jsx";

/**
 * The input bar is the only place a job is described, and it reports upward through a single
 * `onChange({ urls, files })` call rather than owning a submit button. Everything the parent knows
 * about what to submit comes from that payload, so a parsing mistake here is not a display bug —
 * it submits the wrong work, or nothing at all, with no error anywhere.
 *
 * The two behaviours worth pinning are the parse (one text field has to mean "one URL" or "twelve
 * URLs" depending only on whitespace) and the preview trigger, which fires on blur for a single
 * URL only. That condition exists because a preview is a yt-dlp metadata fetch per URL: firing it
 * for a pasted list of twenty would be twenty network round trips nobody asked for.
 */

const setup = () => {
  const onChange = vi.fn();
  const onPreview = vi.fn();
  const utils = render(<InputBar onChange={onChange} onPreview={onPreview} />);
  return { ...utils, onChange, onPreview };
};

const lastPayload = (onChange) => onChange.mock.calls.at(-1)[0];

const videoFile = (name) => new File(["binary"], name, { type: "video/mp4" });

const urlField = () => screen.getByPlaceholderText(/paste a video url/i);

const fileField = () => document.querySelector('input[type="file"]');

describe("InputBar URL parsing", () => {
  it("reports a single pasted URL", async () => {
    const { onChange } = setup();
    await userEvent.type(urlField(), "https://one.test/v");
    expect(lastPayload(onChange)).toEqual({ urls: ["https://one.test/v"], files: [] });
  });

  it("splits on spaces, commas and newlines alike", async () => {
    // The placeholder promises "separated by space / new lines" and the help text talks about
    // batches, so all three separators have to mean the same thing.
    const { onChange } = setup();
    await userEvent.type(urlField(), "https://a.test,https://b.test https://c.test");
    expect(lastPayload(onChange).urls).toEqual([
      "https://a.test",
      "https://b.test",
      "https://c.test",
    ]);
  });

  it("drops empty segments left by trailing separators", async () => {
    // A trailing comma is what a paste from a spreadsheet looks like. An empty string forwarded
    // as a URL becomes a job that fails at download time instead of never being created.
    const { onChange } = setup();
    await userEvent.type(urlField(), "https://a.test, ");
    expect(lastPayload(onChange).urls).toEqual(["https://a.test"]);
  });

  it("reports an empty list once the field is cleared", async () => {
    // The parent must learn that there is nothing to submit any more; keeping the last non-empty
    // parse would let a cleared field still submit the URL the user just deleted.
    const { onChange } = setup();
    await userEvent.type(urlField(), "https://a.test");
    await userEvent.clear(urlField());
    expect(lastPayload(onChange)).toEqual({ urls: [], files: [] });
  });

  it("announces batch mode only once there is more than one URL", async () => {
    setup();
    await userEvent.type(urlField(), "https://a.test");
    expect(screen.queryByText(/batch mode/i)).not.toBeInTheDocument();
    await userEvent.type(urlField(), " https://b.test");
    expect(screen.getByText(/batch mode: 2 urls/i)).toBeInTheDocument();
  });
});

describe("InputBar preview trigger", () => {
  it("previews a single URL when the field loses focus", async () => {
    const { onPreview } = setup();
    await userEvent.type(urlField(), "https://one.test/v");
    await userEvent.tab();
    expect(onPreview).toHaveBeenCalledWith("https://one.test/v");
  });

  it("does not preview a batch, which would be one metadata fetch per URL", async () => {
    const { onPreview } = setup();
    await userEvent.type(urlField(), "https://a.test https://b.test");
    await userEvent.tab();
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("does not preview an empty field", async () => {
    const { onPreview } = setup();
    await userEvent.click(urlField());
    await userEvent.tab();
    expect(onPreview).not.toHaveBeenCalled();
  });
});

describe("InputBar file selection", () => {
  it("reports selected files alongside whatever URLs are typed", async () => {
    // Both inputs feed one payload, so choosing a file must not discard a URL that is already
    // typed — the parent would silently submit half of what the user set up.
    const { onChange } = setup();
    await userEvent.type(urlField(), "https://a.test");
    await userEvent.upload(fileField(), videoFile("morning.mp4"));
    expect(lastPayload(onChange).urls).toEqual(["https://a.test"]);
    expect(lastPayload(onChange).files.map((file) => file.name)).toEqual(["morning.mp4"]);
  });

  it("accepts several files at once, because uploads are a batch case too", async () => {
    const { onChange } = setup();
    await userEvent.upload(fileField(), [videoFile("a.mp4"), videoFile("b.mp4")]);
    expect(lastPayload(onChange).files).toHaveLength(2);
  });

  it("lists the chosen filenames so the picker's own dialog need not be trusted", async () => {
    setup();
    await userEvent.upload(fileField(), [videoFile("a.mp4"), videoFile("b.mp4")]);
    expect(screen.getByText("a.mp4")).toBeInTheDocument();
    expect(screen.getByText("b.mp4")).toBeInTheDocument();
  });

  it("clearing removes the files from the payload and from the input element", async () => {
    // Resetting `fileRef.current.value` is the part that is easy to leave out: the payload would
    // look empty while the DOM input still held the file, so re-picking the same file fires no
    // change event and the file quietly comes back on the next emit.
    const { onChange } = setup();
    await userEvent.upload(fileField(), videoFile("a.mp4"));
    await userEvent.click(screen.getByRole("button", { name: "clear" }));
    expect(lastPayload(onChange)).toEqual({ urls: [], files: [] });
    expect(fileField().value).toBe("");
    expect(screen.queryByText("a.mp4")).not.toBeInTheDocument();
  });

  it("offers no clear button until something is selected", () => {
    setup();
    expect(screen.queryByRole("button", { name: "clear" })).not.toBeInTheDocument();
  });

  it("opens the hidden picker from the visible button", async () => {
    // The real <input type=file> is hidden for styling, so the button is the only way in. If the
    // click were not forwarded there would be no way to upload anything at all.
    setup();
    const opened = vi.spyOn(fileField(), "click").mockImplementation(() => {});
    await userEvent.click(screen.getByRole("button", { name: /upload file\(s\)/i }));
    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("only offers video files in the picker", () => {
    setup();
    expect(fileField()).toHaveAttribute("accept", "video/*");
    expect(fileField()).toHaveAttribute("multiple");
  });
});
