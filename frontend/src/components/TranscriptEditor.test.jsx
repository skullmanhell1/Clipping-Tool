import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api.js";
import TranscriptEditor from "./TranscriptEditor.jsx";

/**
 * U4: the transcript editor is the surface a destructive edit is composed on, so the tests
 * concentrate on the things that would make the user cut the wrong thing — which words map to
 * which times, what happens to a selection that cannot be applied, and whether a failure to
 * load is distinguishable from a clip that simply has no speech.
 */

const TRANSCRIPT = {
  job_id: "job1",
  clip_id: "c1",
  start: 2,
  end: 8,
  duration: 6,
  trimmed: false,
  max_cuts: 200,
  words: [
    { start: 0.0, end: 0.4, text: "keep", probability: 0.99 },
    { start: 0.6, end: 1.0, text: "um", probability: 0.5 },
    { start: 1.4, end: 1.8, text: "this", probability: 0.98 },
  ],
};

const setup = (props = {}) => {
  const onApply = vi.fn();
  render(<TranscriptEditor jobId="job1" clipId="c1" onApply={onApply} {...props} />);
  return { onApply };
};

describe("TranscriptEditor (U4)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "clipTranscript").mockResolvedValue(TRANSCRIPT);
  });

  it("fetches the transcript only when it is mounted", async () => {
    // One request per clip, and most clips are never edited.
    setup();
    await waitFor(() => expect(api.clipTranscript).toHaveBeenCalledWith("job1", "c1"));
  });

  it("renders each word as its own control", async () => {
    setup();
    expect(await screen.findByRole("button", { name: /Cut “um”/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Cut “keep”/ })).toBeInTheDocument();
  });

  it("marks a clicked word as struck and offers to restore it", async () => {
    setup();
    const word = await screen.findByRole("button", { name: /Cut “um”/ });
    await userEvent.click(word);
    expect(screen.getByRole("button", { name: /Restore “um”/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("cannot apply anything until a word is struck", async () => {
    setup();
    expect(await screen.findByRole("button", { name: /apply cuts/i })).toBeDisabled();
  });

  it("sends the struck word's own time range, not its index", async () => {
    // The one conversion in the feature. Sending indices would have the backend guessing.
    const { onApply } = setup();
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    await userEvent.click(screen.getByRole("button", { name: /apply cuts/i }));
    expect(onApply).toHaveBeenCalledWith([{ start: 0.6, end: 1.0 }]);
  });

  it("merges neighbouring struck words into one cut", async () => {
    const { onApply } = setup();
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    await userEvent.click(screen.getByRole("button", { name: /Cut “this”/ }));
    await userEvent.click(screen.getByRole("button", { name: /apply cuts/i }));
    expect(onApply).toHaveBeenCalledWith([{ start: 0.6, end: 1.8 }]);
  });

  it("previews the new length before anything is rendered", async () => {
    setup();
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    // 6s clip, 0.4s struck.
    expect(screen.getByText(/new length ~0:06/)).toBeInTheDocument();
  });

  it("clears a selection without rendering", async () => {
    const { onApply } = setup();
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    await userEvent.click(screen.getByRole("button", { name: /^clear$/i }));
    expect(screen.getByRole("button", { name: /apply cuts/i })).toBeDisabled();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("explains a transcript it cannot load, rather than showing an empty editor", async () => {
    // A 409 is a normal outcome: word times come from the cache the render used.
    api.clipTranscript.mockRejectedValue(new Error("No cached transcript for this clip."));
    setup();
    expect(await screen.findByText(/no cached transcript/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /apply cuts/i })).not.toBeInTheDocument();
  });

  it("distinguishes a clip with no speech from a transcript that failed to load", async () => {
    api.clipTranscript.mockResolvedValue({ ...TRANSCRIPT, words: [] });
    setup();
    expect(await screen.findByText(/nothing to trim/i)).toBeInTheDocument();
  });

  it("warns when the word times run ahead of the media being played", async () => {
    // A clip already tightened at render time; the removed regions are not recorded on it, so
    // they cannot be compensated for and the mismatch has to be said out loud.
    api.clipTranscript.mockResolvedValue({ ...TRANSCRIPT, trimmed: true });
    setup();
    expect(await screen.findByText(/run ahead of/i)).toBeInTheDocument();
  });

  it("refuses to submit more cuts than the backend accepts", async () => {
    api.clipTranscript.mockResolvedValue({
      ...TRANSCRIPT,
      max_cuts: 1,
      words: [
        { start: 0.0, end: 0.4, text: "a" },
        { start: 1.0, end: 1.4, text: "b" },
        { start: 2.0, end: 2.4, text: "c" },
      ],
    });
    const { onApply } = setup();
    // Two non-adjacent words => two cuts, over a limit of one.
    await userEvent.click(await screen.findByRole("button", { name: /Cut “a”/ }));
    await userEvent.click(screen.getByRole("button", { name: /Cut “c”/ }));
    expect(screen.getByRole("button", { name: /apply cuts/i })).toBeDisabled();
    expect(screen.getByText(/over the limit of 1/i)).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("does not carry a selection across to a different clip", async () => {
    // Struck indices are meaningless against another clip's words.
    const { rerender } = render(
      <TranscriptEditor jobId="job1" clipId="c1" onApply={vi.fn()} />,
    );
    await userEvent.click(await screen.findByRole("button", { name: /Cut “um”/ }));
    rerender(<TranscriptEditor jobId="job1" clipId="c2" onApply={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /apply cuts/i })).toBeDisabled(),
    );
  });

  it("blocks the apply button while a re-render is running", async () => {
    setup({ applying: true });
    await screen.findByRole("button", { name: /re-rendering/i });
    expect(screen.getByRole("button", { name: /re-rendering/i })).toBeDisabled();
  });
});
