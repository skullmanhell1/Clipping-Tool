"""Choose the file a platform will receive, and explain that choice.

A clip can have a canonical render plus one file per destination. The canonical file is the
fallback, never a stand-in for a different destination's geometry: sending a 9:16 Shorts file
to YouTube landscape because it is the only variant present is worse than sending the original
and saying so.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Product names that share a publisher and a variant profile with their parent.
INSTAGRAM_NAMES = frozenset({"instagram", "instagram_reels", "reels"})


def variant_platform(requested_platform: str) -> str:
    """The variant key for a requested destination.

    ``instagram_reels`` names the product, while the publisher and output profile are
    registered as ``instagram``. Keeping this mapping in one place means the manager, the
    readiness check, and the UI cannot disagree about which file is eligible.
    """
    key = str(requested_platform or "").strip().casefold()
    return "instagram" if key in INSTAGRAM_NAMES else key


def select_delivery(
    clip: Any,
    requested_platform: str,
    video_path: str | Path,
) -> tuple[Path, dict[str, Any]]:
    """Return the file for ``requested_platform`` and a record describing it.

    Selection is deliberately exact. A YouTube landscape request never picks the Shorts
    variant just because both route through the YouTube publisher. A missing variant falls
    back to the canonical clip and records why, so an operator can see that the delivered
    geometry may not be the requested one.
    """
    canonical = Path(video_path)
    requested = str(requested_platform or "").strip().casefold()
    target = variant_platform(requested)
    for variant in getattr(clip, "variants", None) or []:
        if not isinstance(variant, dict):
            continue
        record_platform = variant_platform(str(variant.get("platform") or ""))
        if record_platform != target:
            continue
        filename = str(variant.get("filename") or "").strip()
        if not filename or Path(filename).name != filename:
            logger.warning(
                "ignoring unsafe variant filename %r for clip %s",
                filename,
                getattr(clip, "id", ""),
            )
            continue
        candidate = canonical.parent / filename
        if not candidate.is_file():
            logger.warning(
                "variant %s for clip %s is missing; publishing the canonical clip",
                filename,
                getattr(clip, "id", ""),
            )
            return canonical, {
                "kind": "canonical",
                "platform": requested,
                "filename": canonical.name,
                "fallback": "variant_file_missing",
                "variant_filename": filename,
            }
        return candidate, {
            "kind": "variant",
            "platform": target,
            "requested_platform": requested,
            "filename": filename,
            "aspect": str(variant.get("aspect") or ""),
            "width": variant.get("width"),
            "height": variant.get("height"),
        }
    return canonical, {
        "kind": "canonical",
        "platform": requested,
        "filename": canonical.name,
    }
