#!/usr/bin/env python3
"""Vendor the emoji PNGs the built-in keyword map can produce (A6, A7).

``.gitignore`` used to claim "Emoji assets are downloaded at build time", and nothing
did that. ``assets/emoji/`` was empty, so every render either fetched from a CDN at
render time or silently dropped the overlay. This script is the build step that comment
described, and its output is committed - the CDN is a convenience for refreshing the set,
never a runtime dependency.

Source: Noto Emoji 512x512 PNGs (OFL-1.1). Chosen over Twemoji's 72x72, which the overlay
scaled up to 151px - a 2.1x upscale, visibly soft. 512px is a *downscale* to every target
size we use. Noto also ships real PNGs, so vendoring needs no SVG rasteriser and the
runtime gains no dependency.

Usage:
    python scripts/fetch_emoji.py            # fetch anything missing
    python scripts/fetch_emoji.py --force    # re-fetch everything
    python scripts/fetch_emoji.py --check    # exit 1 if any glyph is missing (CI)
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from worker.effects.emoji import KEYWORD_EMOJI, emoji_filename  # noqa: E402

ASSETS = REPO_ROOT / "assets" / "emoji"
LICENCE = ASSETS / "LICENSE-NotoEmoji-OFL-1.1.txt"

BASE_URL = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/512"
LICENCE_URL = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/LICENSE"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "clipping-tool-build"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"{url} -> HTTP {response.status}")
        return response.read()


def required_glyphs() -> list[str]:
    """Every distinct glyph the built-in keyword map can emit, in a stable order."""
    return sorted(set(KEYWORD_EMOJI.values()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-fetch existing files")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify every glyph is vendored; fetch nothing, exit 1 if any is missing",
    )
    args = parser.parse_args()

    glyphs = required_glyphs()
    ASSETS.mkdir(parents=True, exist_ok=True)

    if args.check:
        missing = [g for g in glyphs if not (ASSETS / emoji_filename(g)).exists()]
        if missing:
            print(f"missing {len(missing)} of {len(glyphs)} emoji: {' '.join(missing)}")
            print("run: python scripts/fetch_emoji.py")
            return 1
        print(f"all {len(glyphs)} emoji vendored")
        return 0

    failures: list[str] = []
    fetched = skipped = 0
    for glyph in glyphs:
        name = emoji_filename(glyph)
        dest = ASSETS / name
        if dest.exists() and dest.stat().st_size > 0 and not args.force:
            skipped += 1
            continue
        # Noto names its files emoji_u<codepoints>.png, underscore-joined lower-case hex.
        stem = name[: -len(".png")].replace("-", "_")
        try:
            dest.write_bytes(_get(f"{BASE_URL}/emoji_u{stem}.png"))
            fetched += 1
        except Exception as exc:  # noqa: BLE001 - report and continue over the whole set
            failures.append(f"{glyph} ({name}): {exc}")

    if not LICENCE.exists() or args.force:
        try:
            LICENCE.write_bytes(_get(LICENCE_URL))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"LICENSE: {exc}")

    print(f"fetched {fetched}, already present {skipped}, failed {len(failures)}")
    for failure in failures:
        print(f"  FAILED {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
