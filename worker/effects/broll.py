"""B-roll / image & clip overlay auto-insertion.

Plans contextual b-roll overlays over key phrases in a clip, resolves the
overlay assets through pluggable providers (a local library folder + an optional
external BYOK adapter), and orchestrates the two via :class:`Broll_Engine`.

Everything in this module is pure planning / resolution logic — it performs no
ffmpeg work (that lives in the compositor) and never requires network access
unless an operator explicitly configures an external provider *and* enables
external sourcing. The design mirrors ``worker/effects/emoji.py``: a pure
``plan_*`` function that consumes the clip-relative ``Word_Timeline`` plus a
dependency-injected asset resolver so tests can supply fakes.

Pipeline shape::

    plan_broll_cues(words, duration, intensity=...) -> list[BrollCue]   (pure)
    resolve_asset(keyword, mode, local, external)   -> AssetRef | None  (DI)
    Broll_Engine(options, local=, external=)
        .plan(words, duration)  -> list[BrollCue]        (mode-aware planning)
        .resolve(cues)          -> list[BrollCue]        (assets attached / dropped)

Because ``pipeline.py`` rebases the ``Word_Timeline`` *before* the compositor
runs, ``plan_broll_cues`` inherently never places a cue inside a removed
interval (Req 11.2) — it simply consumes the already-rebased words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from config import settings
from worker.effects.caption_presets import plan_keywords

# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AssetRef:
    """A resolved overlay asset plus its provenance / licensing metadata.

    ``license == ""`` means the license is unknown; such assets are treated as
    unusable and dropped by the engine (Req 20.3). ``provider`` is ``"local"``
    for operator-supplied library assets or the external provider's name for
    downloaded assets.
    """

    path: str
    kind: str                 # "image" | "video"
    provider: str
    source_id: str = ""
    license: str = ""
    attribution: str = ""


@dataclass(frozen=True)
class BrollCue:
    """A planned overlay occurrence.

    ``[start, end]`` is the clip-relative on-screen window; ``keyword`` is the
    source phrase that triggered the cue; ``asset`` is attached later by the
    engine (``None`` until an asset is resolved).
    """

    start: float
    end: float
    keyword: str
    asset: Optional[AssetRef] = None


# Intensity -> (max cue count, max total on-screen seconds) per clip (Req 7.4).
BROLL_INTENSITY: dict[str, tuple[int, float]] = {
    "off": (0, 0.0),
    "subtle": (2, 6.0),
    "standard": (4, 12.0),
    "heavy": (7, 20.0),
}


# --------------------------------------------------------------------------- #
# Cue planning (pure)
# --------------------------------------------------------------------------- #
def _keyword_text(word: Any) -> str:
    """Best-effort extraction of a word's display text (stripped)."""
    if isinstance(word, str):
        return word.strip()
    return str(getattr(word, "text", "") or "").strip()


def plan_broll_cues(
    words: list,
    duration: float,
    *,
    intensity: str = "off",
    hold: float = 2.5,
    min_gap: float = 3.0,
    keyword_fn: Optional[Callable[[list], list]] = None,
) -> list[BrollCue]:
    """Plan b-roll cues from a (rebased) clip-relative word timeline.

    Args:
        words: clip-relative words (objects with ``.start``/``.end``/``.text``).
            Already post-filler-removal, so cues never land in a removed
            interval (Req 11.2).
        duration: final clip duration (s); every cue window is bounded to
            ``[0, duration]`` (Reqs 7.3, 11.3).
        intensity: ``off`` | ``subtle`` | ``standard`` | ``heavy`` — caps both
            the cue count and total on-screen seconds (Req 7.4).
        hold: how long each overlay stays on screen (s).
        min_gap: minimum spacing between consecutive cue *starts* (s).
        keyword_fn: injectable ``words -> list[int]`` selector returning the
            indices of key phrases. Defaults to the deterministic content-word
            heuristic shared with the caption keyword planner.

    Returns a chronologically ordered list of cues (``[]`` when disabled or when
    no key phrase qualifies). Emits at most one cue per selected phrase
    (Req 7.1), each timed to its source word's ``start`` (Req 7.2).
    """
    max_count, max_total = BROLL_INTENSITY.get(intensity, (0, 0.0))
    if max_count <= 0 or max_total <= 0.0 or not words or duration <= 0:
        return []

    words = list(words)
    if keyword_fn is None:
        indices = sorted(plan_keywords(words))
    else:
        try:
            indices = sorted({int(i) for i in keyword_fn(words)})
        except Exception:
            indices = []

    cues: list[BrollCue] = []
    total_onscreen = 0.0
    last_start: Optional[float] = None
    for i in indices:
        if i < 0 or i >= len(words):
            continue
        word = words[i]
        start = max(0.0, min(float(getattr(word, "start", 0.0)), duration))
        if start >= duration:
            continue
        # Space consecutive cue starts by at least ``min_gap``.
        if last_start is not None and (start - last_start) < min_gap:
            continue
        end = min(start + hold, duration)
        window = end - start
        if window <= 0:
            continue
        # Stop once adding this cue would exceed the total on-screen cap.
        if total_onscreen + window > max_total + 1e-9:
            break
        # Clamp the (rounded) window to the raw duration so display rounding can
        # never push a cue's end beyond the clip bounds (Reqs 7.3, 11.3).
        cues.append(
            BrollCue(
                start=min(round(start, 3), duration),
                end=min(round(end, 3), duration),
                keyword=_keyword_text(word),
            )
        )
        total_onscreen += window
        last_start = start
        if len(cues) >= max_count:
            break
    return cues


# --------------------------------------------------------------------------- #
# Asset providers
# --------------------------------------------------------------------------- #
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _norm_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens for filename matching."""
    return _TOKEN_RE.findall((text or "").lower())


def _norm_stem(text: str) -> str:
    """Collapse text to a lowercase alphanumeric run for contains matching."""
    return "".join(_norm_tokens(text))


class AssetProvider(Protocol):
    """Pluggable source of overlay assets (Req 21.1)."""

    name: str

    def search(self, keyword: str) -> Optional[AssetRef]:  # pragma: no cover
        ...


class LocalProvider:
    """Resolves assets from an operator-supplied library folder (no network).

    Assets are treated as operator-supplied and therefore usable: the returned
    :class:`AssetRef` records ``provider="local"``, ``license="local"`` and the
    source ``path`` (Req 12.2). A simple case-insensitive *contains* match on
    the file stem is used. Images (``.png/.jpg/.jpeg/.webp``) become kind
    ``"image"``; videos (``.mp4/.mov/.webm``) become kind ``"video"``.
    """

    name = "local"

    def __init__(self, root: "str | Path | None" = None):
        self.root = Path(root) if root is not None else Path(settings.broll_dir)

    def search(self, keyword: str) -> Optional[AssetRef]:
        tokens = [t for t in _norm_tokens(keyword) if len(t) >= 3]
        if not tokens:
            return None
        try:
            entries = sorted(p for p in self.root.iterdir() if p.is_file())
        except Exception:
            # Missing directory / unreadable => no local match (no network).
            return None
        for path in entries:
            ext = path.suffix.lower()
            if ext in _IMAGE_EXTS:
                kind = "image"
            elif ext in _VIDEO_EXTS:
                kind = "video"
            else:
                continue
            stem = _norm_stem(path.stem)
            if not stem:
                continue
            if any(tok in stem or stem in tok for tok in tokens):
                return AssetRef(
                    path=str(path),
                    kind=kind,
                    provider="local",
                    source_id="",
                    license="local",
                    attribution="",
                )
        return None


def _default_external_downloader(
    keyword: str, api_key: str, base_url: str, cache_dir: Path
) -> Optional[AssetRef]:
    """Safe, offline default downloader.

    The concrete provider-specific HTTP/download logic is intentionally not
    implemented here so the module never performs network I/O by default; a
    real adapter is injected via ``downloader=`` (Req 21.1). Returns ``None``.
    """
    return None


class ExternalProvider:
    """Provider-agnostic BYOK stock-asset adapter.

    Requires an operator-supplied API key + base URL and delegates the actual
    fetch to an injectable ``downloader(keyword, api_key, base_url, cache_dir)``
    returning an :class:`AssetRef` (or ``None``). Never raises — any failure
    degrades to ``None`` (Req 9.2). The provider name / source id / license /
    attribution are taken from the download result (Req 12.1). An asset with an
    unknown (empty) license is still returned so the engine's unknown-license
    drop (Req 20.3) is the uniform guarantee.
    """

    def __init__(
        self,
        api_key: "str | None",
        base_url: "str | None",
        downloader: Optional[Callable[..., Optional[AssetRef]]] = None,
        cache_dir: "str | Path | None" = None,
        name: str = "external",
    ):
        self.api_key = api_key or ""
        self.base_url = base_url or ""
        self.cache_dir = (
            Path(cache_dir) if cache_dir is not None else Path(settings.broll_cache_dir)
        )
        self._downloader = downloader or _default_external_downloader
        self.name = name or "external"

    @property
    def has_key(self) -> bool:
        """True when a BYOK API key is configured (Req 8.4)."""
        return bool(self.api_key)

    def search(self, keyword: str) -> Optional[AssetRef]:
        if not keyword or not self.has_key:
            return None
        try:
            result = self._downloader(
                keyword, self.api_key, self.base_url, self.cache_dir
            )
        except Exception:
            return None
        if result is None:
            return None
        if not isinstance(result, AssetRef):
            return None
        # Ensure the provider name is recorded even if the downloader omits it.
        if not result.provider:
            result = replace(result, provider=self.name)
        return result


def _has_known_license(asset: Optional[AssetRef]) -> bool:
    """True when the asset carries a non-empty license string (Req 20.3)."""
    return asset is not None and bool((asset.license or "").strip())


def resolve_asset(
    keyword: str,
    mode: str,
    local: Optional[AssetProvider],
    external: Optional[ExternalProvider],
) -> Optional[AssetRef]:
    """Resolve one overlay asset honouring the asset-sourcing mode (Req 8).

    Semantics:
        * ``off``                -> ``None`` (no provider is queried, Req 8.5).
        * ``local_only``         -> only the local provider is queried; the
          external provider / downloader is never touched (Req 8.2).
        * ``local_then_external``-> local first; on a miss, the external
          provider is queried **only** when it is present and has an API key
          (Req 8.3). With no external / no key this behaves as ``local_only``
          (Req 8.4).

    Assets with an unknown (empty) license are rejected here too so callers
    uniformly receive usable assets (Req 20.3). Never raises.
    """
    if mode == "off":
        return None

    asset: Optional[AssetRef] = None
    if local is not None:
        try:
            asset = local.search(keyword)
        except Exception:
            asset = None

    if asset is None and mode == "local_then_external":
        if external is not None and getattr(external, "has_key", False):
            try:
                asset = external.search(keyword)
            except Exception:
                asset = None

    if asset is None:
        return None
    if not _has_known_license(asset):
        return None
    return asset


# --------------------------------------------------------------------------- #
# Provenance helper (for ClipResult.broll_assets)
# --------------------------------------------------------------------------- #
def broll_asset_record(cue: BrollCue) -> dict:
    """Shape a resolved cue's provenance for ``ClipResult.broll_assets``.

    Returns ``{provider, source_id, license, attribution, keyword, path}``
    (Reqs 12.1, 12.2, 20.1). Assumes ``cue.asset`` is set.
    """
    asset = cue.asset
    return {
        "provider": asset.provider,
        "source_id": asset.source_id,
        "license": asset.license,
        "attribution": asset.attribution,
        "keyword": cue.keyword,
        "path": asset.path,
    }


# --------------------------------------------------------------------------- #
# ffmpeg overlay graph (pure string builder)
# --------------------------------------------------------------------------- #
# Overlay width as a fraction of the frame width (~half-frame, Req 10).
_BROLL_SIZE_FRAC = 0.5
# A few upper / centre vertical placement slots (fractions of frame height),
# rotated through so consecutive overlays don't stack on the exact same spot.
_BROLL_Y_SLOTS = (0.12, 0.20, 0.10, 0.16)


def build_broll_overlay(
    cues: list[BrollCue],
    base_label: str,
    out_label: str,
    *,
    width: int,
    height: int,
    fps: float,
    input_offset: int,
) -> tuple[list[str], str, list[str]]:
    """Build ffmpeg inputs + a ``-filter_complex`` snippet for b-roll overlays.

    Mirrors :func:`worker.effects.emoji.build_overlay`: pure string building
    only (no ffmpeg run). ``cues`` must already be resolved (``asset != None``).

    Args:
        cues: resolved b-roll cues (each with a non-``None`` :class:`AssetRef`).
        base_label: label of the base video stream (no brackets), e.g. ``vlook``.
        out_label: label to assign the final overlaid stream (no brackets).
        width/height: target frame size (overlays are scaled to a fraction of
            ``width``).
        fps: output frame rate; drives the one-frame minimum for zero-length
            windows (Req 10.5).
        input_offset: ffmpeg input index of the first b-roll asset (Req 10.3);
            indices are assigned contiguously with no collision.

    Returns ``(input_args, filtergraph, applied_notes)``:
        * Image assets loop as still inputs bounded to the on-screen window
          (``-loop 1 -t <dur> -i``); video assets carry their own PTS and are
          trimmed to the window (``-i``).
        * Each overlay is scaled to ~0.5 * frame width (aspect preserved),
          placed in the upper/centre area, and gated with
          ``enable='between(t,start,end)'`` (Req 10.4).
        * Zero-length windows get a one-frame minimum on screen (Req 10.5).
        * ``applied_notes`` holds one ``"broll:<keyword>"`` per composited cue
          (Req 9.4).

    When there are no resolved cues, returns ``([], "", [])`` so the caller
    keeps ``base_label`` and renders b-roll-disabled (Reqs 9.3, 10.6).
    """
    resolved = [c for c in cues if getattr(c, "asset", None) is not None]
    if not resolved:
        return [], "", []

    fps = float(fps) if fps and fps > 0 else 30.0
    min_dur = 1.0 / fps
    overlay_w = max(2, int(width * _BROLL_SIZE_FRAC))

    input_args: list[str] = []
    steps: list[str] = []
    notes: list[str] = []
    current = base_label
    n = len(resolved)
    for i, cue in enumerate(resolved):
        asset = cue.asset
        idx = input_offset + i
        start = max(0.0, float(cue.start))
        end = max(start, float(cue.end))
        # Zero-length windows get a one-frame minimum on-screen (Req 10.5).
        disp_dur = max(end - start, min_dur)
        disp_end = start + disp_dur

        if asset.kind == "video":
            # Video assets keep their own PTS; trim to the window and shift so
            # the trimmed segment plays during [start, disp_end].
            input_args += ["-i", asset.path]
            prep = (
                f"[{idx}:v]trim=start=0:end={disp_dur:.3f},"
                f"setpts=PTS-STARTPTS+{start:.3f}/TB,"
                f"scale={overlay_w}:-1,format=rgba[bpre{i}]"
            )
        else:
            # Still images loop for the window duration (like emoji), shifted so
            # the looped frames line up with [start, disp_end].
            input_args += ["-loop", "1", "-t", f"{disp_dur:.3f}", "-i", asset.path]
            prep = (
                f"[{idx}:v]scale={overlay_w}:-1,format=rgba,"
                f"setpts=PTS-STARTPTS+{start:.3f}/TB[bpre{i}]"
            )
        steps.append(prep)

        y_frac = _BROLL_Y_SLOTS[i % len(_BROLL_Y_SLOTS)]
        nxt = out_label if i == n - 1 else f"bro{i}"
        steps.append(
            f"[{current}][bpre{i}]overlay=x='(W-w)/2':y='H*{y_frac:g}':"
            f"enable='between(t,{start:.3f},{disp_end:.3f})'[{nxt}]"
        )
        current = nxt
        notes.append(f"broll:{cue.keyword}")

    return input_args, ";".join(steps), notes


# --------------------------------------------------------------------------- #
# Engine orchestration
# --------------------------------------------------------------------------- #
class Broll_Engine:
    """Plans and resolves b-roll cues for a clip given user options + providers.

    Dependency-injected ``local`` / ``external`` providers keep the engine
    testable without any filesystem or network access (Req 21.1).
    """

    def __init__(
        self,
        options,
        *,
        local: Optional[AssetProvider] = None,
        external: Optional[ExternalProvider] = None,
    ):
        self.options = options
        self.local = local
        self.external = external

    def _effective_mode(self) -> str:
        """Resolve the effective asset-sourcing mode for this run.

        Applies ``effective_options`` semantics without depending on the global
        settings key: b-roll disabled or ``asset_sourcing_mode == "off"`` yields
        ``"off"``; ``permissibility_mode`` forces ``local_only`` (Reqs 8.6,
        19.1); and ``local_then_external`` downgrades to ``local_only`` when the
        injected external provider is missing or has no key (Req 8.4).
        """
        o = self.options
        if not getattr(o, "broll", False):
            return "off"
        if getattr(o, "permissibility_mode", False):
            return "local_only"
        mode = getattr(o, "asset_sourcing_mode", "off")
        if mode == "off":
            return "off"
        if mode == "local_then_external":
            if self.external is None or not getattr(self.external, "has_key", False):
                return "local_only"
        return mode

    def plan(self, words, duration) -> list[BrollCue]:
        """Plan cues, honouring the effective sourcing mode + intensity.

        Returns ``[]`` when b-roll is disabled or sourcing is ``off``
        (Reqs 8.5, 7.5); otherwise delegates to :func:`plan_broll_cues` using
        ``options.broll_intensity``.
        """
        if self._effective_mode() == "off":
            return []
        intensity = getattr(self.options, "broll_intensity", "off")
        return plan_broll_cues(words, duration, intensity=intensity)

    def resolve(self, cues: list[BrollCue]) -> list[BrollCue]:
        """Attach assets to cues, dropping any that are unusable.

        A cue is dropped when no asset is found (Req 9.1), the download/decode
        fails (Req 9.2 — treated as ``None``), or the asset's license is unknown
        (Req 20.3). Returns only the resolvable cues (may be empty).
        """
        mode = self._effective_mode()
        if mode == "off":
            return []
        resolved: list[BrollCue] = []
        for cue in cues:
            try:
                asset = resolve_asset(cue.keyword, mode, self.local, self.external)
            except Exception:
                asset = None
            if asset is None or not _has_known_license(asset):
                continue
            resolved.append(replace(cue, asset=asset))
        return resolved
