#!/usr/bin/env python
"""Are the captions on these clips in sync? Answer it with a number.

`evaluation/caption_timing.py` was library-only, so "the subtitles aren't synced" could only be
argued about. This is the entry point that settles it, and it takes no labels: it reads the cue
times out of the rendered ASS or the SRT sidecar, reads when sound actually happens out of the
clip's own audio, and reports the shift that best fits.

Reading the three columns:

  lag near zero, overlap high      synced.
  the SAME lag on every clip       a constant offset -- an arithmetic bug, and the sign says which
                                   way. Look at the clip-start subtraction and the rebase.
  lag varies in sign clip to clip  not an offset. Per-word ASR timing error, which no constant
                                   compensation fixes.
  a big lag, overlap barely up     the search found a spurious alignment in continuous speech.
                                   Noise, not a finding.

Usage:
  scripts/measure_caption_sync.py storage/clips/<job_id>
  scripts/measure_caption_sync.py storage/clips/<job_id> --ass-dir storage/temp/<job_id>
  scripts/measure_caption_sync.py storage/clips/<job_id> --json report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.caption_timing import (
    PERCEPTIBLE_MS,
    best_fit_lag_ms,
    parse_ass_events,
    parse_srt_events,
    speech_mask,
)


def _events_for(clip: Path, ass_dir: Path | None):
    """Prefer the ASS: it is what was burned in. The SRT is a different grouping (8-word cues
    versus the burn-in's 3), so it answers a slightly different question and is only a fallback."""
    candidates = []
    if ass_dir:
        candidates.append((ass_dir / f"{clip.stem}.ass", parse_ass_events, "ass"))
    candidates += [
        (clip.with_suffix(".ass"), parse_ass_events, "ass"),
        (clip.with_suffix(".srt"), parse_srt_events, "srt"),
    ]
    for path, parser, kind in candidates:
        if path.is_file():
            return parser(path), kind
    return [], ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clips_dir", help="directory of rendered clips")
    parser.add_argument("--ass-dir", default="", help="where the .ass files are, if not alongside")
    parser.add_argument("--json", default="", help="also write the full report here")
    args = parser.parse_args()

    clips_dir = Path(args.clips_dir)
    ass_dir = Path(args.ass_dir) if args.ass_dir else None
    clips = sorted(clips_dir.glob("*.mp4"))
    if not clips:
        print(f"no .mp4 files under {clips_dir}", file=sys.stderr)
        return 2

    print(f"{len(clips)} clip(s); perceptible threshold {PERCEPTIBLE_MS:.0f} ms\n")
    header = f"{'clip':26} {'src':>4} {'cues':>5} {'lag ms':>8} {'ovl@0':>7} {'ovl@lag':>8}"
    print(header)
    print("-" * len(header))

    rows, lags = [], []
    for clip in clips:
        events, kind = _events_for(clip, ass_dir)
        if not events:
            print(f"{clip.stem:26} {'--':>4}  no .ass or .srt found")
            continue
        try:
            mask = speech_mask(clip)
        except RuntimeError as exc:
            print(f"{clip.stem:26} {kind:>4}  {exc}")
            continue
        lag, at_zero, at_lag = best_fit_lag_ms(events, mask)
        lags.append(lag)
        rows.append(
            {
                "clip": clip.stem,
                "events_from": kind,
                "cues": len(events),
                "lag_ms": lag,
                "overlap_at_zero": at_zero,
                "overlap_at_lag": at_lag,
            }
        )
        print(f"{clip.stem:26} {kind:>4} {len(events):5} {lag:+8.0f} {at_zero:6.1%} {at_lag:7.1%}")

    if not rows:
        print("\nnothing measured.", file=sys.stderr)
        return 2

    print("-" * len(header))
    median = statistics.median(lags)
    outlier_signs = {lag > 0 for lag in lags if abs(lag) > PERCEPTIBLE_MS}
    print(f"median lag {median:+.0f} ms   spread {min(lags):+.0f}..{max(lags):+.0f} ms")
    if abs(median) <= PERCEPTIBLE_MS:
        print("verdict: no constant offset; median is inside the perceptible threshold.")
        if len(outlier_signs) > 1:
            print("         outliers disagree in sign -> per-word timing error, not an offset.")
    else:
        print(
            f"verdict: CONSTANT OFFSET of {median:+.0f} ms -- captions are "
            f"{'late' if median > 0 else 'early'}. Check the clip-start subtraction."
        )

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
        print(f"\nreport -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
