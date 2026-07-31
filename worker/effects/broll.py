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

import json
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, cast

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


# --------------------------------------------------------------------------- #
# Tag matching for the local library (A19)
# --------------------------------------------------------------------------- #
#
# Local assets were matched by a *substring* test against the filename: the first file whose
# normalised stem contained a keyword token, or was contained by one, won. That has three
# separate failures, and each of them is silent.
#
# 1. **Short stems match almost everything.** ``stem in token`` with a two-character stem is
#    nearly always true - a file called ``on.mp4`` matches the keyword "money", and ``ca.mp4``
#    matches "car". Tokens were length-filtered; stems were not.
# 2. **First match, not best match.** Directory order decided which of five plausible files was
#    used, so renaming an unrelated file changed the b-roll.
# 3. **A filename is not a description.** A stock clip is called ``pexels-4276282.mp4``, and a
#    library the operator curated by hand still cannot say that ``sunrise-timelapse.mp4`` is a
#    reasonable answer for "morning".
#
# So: an optional ``tags.json`` in the library root describes each file, matching *scores* rather
# than short-circuits, and synonyms are expanded.
#
# **The synonym source is the emoji keyword map.** ``KEYWORD_EMOJI`` clusters ~1190 words into
# ~326 groups, and inverting it yields a curated, already-tested synonym table for free - rather
# than a second hand-written word list to keep in step with the first.
#
# What that table actually asserts is narrower than synonymy, and the difference matters: two words
# share a group only when they share a *picture*. So "money"/"wealth"/"fortune"/"funds" are one
# group, and "cash" is in a different one, because 💰 and 💵 are different images. Expansion is
# therefore **conservative** - it adds true synonyms and misses some near-synonyms. That is the
# right error direction here: a missed synonym costs one weaker match, a wrong one puts unrelated
# footage on screen. Synonyms also only ever *expand* the candidate set and score below an explicit
# tag, so they can never override something the operator actually said.

#: Filename of the optional tag manifest in the b-roll library root.
TAG_MANIFEST_NAME = "tags.json"

#: Minimum token length considered for matching, on both sides now.
_MIN_MATCH_TOKEN = 3

#: Score for an exact tag hit, a synonym hit, and a filename-token hit.
#:
#: Ordered by how much the operator *said*. A tag is a deliberate description; a synonym is this
#: code inferring; a filename token is a coincidence that is often right. Keeping filenames as the
#: weakest signal rather than removing them means a library with no manifest still works.
_SCORE_TAG = 1.0
_SCORE_SYNONYM = 0.6
_SCORE_FILENAME = 0.3


@lru_cache(maxsize=1)
def _synonym_groups() -> dict[str, frozenset[str]]:
    """``word -> the words that mean the same thing``, inverted from the emoji keyword map."""
    try:
        from worker.effects.emoji import KEYWORD_EMOJI
    except Exception:      # pragma: no cover - emoji module is a hard sibling, but stay total
        return {}
    clusters: dict[str, set[str]] = {}
    for word, glyph in KEYWORD_EMOJI.items():
        clusters.setdefault(glyph, set()).add(word)
    groups: dict[str, frozenset[str]] = {}
    for members in clusters.values():
        if len(members) < 2:
            continue
        frozen = frozenset(members)
        for word in members:
            groups[word] = frozen
    return groups


def synonyms(token: str) -> frozenset[str]:
    """Words that mean the same as ``token``, excluding itself. Empty when unknown."""
    group = _synonym_groups().get((token or "").strip().lower())
    if not group:
        return frozenset()
    return frozenset(group - {token})


def load_tag_manifest(root: "str | Path") -> dict[str, frozenset[str]]:
    """``filename -> tags`` from ``root/tags.json``, or ``{}``.

    Accepts either ``{"file.mp4": ["money", "cash"]}`` or a space/comma-separated string per file,
    because both are things a person writes by hand and rejecting one of them would only produce
    a library that silently has no tags.

    Never raises: a malformed manifest degrades to filename matching, which is what the library
    did before A19.
    """
    path = Path(root) / TAG_MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # A nested {"assets": {...}} shape is also accepted, matching assets/fonts.json' style.
    if "assets" in data and isinstance(data["assets"], dict):
        data = data["assets"]

    manifest: dict[str, frozenset[str]] = {}
    for name, value in data.items():
        if isinstance(value, str):
            raw = _norm_tokens(value)
        elif isinstance(value, (list, tuple)):
            raw = [tok for item in value for tok in _norm_tokens(str(item))]
        else:
            continue
        tags = frozenset(tok for tok in raw if len(tok) >= _MIN_MATCH_TOKEN)
        if tags:
            manifest[str(name)] = tags
    return manifest


def match_score(keyword: str, tags: "frozenset[str] | set[str]", filename_stem: str = "") -> float:
    """How well ``tags`` (and, weakly, ``filename_stem``) answer ``keyword`` (A19).

    Returns ``0.0`` for no match. Sums over the keyword's tokens rather than taking the best one,
    so a two-word keyword matched on both tokens beats one matched on either - which is the whole
    reason a keyword has more than one word.
    """
    tokens = [tok for tok in _norm_tokens(keyword) if len(tok) >= _MIN_MATCH_TOKEN]
    if not tokens:
        return 0.0
    tag_set = {str(t).lower() for t in tags}
    stem_tokens = {tok for tok in _norm_tokens(filename_stem) if len(tok) >= _MIN_MATCH_TOKEN}

    total = 0.0
    for token in tokens:
        if token in tag_set:
            total += _SCORE_TAG
            continue
        overlap = synonyms(token) & tag_set
        if overlap:
            total += _SCORE_SYNONYM
            continue
        # Both sides length-filtered, so a two-character stem can no longer match everything.
        if token in stem_tokens:
            total += _SCORE_FILENAME
    return total


class LocalProvider:
    """Resolves assets from an operator-supplied library folder (no network).

    Assets are treated as operator-supplied and therefore usable: the returned
    :class:`AssetRef` records ``provider="local"``, ``license="local"`` and the
    source ``path`` (Req 12.2). Images (``.png/.jpg/.jpeg/.webp``) become kind
    ``"image"``; videos (``.mp4/.mov/.webm``) become kind ``"video"``.

    Matching is by tag, then synonym, then filename token (A19) - see :func:`match_score`. The
    *best* scoring file wins rather than the first one found, with ties broken by name, so the
    result does not depend on directory order.
    """

    name = "local"

    def __init__(self, root: "str | Path | None" = None):
        self.root = Path(root) if root is not None else Path(settings.broll_dir)

    def search(self, keyword: str) -> Optional[AssetRef]:
        tokens = [t for t in _norm_tokens(keyword) if len(t) >= _MIN_MATCH_TOKEN]
        if not tokens:
            return None
        try:
            entries = sorted(p for p in self.root.iterdir() if p.is_file())
        except Exception:
            # Missing directory / unreadable => no local match (no network).
            return None

        manifest = load_tag_manifest(self.root)
        best: Optional[tuple[float, str, Path, str]] = None
        for path in entries:
            ext = path.suffix.lower()
            if ext in _IMAGE_EXTS:
                kind = "image"
            elif ext in _VIDEO_EXTS:
                kind = "video"
            else:
                continue
            score = match_score(keyword, manifest.get(path.name, frozenset()), path.stem)
            if score <= 0.0:
                continue
            # Name is the tie-break, so two equally-tagged files resolve the same way on every
            # machine regardless of how the filesystem happens to enumerate them.
            candidate = (score, path.name, path, kind)
            if best is None or (candidate[0], ) > (best[0], ) or (
                candidate[0] == best[0] and candidate[1] < best[1]
            ):
                best = candidate

        if best is None:
            return None
        return AssetRef(
            path=str(best[2]),
            kind=best[3],
            provider="local",
            source_id="",
            license="local",
            attribution="",
        )


def _default_external_downloader(
    keyword: str, api_key: str, base_url: str, cache_dir: Path
) -> Optional[AssetRef]:
    """Safe, offline default downloader.

    The concrete provider-specific HTTP/download logic is intentionally not
    implemented here so the module never performs network I/O by default; a
    real adapter is injected via ``downloader=`` (Req 21.1). Returns ``None``.
    """
    return None


# --------------------------------------------------------------------------- #
# A20 - cache downloaded assets with their licence metadata
# --------------------------------------------------------------------------- #
#: Filename of the sidecar written beside a cached asset.
LICENSE_SIDECAR_SUFFIX = ".license.json"


def license_sidecar_path(asset_path: "str | Path") -> Path:
    """Where the licence record for ``asset_path`` lives."""
    path = Path(asset_path)
    return path.with_name(path.name + LICENSE_SIDECAR_SUFFIX)


def record_asset_license(asset: AssetRef, keyword: str = "") -> Optional[Path]:
    """Write ``asset``'s provenance beside the file, returning the sidecar path (A20).

    **Why a sidecar and not just a cache.** A downloaded asset with an empty ``license`` is
    dropped by :func:`resolve_asset`, so an asset whose licence is lost is not merely
    undocumented - it becomes *unusable*. Cached files carry no metadata of their own, so a
    second run against the cache would re-derive nothing and the asset would be discarded
    despite already having been fetched and approved.

    It is also the only durable record that a clip's b-roll was licensed. A creator asked to
    prove it after the fact has the file and nothing else; the attribution a provider requires
    lives only in the API response that is long gone.

    Never raises: caching is an optimisation, and a read-only cache directory must cost the
    cache and not the clip.
    """
    try:
        path = Path(asset.path)
        if not path.name:
            return None
        sidecar = license_sidecar_path(path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "provider": asset.provider or "",
            "source_id": asset.source_id or "",
            "license": asset.license or "",
            "attribution": asset.attribution or "",
            # AssetRef carries no keyword of its own, so the search term is passed in. It is
            # what the cache is looked up by, and a provider's own filename says nothing about
            # what was searched for.
            "keyword": (keyword or "").strip(),
            "kind": asset.kind or "video",
            "asset": path.name,
        }
        sidecar.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return sidecar
    except Exception:
        return None


def load_asset_license(asset_path: "str | Path") -> Optional[dict]:
    """Read a cached asset's licence record, or ``None`` (A20).

    A record missing the one field that matters - ``license`` - is treated as no record at all,
    so a truncated or hand-edited sidecar cannot resurrect an asset that would otherwise be
    dropped for having an unknown licence. That failure would be silent and would put
    unlicensed footage in someone's published clip.
    """
    try:
        data = json.loads(license_sidecar_path(asset_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not str(data.get("license") or "").strip():
        return None
    return data


def cached_asset(cache_dir: "str | Path", keyword: str) -> Optional[AssetRef]:
    """An already-downloaded asset for ``keyword``, if one is cached with its licence (A20).

    Matched on the sidecar's recorded ``keyword`` rather than on the filename, because a
    provider's filename is its own identifier and says nothing about what was searched for.
    """
    if not keyword:
        return None
    try:
        root = Path(cache_dir)
        if not root.is_dir():
            return None
        wanted = keyword.strip().lower()
        for sidecar in sorted(root.glob(f"*{LICENSE_SIDECAR_SUFFIX}")):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if str(data.get("keyword") or "").strip().lower() != wanted:
                continue
            if not str(data.get("license") or "").strip():
                continue
            asset_path = root / str(data.get("asset") or "")
            if not asset_path.is_file():
                # The sidecar outlived its asset - a retention sweep, a manual delete. Ignore
                # it rather than returning a path that does not exist.
                continue
            return AssetRef(
                path=str(asset_path),
                kind=str(data.get("kind") or "video"),
                provider=str(data.get("provider") or ""),
                source_id=str(data.get("source_id") or ""),
                license=str(data.get("license") or ""),
                attribution=str(data.get("attribution") or ""),
            )
    except Exception:
        return None
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

        # A20: serve an already-downloaded asset before spending a request. Checked first
        # because the alternative is re-downloading the same file on every clip of every job -
        # bandwidth, a provider rate limit, and on a metered API an actual bill, for a file
        # already on disk.
        cached = cached_asset(self.cache_dir, keyword)
        if cached is not None:
            return cached

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
        # A20: persist the licence beside the file. Without this the cached asset has no
        # provenance, and resolve_asset drops an asset with an unknown licence - so a cache hit
        # on the next run would discard a file that was already fetched and approved.
        record_asset_license(result, keyword)
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
    # Precondition, not an assumption the checker can see: callers pass cues from the
    # `resolved` list built in `broll_filtergraph`, or from `Broll_Engine.resolve`.
    asset = cast(AssetRef, cue.asset)
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

#: Aspect ratio a still is fitted into when Ken Burns is on (A22).
#:
#: A fixed box is required, not a preference: ``zoompan`` needs an explicit output size, and the
#: default graph scales stills with ``-1`` height, so the height is not known where the filter
#: string is built. Probing every asset for its dimensions would put an ``ffprobe`` per asset
#: inside a documented pure string builder. 16:9 cover-cropping is what a half-frame overlay
#: rectangle looks like anyway.
_KEN_BURNS_ASPECT = 16 / 9

#: Where the zoom converges, rotated per cue.
#:
#: With a *fixed* anchor and a rising zoom, the visible region drifts towards that anchor - which
#: is the pan half of Ken Burns, for free. Rotating the anchor is what stops four stills in one
#: clip all drifting the same way, which would read as a template.
_KEN_BURNS_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.5, 0.5), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0),
)


def build_broll_overlay(
    cues: list[BrollCue],
    base_label: str,
    out_label: str,
    *,
    width: int,
    height: int,
    fps: float,
    input_offset: int,
    ken_burns: bool = False,
    zoom: float = 0.0,
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
        ken_burns: apply A22's slow zoom-and-drift to *still* assets. Off by default, so the
            shipped graph and the v0.8.0 parity goldens are unchanged. Video assets already
            move and are never affected.
        zoom: how far a still zooms over its window, as a fraction (``0.12`` = 12%). Zero
            disables the motion even when ``ken_burns`` is set, so one setting can turn it off
            without the caller having to know two.

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
    # Even, because libx264's 4:2:0 chroma subsampling requires it and `crop` will not round for
    # you - an odd height fails the encode rather than the filter, several stages later.
    overlay_h = max(2, int(round(overlay_w / _KEN_BURNS_ASPECT)))
    overlay_h -= overlay_h % 2
    overlay_w -= overlay_w % 2
    zoom = max(0.0, float(zoom))

    input_args: list[str] = []
    steps: list[str] = []
    notes: list[str] = []
    current = base_label
    n = len(resolved)
    for i, cue in enumerate(resolved):
        # `resolved` was filtered on `asset is not None` above; cast keeps that invariant
        # documented rather than adding a branch that can never be taken.
        asset = cast(AssetRef, cue.asset)
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
        elif ken_burns and zoom > 0.0:
            # A22: a still that sits motionless for three seconds over moving footage is the
            # clearest sign a clip was assembled rather than edited. `zoompan` supplies both the
            # zoom and - via a fixed off-centre anchor - the drift.
            input_args += ["-loop", "1", "-t", f"{disp_dur:.3f}", "-i", asset.path]
            frames = max(1, int(round(disp_dur * fps)))
            anchor_x, anchor_y = _KEN_BURNS_ANCHORS[i % len(_KEN_BURNS_ANCHORS)]
            prep = (
                # Cover-crop into the fixed box zoompan needs an explicit size for.
                f"[{idx}:v]scale={overlay_w}:{overlay_h}"
                f":force_original_aspect_ratio=increase,"
                f"crop={overlay_w}:{overlay_h},format=rgba,"
                # Zoom as an explicit function of the output frame number, not the accumulating
                # `z='zoom+step'` recipe: accumulation makes the final framing depend on how many
                # frames were produced, so the same still zooms further on a 60fps render than on
                # a 30fps one. `on/frames` is the same motion at any frame rate.
                f"zoompan=z='1+{zoom:.4f}*on/{frames}'"
                f":x='(iw-iw/zoom)*{anchor_x:g}':y='(ih-ih/zoom)*{anchor_y:g}'"
                f":d=1:s={overlay_w}x{overlay_h}:fps={fps:g},"
                # zoompan outputs yuv/rgb; the overlay below needs alpha back. Verified against
                # a half-transparent PNG: transparency survives the round trip.
                f"format=rgba,"
                f"setpts=PTS-STARTPTS+{start:.3f}/TB[bpre{i}]"
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
