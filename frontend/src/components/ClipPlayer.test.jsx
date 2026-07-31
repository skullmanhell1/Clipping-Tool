import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ClipPlayer from "./ClipPlayer.jsx";

/**
 * U3: the review player.
 *
 * jsdom has no media stack: `play()` is not implemented, `duration` is `NaN` and no `timeupdate`
 * ever fires. So these tests drive the element's properties directly and assert on what the
 * component *asks the element to do* - which is the whole of its own logic. Whether a browser
 * then decodes the video is not something a unit test can establish, and pretending otherwise
 * with a mocked media element would test the mock.
 */

const setup = (props = {}) => {
  const utils = render(<ClipPlayer src="/clips/a.mp4" {...props} />);
  const video = screen.getByTestId("clip-video");
  // Give the element a duration and a settable currentTime, which jsdom does not.
  let currentTime = 0;
  Object.defineProperty(video, "duration", { value: 30, configurable: true });
  Object.defineProperty(video, "currentTime", {
    configurable: true,
    get: () => currentTime,
    set: (value) => {
      currentTime = value;
    },
  });
  Object.defineProperty(video, "paused", { value: true, writable: true, configurable: true });
  video.play = vi.fn(() => Promise.resolve());
  video.pause = vi.fn();
  return { ...utils, video };
};

describe("ClipPlayer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("offers frame stepping, which the browser's own controls cannot do", async () => {
    // Judging a clip means landing on a specific frame: does it open mid-word, is the caption in
    // sync, is the last frame a blink.
    const { video } = setup();
    await userEvent.click(screen.getByRole("button", { name: /next frame/i }));
    expect(video.currentTime).toBeCloseTo(1 / 30, 5);
    await userEvent.click(screen.getByRole("button", { name: /previous frame/i }));
    expect(video.currentTime).toBeCloseTo(0, 5);
  });

  it("pauses before stepping, since a step during playback is immediately undone", async () => {
    const { video } = setup();
    await userEvent.click(screen.getByRole("button", { name: /next frame/i }));
    expect(video.pause).toHaveBeenCalled();
  });

  it("skips by a second in each direction", async () => {
    const { video } = setup();
    await userEvent.click(screen.getByRole("button", { name: /forward one second/i }));
    expect(video.currentTime).toBeCloseTo(1, 5);
    await userEvent.click(screen.getByRole("button", { name: /back one second/i }));
    expect(video.currentTime).toBeCloseTo(0, 5);
  });

  it("never seeks before the start or past the end", async () => {
    // A negative currentTime throws in some browsers, and seeking past the end leaves the player
    // showing nothing with no indication why.
    const { video } = setup();
    await userEvent.click(screen.getByRole("button", { name: /back one second/i }));
    expect(video.currentTime).toBe(0);
    for (let index = 0; index < 40; index += 1) {
      await userEvent.click(screen.getByRole("button", { name: /forward one second/i }));
    }
    expect(video.currentTime).toBeLessThanOrEqual(30);
  });

  it("plays and pauses from its own control", async () => {
    const { video } = setup();
    await userEvent.click(screen.getByRole("button", { name: /^play$/i }));
    expect(video.play).toHaveBeenCalledTimes(1);
  });

  it("swallows a rejected play() instead of throwing", async () => {
    // Autoplay policy and detached elements both reject; an unhandled rejection on every click
    // would fill the console and hide real errors.
    const { video } = setup();
    video.play = vi.fn(() => Promise.reject(new Error("NotAllowedError")));
    await userEvent.click(screen.getByRole("button", { name: /^play$/i }));
    expect(video.play).toHaveBeenCalled();
  });

  it("shows a time readout, so a frame can be named", () => {
    setup();
    expect(screen.getByTestId("clip-time")).toHaveTextContent("0:00.0");
  });

  it("publishes its controls upward for the keyboard handler to drive", () => {
    // U11 binds keys on the window rather than per card, so it needs a handle on the player
    // without reaching into this component's DOM.
    const onRegisterControls = vi.fn();
    setup({ onRegisterControls });
    expect(onRegisterControls).toHaveBeenCalled();
    const controls = onRegisterControls.mock.calls.at(-1)[0];
    expect(typeof controls.togglePlay).toBe("function");
    expect(typeof controls.step).toBe("function");
    expect(typeof controls.skip).toBe("function");
    expect(typeof controls.seekTo).toBe("function");
  });

  it("exposes a scrub control", () => {
    setup();
    expect(screen.getByLabelText("Scrub")).toBeInTheDocument();
  });
});
