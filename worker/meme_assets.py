"""Local meme asset discovery and safe synthetic effect definitions.

Meme mode deliberately does not ship a copyrighted sound or image library.  A creator may point it
at a local directory containing their own assets and an optional ``manifest.json``.  The manifest is
where tags, license, attribution, and provenance live, so a finished clip can explain exactly which
file was used.  Small synthetic sounds are generated locally and are safe to use without an asset
download.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from config import settings

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"})
OVERLAY_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm"}
)


@dataclass(frozen=True)
class MemeAsset:
    """A local asset plus the rights/provenance facts supplied by its owner."""

    id: str
    name: str
    path: Path
    kind: str = "overlay"
    tags: tuple[str, ...] = ()
    license: str = "unknown"
    attribution: str = ""
    provenance: str = "user_supplied"
    source_url: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "kind": self.kind,
            "tags": list(self.tags),
            "license": self.license,
            "attribution": self.attribution,
            "provenance": self.provenance,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class SyntheticEffect:
    """A generated, rights-clean audio effect definition."""

    name: str
    lavfi: str
    duration: float
    license: str = "generated"
    provenance: str = "generated_locally"

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.name,
            "name": self.name,
            "kind": "sfx",
            "license": self.license,
            "provenance": self.provenance,
            "source": "synthetic",
        }


# These are deliberately simple generated tones/noise bursts.  They are definitions, not audio
# files, so no third-party or copyrighted recording enters the repository.
SYNTHETIC_EFFECTS: dict[str, SyntheticEffect] = {
    "pop": SyntheticEffect(
        "pop",
        "anoisesrc=color=white:sample_rate=48000:duration=0.10,"
        "bandpass=f=1400:width_type=o:w=1.6,"
        "afade=t=in:st=0:d=0.004,afade=t=out:st=0.02:d=0.075",
        0.10,
    ),
    "click": SyntheticEffect(
        "click",
        "anoisesrc=color=white:sample_rate=48000:duration=0.05,"
        "highpass=f=2500,afade=t=out:st=0.002:d=0.045",
        0.05,
    ),
    "whoosh": SyntheticEffect(
        "whoosh",
        "anoisesrc=color=white:sample_rate=48000:duration=0.35,"
        "bandpass=f=1200:width_type=o:w=2.0,volume=0.35,"
        "afade=t=in:st=0:d=0.03,afade=t=out:st=0.18:d=0.17",
        0.35,
    ),
    "swipe": SyntheticEffect(
        "swipe",
        "anoisesrc=color=white:sample_rate=48000:duration=0.22,"
        "bandpass=f=2400:width_type=o:w=1.6,volume=0.28,"
        "afade=t=in:st=0:d=0.02,afade=t=out:st=0.12:d=0.10",
        0.22,
    ),
    "beep": SyntheticEffect(
        "beep",
        "sine=frequency=880:sample_rate=48000:duration=0.16,"
        "afade=t=in:st=0:d=0.01,afade=t=out:st=0.10:d=0.06",
        0.16,
    ),
    "bass_drop": SyntheticEffect(
        "bass_drop",
        "sine=frequency=110:sample_rate=48000:duration=0.45,"
        "afade=t=in:st=0:d=0.02,afade=t=out:st=0.28:d=0.17",
        0.45,
    ),
    # These are deliberately original signal recipes, not recordings of branded or copyrighted
    # catchphrases. A creator can replace any name with a licensed local file of the same id.
    "ding": SyntheticEffect(
        "ding",
        "sine=frequency=1320:sample_rate=48000:duration=0.32,"
        "afade=t=in:st=0:d=0.005,afade=t=out:st=0.08:d=0.24",
        0.32,
    ),
    "dramatic_sting": SyntheticEffect(
        "dramatic_sting",
        "sine=frequency=180:sample_rate=48000:duration=0.6,"
        "afade=t=in:st=0:d=0.08,afade=t=out:st=0.35:d=0.25",
        0.6,
    ),
    "airhorn": SyntheticEffect(
        "airhorn",
        "anoisesrc=color=white:sample_rate=48000:duration=0.42,"
        "bandpass=f=480:width_type=o:w=1.2,afade=t=in:st=0:d=0.02,afade=t=out:st=0.28:d=0.14",
        0.42,
    ),
    "crickets": SyntheticEffect(
        "crickets",
        "anoisesrc=color=white:sample_rate=48000:duration=0.8,"
        "bandpass=f=3600:width_type=o:w=0.5,volume=0.22,"
        "tremolo=f=7:d=0.7,afade=t=out:st=0.55:d=0.25",
        0.8,
    ),
    "vine_boom": SyntheticEffect(
        "vine_boom",
        "sine=frequency=72:sample_rate=48000:duration=0.52,"
        "afade=t=in:st=0:d=0.015,afade=t=out:st=0.25:d=0.27",
        0.52,
    ),
    "record_scratch": SyntheticEffect(
        "record_scratch",
        "anoisesrc=color=white:sample_rate=48000:duration=0.28,"
        "bandpass=f=1800:width_type=o:w=1.4,volume=0.45,afade=t=in:st=0:d=0.01,afade=t=out:st=0.16:d=0.12",
        0.28,
    ),
}


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value).strip()
    except Exception:
        return default


def _manifest(directory: Path) -> dict[str, dict[str, Any]]:
    """Read an optional manifest, tolerating both list and mapping forms."""
    for name in ("manifest.json", "meme_assets.json", "assets.json"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            logger.warning("meme asset manifest could not be read: %s", path)
            return {}
        records = raw.get("assets", raw) if isinstance(raw, Mapping) else raw
        if isinstance(records, Mapping):
            records = [dict(value, id=key) if isinstance(value, Mapping) else {"id": key} for key, value in records.items()]
        if not isinstance(records, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in records:
            if not isinstance(item, Mapping):
                continue
            key = _safe_text(item.get("id") or item.get("name") or item.get("path"))
            if key:
                result[key] = dict(item)
        return result
    return {}


def _sidecar(path: Path) -> dict[str, Any]:
    sidecar = path.with_suffix(path.suffix + ".json")
    if not sidecar.is_file():
        sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def discover_assets(directory: str | Path | None, *, kind: str = "") -> list[MemeAsset]:
    """Discover local assets in deterministic order.

    ``kind`` may be ``"sfx"`` or ``"overlay"``.  Unknown/missing paths simply return an empty
    library, which is the desired no-op behavior for optional meme assets.
    """
    if not directory:
        return []
    root = Path(directory).expanduser()
    if not root.is_dir():
        return []
    manifest = _manifest(root)
    out: list[MemeAsset] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or path.name.startswith("."):
            continue
        suffix = path.suffix.lower()
        detected_kind = "sfx" if suffix in AUDIO_EXTENSIONS else "overlay" if suffix in OVERLAY_EXTENSIONS else ""
        if not detected_kind or (kind and detected_kind != kind):
            continue
        record = dict(manifest.get(path.name) or manifest.get(path.stem) or {})
        record.update({key: value for key, value in _sidecar(path).items() if value is not None})
        asset_id = _safe_text(record.get("id") or path.stem) or path.stem
        raw_tags = record.get("tags", ())
        if isinstance(raw_tags, str):
            tags = tuple(part.strip().lower() for part in raw_tags.split(",") if part.strip())
        elif isinstance(raw_tags, (list, tuple, set)):
            tags = tuple(_safe_text(item).lower() for item in raw_tags if _safe_text(item))
        else:
            tags = ()
        out.append(
            MemeAsset(
                id=asset_id,
                name=_safe_text(record.get("name")) or path.stem,
                path=path,
                kind=_safe_text(record.get("kind")) or detected_kind,
                tags=tags,
                license=_safe_text(record.get("license"), "unknown") or "unknown",
                attribution=_safe_text(record.get("attribution")),
                provenance=_safe_text(record.get("provenance"), "user_supplied") or "user_supplied",
                source_url=_safe_text(record.get("source_url") or record.get("url")),
                metadata=record,
            )
        )
    return out


class MemeAssetLibrary:
    """A deterministic local asset index."""

    def __init__(self, directory: str | Path | None, *, kind: str = "") -> None:
        self.directory = Path(directory).expanduser() if directory else None
        self.kind = kind
        self.assets = discover_assets(directory, kind=kind)

    def find(self, name: str = "", *, tags: tuple[str, ...] = ()) -> MemeAsset | None:
        key = _safe_text(name).lower()
        if key:
            for asset in self.assets:
                if asset.id.lower() == key or asset.name.lower() == key or asset.path.stem.lower() == key:
                    return asset
        wanted = {item.lower() for item in tags if item}
        if wanted:
            ranked = [asset for asset in self.assets if wanted.intersection(asset.tags)]
            if ranked:
                return sorted(ranked, key=lambda asset: (-len(wanted.intersection(asset.tags)), str(asset.path)))[0]
        return self.assets[0] if self.assets and not name and not wanted else None


def synthetic_effect(name: str) -> SyntheticEffect | None:
    return SYNTHETIC_EFFECTS.get(_safe_text(name).lower())


def resolve_sfx(
    name: str,
    *,
    library_dir: str | Path | None = None,
    tags: tuple[str, ...] = (),
) -> MemeAsset | SyntheticEffect | None:
    """Resolve a local/user effect first, then a generated safe definition."""
    directory = library_dir or getattr(settings, "sfx_dir", "")
    direct = Path(str(name)).expanduser() if name else Path()
    if direct.is_file() and direct.suffix.lower() in AUDIO_EXTENSIONS:
        return MemeAsset(
            id=direct.stem,
            name=direct.stem,
            path=direct,
            kind="sfx",
            license="unknown",
            provenance="user_supplied",
        )
    local = MemeAssetLibrary(directory, kind="sfx").find(name, tags=tags)
    return local or synthetic_effect(name)


def is_finite_time(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


__all__ = [
    "AUDIO_EXTENSIONS",
    "OVERLAY_EXTENSIONS",
    "MemeAsset",
    "MemeAssetLibrary",
    "SyntheticEffect",
    "SYNTHETIC_EFFECTS",
    "discover_assets",
    "resolve_sfx",
    "synthetic_effect",
]
