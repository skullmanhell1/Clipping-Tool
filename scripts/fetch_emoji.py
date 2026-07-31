#!/usr/bin/env python3
"""Vendor the emoji PNGs the built-in keyword map can produce (A6, A7, A9, A13).

``.gitignore`` used to claim "Emoji assets are downloaded at build time", and nothing
did that. ``assets/emoji/`` was empty, so every render either fetched from a CDN at
render time or silently dropped the overlay. This script is the build step that comment
described, and its output is committed - the CDN is a convenience for refreshing the set,
never a runtime dependency.

The default source is Noto Emoji 512x512 PNGs (OFL-1.1), chosen over Twemoji's 72x72, which
the overlay scaled up to 151px - a 2.1x upscale, visibly soft. 512px is a *downscale* to every
target size we use. Noto also ships real PNGs, so vendoring needs no SVG rasteriser and the
runtime gains no dependency.

``--style`` vendors one of the alternative artwork sets instead (A13). Those are not committed:
three sets over 326 glyphs would triple the repository's asset weight to ship two looks most
installs never select. Run this once with a style to make that look work offline.

Usage:
    python scripts/fetch_emoji.py                     # fetch anything missing (noto)
    python scripts/fetch_emoji.py --force             # re-fetch everything
    python scripts/fetch_emoji.py --check             # exit 1 if any glyph is missing (CI)
    python scripts/fetch_emoji.py --style openmoji    # vendor an alternative look
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from worker.effects.emoji import (  # noqa: E402
    DEFAULT_STYLE,
    EMOJI_STYLES,
    KEYWORD_EMOJI,
    emoji_filename,
    resolve_style,
    style_assets_dir,
)

#: Where each style's licence text lives upstream. Vendoring artwork without its licence is the
#: one failure here that is a legal problem rather than a rendering one, so it is per style
#: rather than a constant.
LICENCE_URLS: dict[str, tuple[str, str]] = {
    "noto": (
        "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/LICENSE",
        "LICENSE-NotoEmoji-OFL-1.1.txt",
    ),
    "twemoji": (
        "https://raw.githubusercontent.com/jdecked/twemoji/main/LICENSE-GRAPHICS",
        "LICENSE-Twemoji-CC-BY-4.0.txt",
    ),
    "openmoji": (
        "https://raw.githubusercontent.com/hfg-gmuend/openmoji/master/LICENSE.txt",
        "LICENSE-OpenMoji-CC-BY-SA-4.0.txt",
    ),
}


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
    parser.add_argument(
        "--style",
        default=DEFAULT_STYLE,
        choices=sorted(EMOJI_STYLES),
        help="artwork set to vendor (default: noto, the committed one)",
    )
    args = parser.parse_args()

    style = resolve_style(args.style)
    glyphs = required_glyphs()
    assets = style_assets_dir(style)
    assets.mkdir(parents=True, exist_ok=True)

    if args.check:
        missing = [g for g in glyphs if not (assets / emoji_filename(g)).exists()]
        if missing:
            print(f"missing {len(missing)} of {len(glyphs)} {style.name} emoji: {' '.join(missing)}")
            print(f"run: python scripts/fetch_emoji.py --style {style.name}")
            return 1
        print(f"all {len(glyphs)} {style.name} emoji vendored")
        return 0

    failures: list[str] = []
    fetched = skipped = 0
    for glyph in glyphs:
        dest = assets / emoji_filename(glyph)
        if dest.exists() and dest.stat().st_size > 0 and not args.force:
            skipped += 1
            continue
        try:
            dest.write_bytes(_get(style.remote_url(glyph)))
            fetched += 1
        except Exception as exc:  # noqa: BLE001 - report and continue over the whole set
            failures.append(f"{glyph} ({style.remote_filename(glyph)}): {exc}")

    licence_url, licence_name = LICENCE_URLS[style.name]
    licence = assets / licence_name
    if not licence.exists() or args.force:
        try:
            licence.write_bytes(_get(licence_url))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"LICENSE: {exc}")

    print(f"fetched {fetched}, already present {skipped}, failed {len(failures)}")
    for failure in failures:
        print(f"  FAILED {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
