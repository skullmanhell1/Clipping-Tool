import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import InputBar from "./InputBar.jsx";

const file = (name) => new File(["x"], name, { type: "video/mp4" });

function setup(props = {}) {
  const onChange = vi.fn();
  const onPreview = vi.fn();
  const utils = render(<InputBar onChange={onChange} onPreview={onPreview} {...props} />);
  return { onChange, onPreview, ...utils };
}

const lastCall = (mock) => mock.mock.calls.at(-1)[0];

describe("URL parsing", () => {
  it("reports a single pasted URL", async () => {
    const { onChange } = setup();
    await userEvent.type(screen.getByRole("textbox"), "https://example.test/a");
    expect(lastCall(onChange)).toEqual({ urls: ["https://example.test/a"], files: [] });
  });

  it("splits on spaces, so a pasted list is a batch", async () => {
    const { onChange } = setup();
    await userEvent.type(screen.getByRole("textbox"), "https://a.test/1 https://a.test/2");
    expect(lastCall(onChange).urls).toEqual(["https://a.test/1", "https://a.test/2"]);
  });

  it("splits on commas and newlines too", async () => {
    const { onChange } = setup();
    // The placeholder promises "space / new lines" and the parser also accepts commas; all three
    // are asserted because a user pasting from a spreadsheet gets commas.
    await userEvent.paste; // no-op, keeps the intent visible
    const box = screen.getByRole("textbox");
    await userEvent.click(box);
    await userEvent.paste("https://a.test/1,https://a.test/2\nhttps://a.test/3");
    expect(lastCall(onChange).urls).toEqual([
      "https://a.test/1",
      "https://a.test/2",
      "https://a.test/3",
    ]);
  });

  it("drops empty entries rather than sending blank URLs", async () => {
    const { onChange } = setup();
    const box = screen.getByRole("textbox");
    await userEvent.click(box);
    await userEvent.paste("  https://a.test/1 ,,  \n\n ");
    expect(lastCall(onChange).urls).toEqual(["https://a.test/1"]);
  });

  it("announces batch mode only when there is more than one URL", async () => {
    setup();
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "https://a.test/1");
    expect(screen.queryByText(/Batch mode/)).toBeNull();
    await userEvent.type(box, " https://a.test/2");
    expect(screen.getByText(/Batch mode: 2 URLs/)).toBeInTheDocument();
  });
});

describe("the preview trigger", () => {
  it("previews on blur when exactly one URL is present", async () => {
    // On blur rather than on change: previewing per keystroke would fire a metadata fetch for
    // every prefix of the URL.
    const { onPreview } = setup();
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "https://a.test/1");
    expect(onPreview).not.toHaveBeenCalled();
    await userEvent.tab();
    expect(onPreview).toHaveBeenCalledWith("https://a.test/1");
  });

  it("does not preview a batch", async () => {
    // There is one preview card, so previewing the first of five URLs would describe the wrong
    // video.
    const { onPreview } = setup();
    await userEvent.type(screen.getByRole("textbox"), "https://a.test/1 https://a.test/2");
    await userEvent.tab();
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("does not preview an empty box", async () => {
    const { onPreview } = setup();
    await userEvent.click(screen.getByRole("textbox"));
    await userEvent.tab();
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("survives being given no onPreview handler", async () => {
    // The prop is optional (`onPreview?.(...)`), and blurring must not throw when it is absent.
    const onChange = vi.fn();
    render(<InputBar onChange={onChange} />);
    await userEvent.type(screen.getByRole("textbox"), "https://a.test/1");
    await expect(userEvent.tab()).resolves.not.toThrow();
  });
});

describe("file selection", () => {
  it("reports picked files and lists them by name", async () => {
    const { onChange } = setup();
    const input = document.querySelector('input[type="file"]');
    await userEvent.upload(input, [file("a.mp4"), file("b.mp4")]);
    expect(lastCall(onChange).files.map((f) => f.name)).toEqual(["a.mp4", "b.mp4"]);
    expect(screen.getByText("a.mp4")).toBeInTheDocument();
    expect(screen.getByText("b.mp4")).toBeInTheDocument();
  });

  it("keeps URLs and files together in one report", async () => {
    // The parent decides between the URL and upload endpoints from this single object, so a
    // change to either input has to carry the current value of both.
    const { onChange } = setup();
    await userEvent.type(screen.getByRole("textbox"), "https://a.test/1");
    await userEvent.upload(document.querySelector('input[type="file"]'), [file("a.mp4")]);
    expect(lastCall(onChange)).toEqual({
      urls: ["https://a.test/1"],
      files: [expect.objectContaining({ name: "a.mp4" })],
    });
  });

  it("clears the selection and says so, without losing the URL", async () => {
    const { onChange } = setup();
    await userEvent.type(screen.getByRole("textbox"), "https://a.test/1");
    await userEvent.upload(document.querySelector('input[type="file"]'), [file("a.mp4")]);
    await userEvent.click(screen.getByRole("button", { name: "clear" }));
    expect(lastCall(onChange)).toEqual({ urls: ["https://a.test/1"], files: [] });
    expect(screen.queryByText("a.mp4")).toBeNull();
  });

  it("resets the native input on clear, so re-picking the same file still fires", async () => {
    // Without resetting `input.value` the browser treats re-selecting the same path as no
    // change and emits no event, so the file could never be re-added after clearing.
    setup();
    const input = document.querySelector('input[type="file"]');
    await userEvent.upload(input, [file("a.mp4")]);
    expect(input.value).not.toBe("");
    await userEvent.click(screen.getByRole("button", { name: "clear" }));
    expect(input.value).toBe("");
  });

  it("shows no selection row when nothing is picked", () => {
    setup();
    expect(screen.queryByText("Selected:")).toBeNull();
  });

  it("opens the hidden picker from the visible button", async () => {
    // The real <input type=file> is hidden for styling, so the button is the only way in.
    setup();
    const input = document.querySelector('input[type="file"]');
    const click = vi.spyOn(input, "click");
    await userEvent.click(screen.getByRole("button", { name: /Upload file/ }));
    expect(click).toHaveBeenCalled();
  });

  it("accepts only video files", () => {
    setup();
    expect(document.querySelector('input[type="file"]')).toHaveAttribute("accept", "video/*");
  });
});
