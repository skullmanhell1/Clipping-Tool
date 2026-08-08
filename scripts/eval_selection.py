#!/usr/bin/env python3
"""Score clip selection against hand-labelled moments (S1).

    # 1. scaffold a label file per source, then fill in the moments by hand
    python scripts/eval_selection.py template --source /media/ep12.mp4 --out eval/labels

    # 2. check the labels before spending an hour transcribing
    python scripts/eval_selection.py validate --labels eval/labels

    # 3. transcribe once (cached), then score
    python scripts/eval_selection.py run --labels eval/labels --k 5

    # 4. after changing a selection signal, score again and compare
    python scripts/eval_selection.py run --labels eval/labels --k 5 --json after.json
    python scripts/eval_selection.py compare --before before.json --after after.json

``run`` needs ffmpeg, a Whisper model and - for the ``ai`` strategy - an LLM key. Transcripts
are cached under ``--cache``, so only the first run pays for transcription; iterating on scoring
afterwards is fast, which is the difference between a harness that gets used and one that gets
run once.

Every run scores naive baselines on the same footage. That is not decoration: whether
precision@5 of 0.40 is good depends entirely on what evenly spaced guesses score on the same
sources, and that number is not guessable from the outside.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evaluation import harness  # noqa: E402
from evaluation.dataset import TEMPLATE, DatasetError, load_dataset  # noqa: E402
from evaluation.report import render_comparison, render_text  # noqa: E402

DEFAULT_LABELS = REPO_ROOT / "eval" / "labels"
DEFAULT_CACHE = REPO_ROOT / "eval" / "cache"


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #
def cmd_template(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = Path(args.source)
    dest = out_dir / f"{source.stem}.json"
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    payload = dict(TEMPLATE)
    payload["source"] = str(source)
    payload["notes"] = ""
    duration = None
    try:
        from worker.ffmpeg_utils import probe

        duration = probe(source).duration
    except Exception:  # a template is useful without a probe
        pass

    if duration:
        # A single placeholder covering the middle of the video, so the file is valid as
        # written and the first edit is replacing a moment rather than inventing the schema.
        centre = duration / 2.0
        payload["moments"] = [
            {
                "start": round(max(0.0, centre - 15.0), 2),
                "end": round(min(duration, centre + 15.0), 2),
                "note": "REPLACE ME - the moments you would actually post",
            }
        ]
        payload["notes"] = f"duration {duration:.1f}s"

    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {dest}")
    print("Now replace the placeholder moment with the ones you would actually post.")
    print("Guidance: mark what you would post, not what looks technically clean. Aim for")
    print("3-8 moments per source, and do not try to be exhaustive - a moment you forgot")
    print("counts against the selector, so under-labelling is the one bias worth avoiding.")
    return 0


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #
def cmd_validate(args) -> int:
    try:
        dataset = load_dataset(args.labels)
    except DatasetError as exc:
        print(f"invalid dataset: {exc}", file=sys.stderr)
        return 1

    print(f"{len(dataset)} sources, {dataset.moment_count} labelled moments")
    for source in dataset.sources:
        total = sum(moment.duration for moment in source.moments)
        marker = " " if source.exists else "!"
        print(
            f" {marker} {source.name:<40} {len(source.moments):>3} moments {total:>7.1f}s labelled"
        )

    missing = dataset.missing_media()
    if missing:
        print(f"\n{len(missing)} source file(s) not found:", file=sys.stderr)
        for source in missing:
            print(f"  {source.source}", file=sys.stderr)
        print("Labels are still valid; `run` needs the media.", file=sys.stderr)

    if len(dataset) < 20:
        # S1 asks for 20 sources. Fewer still works, and the harness will happily score 3 -
        # but the result is noisy enough that a small change cannot be distinguished from it.
        print(
            f"\nNote: {len(dataset)} sources. S1 asks for 20; with fewer, a difference of a "
            "few points between runs is noise rather than signal."
        )
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _real_selector(strategy: str, clip_length: str):
    """The shipped selector, wrapped in the harness' narrower interface."""
    from worker.models import ProcessingOptions
    from worker.selection import select_moments

    def selector(source: Path, duration: float, transcript, k: int):
        options = ProcessingOptions(
            strategy=strategy,
            clip_length=clip_length,
            # The selector caps its own output; the harness truncates to k when scoring, so
            # asking for more here lets precision@k be measured against a full ranking.
            num_clips="max",
        )
        return select_moments(transcript, options, source, duration)

    return selector


def cmd_run(args) -> int:
    try:
        dataset = load_dataset(args.labels)
    except DatasetError as exc:
        print(f"invalid dataset: {exc}", file=sys.stderr)
        return 1

    missing = dataset.missing_media()
    if missing:
        print(f"{len(missing)} source file(s) missing; cannot run:", file=sys.stderr)
        for source in missing:
            print(f"  {source.source}", file=sys.stderr)
        return 1

    from worker import segmentation
    from worker.ffmpeg_utils import probe
    from worker.transcribe import transcribe

    cache_dir = Path(args.cache)

    def duration_of(source: Path) -> float:
        return probe(source).duration

    def transcript_of(source: Path):
        if args.strategy != "ai":
            # The deterministic strategies select from silence detection and length presets and
            # never read the transcript, so transcribing for them would cost minutes per source
            # to produce something nothing consults. This is also what makes the harness
            # runnable end to end without a Whisper model - useful for checking a dataset and
            # establishing the fallback's own score before paying for anything.
            from worker.transcribe import Transcript

            return Transcript(language="", segments=[])

        cached = harness.load_cached_transcript(cache_dir, source)
        if cached is not None:
            return cached
        print(f"  transcribing {source.name} (not cached)...", flush=True)
        transcript = transcribe(source)
        harness.save_cached_transcript(cache_dir, source, transcript)
        return transcript

    def segments_of(source: Path, duration: float):
        return segmentation.segment_video(
            source,
            duration,
            clip_length=args.clip_length,
            strategy="silence",
            max_clips=None,
        )

    print(f"scoring {len(dataset)} sources at k={args.k} (strategy={args.strategy})")
    selector_score, runs = harness.run_selector(
        dataset,
        _real_selector(args.strategy, args.clip_length),
        k=args.k,
        label=f"selector:{args.strategy}",
        duration_of=duration_of,
        transcript_of=transcript_of,
    )

    baseline_scores = []
    if not args.no_baselines:
        baseline_scores = harness.run_baselines(
            dataset,
            k=args.k,
            duration_of=duration_of,
            segments_of=segments_of,
        )

    report = harness.build_report(dataset, selector_score, baseline_scores, runs)
    print(render_text(report))

    if args.json:
        Path(args.json).write_text(report.to_json() + "\n", encoding="utf-8")
        print(f"wrote {args.json}")

    # A failing exit code only for a *broken* run, not a poor score: a bad result is
    # information, and a harness that fails the build on it would just get switched off.
    return 1 if report.errors and args.strict else 0


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #
def cmd_compare(args) -> int:
    from evaluation.metrics import AggregateScore, SourceScore, ThresholdScore
    from evaluation.report import Report

    def _load(path) -> Report:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))

        def _score(blob) -> AggregateScore:
            score = AggregateScore(label=blob["label"], k=int(blob["k"]))
            for source_blob in blob["sources"]:
                source = SourceScore(
                    name=source_blob["name"],
                    k=int(source_blob["k"]),
                    predictions=int(source_blob["predictions"]),
                    labels=int(source_blob["labels"]),
                    best_iou_per_label=[float(source_blob.get("mean_best_iou", 0.0))],
                )
                for entry in source_blob["thresholds"]:
                    source.thresholds[float(entry["threshold"])] = ThresholdScore(
                        threshold=float(entry["threshold"]),
                        matched=int(entry["matched"]),
                        predictions=int(entry["predictions"]),
                        labels=int(entry["labels"]),
                    )
                score.sources.append(source)
            return score

        return Report(
            dataset_size=int(raw["dataset"]["sources"]),
            moment_count=int(raw["dataset"]["labelled_moments"]),
            selector=_score(raw["selector"]),
            baselines=[_score(b) for b in raw.get("baselines", [])],
        )

    print(render_comparison(_load(args.before), _load(args.after)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    template = sub.add_parser("template", help="scaffold a label file for one source")
    template.add_argument("--source", required=True, type=Path)
    template.add_argument("--out", default=DEFAULT_LABELS, type=Path)
    template.add_argument("--force", action="store_true")
    template.set_defaults(func=cmd_template)

    validate = sub.add_parser("validate", help="check label files without transcribing")
    validate.add_argument("--labels", default=DEFAULT_LABELS, type=Path)
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="transcribe (cached), select, and score")
    run.add_argument("--labels", default=DEFAULT_LABELS, type=Path)
    run.add_argument("--cache", default=DEFAULT_CACHE, type=Path)
    run.add_argument("--k", type=int, default=5, help="how many top clips to score")
    run.add_argument("--strategy", default="ai", choices=["ai", "silence", "fixed"])
    run.add_argument("--clip-length", default="auto")
    run.add_argument("--json", type=Path, help="also write the result as JSON")
    run.add_argument(
        "--no-baselines",
        action="store_true",
        help="skip the baselines (the results become uninterpretable)",
    )
    run.add_argument("--strict", action="store_true", help="exit non-zero if any source errored")
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="diff two JSON results")
    compare.add_argument("--before", required=True, type=Path)
    compare.add_argument("--after", required=True, type=Path)
    compare.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
