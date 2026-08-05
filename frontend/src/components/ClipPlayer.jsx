import { useCallback, useEffect, useRef, useState } from "react";

/**
 * U3: a review player with scrubbing, frame stepping and keyboard control.
 *
 * The clip surface was a bare `<video controls>`, which is a *playback* control, not a review
 * one. Deciding whether a clip is publishable means checking the specific things that go wrong in
 * this pipeline — does it open mid-word, is the caption in sync, does the reframe lose the
 * speaker, is the last frame a blink — and every one of those needs a frame you can land on and
 * hold. The browser's own bar cannot step a frame or seek to a time you can name.
 *
 * Frame stepping assumes 30 fps, which is what the renderer normalises to (O3). It is a
 * deliberate approximation rather than a guess: reading the true frame rate needs a metadata
 * request the player does not otherwise make, and being one frame out at a different rate costs
 * nothing here, where the point is "advance by a small fixed amount I can repeat".
 */

const FRAME_SECONDS = 1 / 30;
const SKIP_SECONDS = 1;

const formatTime = (seconds) => {
  if (!Number.isFinite(seconds)) return "0:00.0";
  const whole = Math.max(0, seconds);
  const minutes = Math.floor(whole / 60);
  const rest = whole - minutes * 60;
  return `${minutes}:${rest.toFixed(1).padStart(4, "0")}`;
};

export default function ClipPlayer({ src, poster, className = "", onRegisterControls }) {
  const videoRef = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [ready, setReady] = useState(false);

  const seekTo = useCallback((seconds) => {
    const video = videoRef.current;
    if (!video || !Number.isFinite(seconds)) return;
    const limit = Number.isFinite(video.duration) ? video.duration : seconds;
    video.currentTime = Math.max(0, Math.min(limit, seconds));
  }, []);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      // Playback can be rejected (autoplay policy, a detached element); swallowing it keeps a
      // failed play from throwing an unhandled rejection into the console on every click.
      const started = video.play();
      if (started && typeof started.catch === "function") started.catch(() => {});
    } else {
      video.pause();
    }
  }, []);

  const step = useCallback(
    (frames) => {
      const video = videoRef.current;
      if (!video) return;
      // Stepping only makes sense on a still frame; playing through a step would immediately
      // undo it.
      video.pause();
      seekTo(video.currentTime + frames * FRAME_SECONDS);
    },
    [seekTo]
  );

  const skip = useCallback(
    (seconds) => {
      const video = videoRef.current;
      if (!video) return;
      seekTo(video.currentTime + seconds);
    },
    [seekTo]
  );

  // Published upward so a parent can drive the player from its own keyboard handler (U11)
  // without reaching into this component's DOM.
  useEffect(() => {
    onRegisterControls?.({ togglePlay, step, skip, seekTo });
  }, [onRegisterControls, togglePlay, step, skip, seekTo]);

  const onLoadedMetadata = (event) => {
    setDuration(event.currentTarget.duration || 0);
    setReady(true);
  };

  const progress = duration > 0 ? (current / duration) * 100 : 0;

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <video
        ref={videoRef}
        src={src}
        poster={poster}
        preload="metadata"
        playsInline
        onLoadedMetadata={onLoadedMetadata}
        onTimeUpdate={(event) => setCurrent(event.currentTarget.currentTime)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onClick={togglePlay}
        className="aspect-[9/16] w-full cursor-pointer rounded-lg bg-black object-contain"
        data-testid="clip-video"
      />

      <div className="relative">
        <input
          type="range"
          min={0}
          max={duration || 0}
          step={FRAME_SECONDS}
          value={current}
          disabled={!ready}
          onChange={(event) => seekTo(Number(event.target.value))}
          aria-label="Scrub"
          className="w-full accent-brand-accent"
        />
        {/* A filled track behind the thumb: the native range input gives no progress indication,
            and "how far through am I" is the one thing a scrub bar has to answer at a glance. */}
        <div className="pointer-events-none absolute inset-x-0 top-1/2 -z-10 h-1 -translate-y-1/2 rounded bg-slate-800">
          <div className="h-full rounded bg-brand-accent/40" style={{ width: `${progress}%` }} />
        </div>
      </div>

      <div className="flex items-center justify-between gap-1 text-[10px] text-slate-400">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => skip(-SKIP_SECONDS)}
            title="Back 1 second (←)"
            aria-label="Back one second"
            className="rounded border border-slate-700 px-1.5 py-0.5 hover:border-brand-accent"
          >
            «
          </button>
          <button
            type="button"
            onClick={() => step(-1)}
            title="Previous frame (,)"
            aria-label="Previous frame"
            className="rounded border border-slate-700 px-1.5 py-0.5 hover:border-brand-accent"
          >
            ‹
          </button>
          <button
            type="button"
            onClick={togglePlay}
            title="Play/pause (space)"
            aria-label={playing ? "Pause" : "Play"}
            className="rounded border border-slate-700 px-2 py-0.5 font-semibold hover:border-brand-accent"
          >
            {playing ? "❚❚" : "▶"}
          </button>
          <button
            type="button"
            onClick={() => step(1)}
            title="Next frame (.)"
            aria-label="Next frame"
            className="rounded border border-slate-700 px-1.5 py-0.5 hover:border-brand-accent"
          >
            ›
          </button>
          <button
            type="button"
            onClick={() => skip(SKIP_SECONDS)}
            title="Forward 1 second (→)"
            aria-label="Forward one second"
            className="rounded border border-slate-700 px-1.5 py-0.5 hover:border-brand-accent"
          >
            »
          </button>
        </div>
        <span className="tabular-nums" data-testid="clip-time">
          {formatTime(current)} / {formatTime(duration)}
        </span>
      </div>
    </div>
  );
}
