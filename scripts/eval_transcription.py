#!/usr/bin/env python3
"""Compare ASR model sizes by word error rate on your own footage (M3).

``T1`` raised the default model from ``base`` to ``small`` by argument. This measures it.

Usage:
    scripts/eval_transcription.py template  --dataset eval/asr.json
    scripts/eval_transcription.py validate  --dataset eval/asr.json
    scripts/eval_transcription.py run       --dataset eval/asr.json --models base,small,medium

The dataset is a JSON list of ``{"source": "...", "reference": "..."}`` entries, where
``reference`` is either the text itself or a path to a ``.txt`` file holding it. A reference has
to be what was *said*, not what a model produced - scoring one model's output against another's
measures agreement, not accuracy, and two models can agree on the same mistake.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from evaluation import wer  # noqa: E402

TEMPLATE = [
    {
        "source": "eval/media/interview.mp4",
        "reference": "eval/media/interview.txt",
        "note": "reference may be inline text or a path to a .txt file",
    }
]


def load_dataset(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("dataset must be a JSON list")
    return data


def resolve_reference(entry: dict, base: Path) -> str:
    raw = str(entry.get("reference", "") or "")
    if not raw:
        return ""
    candidate = (base / raw) if not Path(raw).is_absolute() else Path(raw)
    # A path is only treated as one when it exists, so an inline reference that happens to look
    # like a filename is never silently read as an empty file.
    if candidate.suffix.lower() in {".txt", ".md"} and candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    return raw


def cmd_template(args) -> int:
    path = Path(args.dataset)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(TEMPLATE, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0


def cmd_validate(args) -> int:
    path = Path(args.dataset)
    base = path.parent
    try:
        entries = load_dataset(path)
    except (OSError, ValueError) as exc:
        print(f"dataset unreadable: {exc}")
        return 1

    problems = 0
    for index, entry in enumerate(entries):
        source = Path(entry.get("source", "") or "")
        if not source.is_absolute():
            source = base / source
        if not source.is_file():
            print(f"[{index}] missing source: {source}")
            problems += 1
        reference = resolve_reference(entry, base)
        words = wer.normalise(reference)
        if len(words) < 20:
            print(
                f"[{index}] reference has only {len(words)} words - too short to be a "
                f"meaningful measurement"
            )
            problems += 1
    if problems:
        print(f"\n{problems} problem(s)")
        return 1
    print(f"{len(entries)} entr(ies) look usable")
    return 0


def cmd_run(args) -> int:
    from worker import transcribe as tr

    path = Path(args.dataset)
    base = path.parent
    entries = load_dataset(path)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("no models given")
        return 1

    rows = []
    for model in models:
        original = settings.whisper_model
        settings.whisper_model = model
        results, elapsed, failures = [], 0.0, 0
        try:
            for index, entry in enumerate(entries):
                source = Path(entry.get("source", "") or "")
                if not source.is_absolute():
                    source = base / source
                reference = resolve_reference(entry, base)
                if not source.is_file() or not reference.strip():
                    failures += 1
                    continue
                started = time.monotonic()
                try:
                    # transcribe_uncached, not transcribe: a benchmark that reads the cache
                    # would report the timing of a JSON read, and would silently compare a
                    # stale transcript against the model it is meant to be measuring.
                    transcript = tr.transcribe_uncached(
                        source, vocabulary=str(entry.get("vocabulary", "") or "")
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{index}] {source.name}: {type(exc).__name__}: {exc}")
                    failures += 1
                    continue
                elapsed += time.monotonic() - started
                results.append(wer.word_error_rate(reference, transcript.text))
        finally:
            settings.whisper_model = original

        if not results:
            print(f"{model}: no usable entries")
            continue
        pooled = wer.aggregate(results)
        rows.append((model, pooled))
        print(
            f"{model}: WER {pooled.wer:.2%} over {pooled.reference_words} words "
            f"in {elapsed:.1f}s" + (f" ({failures} skipped)" if failures else "")
        )

    if not rows:
        return 1
    print()
    print(wer.format_comparison(rows))
    print()
    print("Most frequent substitutions for the best model:")
    for reference_word, heard in rows[0][1].examples:
        print(f"  {reference_word!r} heard as {heard!r}")
    return 0


def main(argv=None) -> int:
    # --dataset lives on a shared parent parser rather than only on the top-level one, so it
    # can be given *after* the subcommand. argparse otherwise requires it before, which is the
    # opposite of how everyone types it and fails with an unhelpful "unrecognized arguments".
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dataset", default="eval/asr.json")

    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("template", parents=[common]).set_defaults(func=cmd_template)
    sub.add_parser("validate", parents=[common]).set_defaults(func=cmd_validate)
    run = sub.add_parser("run", parents=[common])
    run.add_argument(
        "--models", default="base,small", help="comma-separated faster-whisper model names"
    )
    run.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
