"""Optional hardware H.264 encoding, with an honest availability probe (O8).

Every encode goes through ``libx264``. On a machine with a GPU that is several times slower than
it needs to be, and on a long source the encode is the largest single cost in the pipeline - so
"use the hardware if there is any" is a real win. It is also the kind of change that goes wrong
silently, in three specific ways this module exists to prevent.

**1. A listed encoder is not a usable encoder.** ``ffmpeg -encoders`` reports what was *compiled
in*. The ffmpeg this project develops against lists ``h264_v4l2m2m`` and fails at runtime the
moment you ask it to encode a frame, because there is no V4L2 device behind it; ``h264_nvenc``
behaves the same way on a host with the libraries but no NVIDIA card. So availability is decided
by **actually encoding a frame** and caching the answer, not by reading a list. Anything else
turns a missing GPU into a failed job at the point where the transcription has already been paid
for.

**2. The quality flag is not ``-crf`` anywhere else.** Each encoder expresses "constant quality"
differently, and three of them use a *different scale*:

===================== ====================== ===============================================
encoder               quality flag           scale
===================== ====================== ===============================================
``libx264``           ``-crf N``             0-51, **lower is better**
``h264_nvenc``        ``-rc vbr -cq N``      0-51, lower is better (comparable to crf)
``h264_qsv``          ``-global_quality N``  ~1-51, lower is better
``h264_vaapi``        ``-qp N``              0-51, lower is better
``h264_videotoolbox`` ``-q:v N``             **1-100, higher is better** - inverted
===================== ====================== ===============================================

Passing ``-crf 20`` to VideoToolbox does not error - ``-crf`` is simply ignored, and the encoder
falls back to its own default bitrate. Passing ``20`` to ``-q:v`` asks for near-worst quality.
Either way the output is wrong and nothing says so, which is why the mapping is a table with the
scale written down rather than a string substitution.

**3. Presets do not share a vocabulary.** ``-preset veryfast`` is meaningless to NVENC, which uses
``p1``..``p7``, and VideoToolbox has no preset at all. An unrecognised preset is a hard error on
some builds and ignored on others.

**Deliberately not offered: ``h264_v4l2m2m``.** It has no constant-quality mode - only ``-b:v`` -
so presenting it as a drop-in for ``-crf`` would mean quietly switching the whole pipeline from a
quality target to a bitrate target. It stays in :data:`KNOWN_ENCODERS` as *unsupported* rather
than being silently absent, because "why is my Raspberry Pi encoder not used" deserves an answer.

**The default is ``libx264``, not ``auto``.** Hardware encoders are not bit-comparable with x264
at the same nominal quality, and several are visibly softer at the same number. Defaulting to
``auto`` would change the output of every existing install the first time it landed on a machine
with a GPU, with no setting changed and no way to tell from the clip record - the same reasoning
every other new visual default in this project follows, and the reason the M1 golden renders can
detect an accidental change at all.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger(__name__)

#: The software encoder, and the fallback for everything.
SOFTWARE_ENCODER = "libx264"

#: How long a probe encode may take before it is treated as unavailable.
#:
#: Generous: a cold GPU driver initialising for the first time is slow, and a probe that times out
#: on a working encoder would disable hardware for the whole process.
PROBE_TIMEOUT_S = 20.0

#: x264 preset -> NVENC preset. NVENC's p1..p7 run fastest to slowest, the opposite direction to
#: reading x264's names left to right, so this is written out rather than computed.
_NVENC_PRESETS: dict[str, str] = {
    "ultrafast": "p1",
    "superfast": "p1",
    "veryfast": "p2",
    "faster": "p3",
    "fast": "p4",
    "medium": "p4",
    "slow": "p5",
    "slower": "p6",
    "veryslow": "p7",
    "placebo": "p7",
}

#: x264 preset -> QSV preset. QSV accepts the x264 names, but only seven of the ten.
_QSV_PRESETS: dict[str, str] = {
    "ultrafast": "veryfast",
    "superfast": "veryfast",
    "veryfast": "veryfast",
    "faster": "faster",
    "fast": "fast",
    "medium": "medium",
    "slow": "slow",
    "slower": "slower",
    "veryslow": "veryslow",
    "placebo": "veryslow",
}


def _clamp_crf(crf: int) -> int:
    return max(0, min(51, int(crf)))


@dataclass(frozen=True)
class VideoEncoder:
    """One H.264 encoder and how to ask it for a quality target."""

    name: str
    #: Human label for markers and the API.
    kind: str
    #: ``True`` when this encoder can be selected. See the module note on ``h264_v4l2m2m``.
    supported: bool = True
    #: Why not, when ``supported`` is False.
    unsupported_reason: str = ""
    #: A pixel format this encoder requires instead of the project default.
    pix_fmt: str = ""
    #: Whether ``-profile:v``/``-level`` may be passed. VAAPI rejects ``-level`` on some drivers.
    accepts_level: bool = True
    _preset_map: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def hardware(self) -> bool:
        return self.kind != "software"

    def quality_args(self, crf: int) -> list[str]:
        """The flags that express ``crf`` (an x264 CRF value) for this encoder."""
        crf = _clamp_crf(crf)
        if self.kind == "software":
            return ["-crf", str(crf)]
        if self.kind == "nvenc":
            # `-rc vbr` is required: without it `-cq` is accepted and ignored, and the encoder
            # uses its default bitrate instead of a quality target.
            return ["-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
        if self.kind == "qsv":
            return ["-global_quality", str(crf)]
        if self.kind == "vaapi":
            return ["-qp", str(crf)]
        if self.kind == "videotoolbox":
            # Inverted and differently ranged: 1-100, higher is better. Mapped linearly from the
            # CRF scale so a configured crf of 20 asks for roughly the same *intent* rather than
            # for near-worst quality, which is what passing 20 straight through would mean.
            quality = int(round((51 - crf) / 51 * 100))
            return ["-q:v", str(max(1, min(100, quality)))]
        return ["-crf", str(crf)]

    def preset_args(self, x264_preset: str) -> list[str]:
        """The flags that express ``x264_preset`` for this encoder, or ``[]`` if it has none."""
        if self.kind == "videotoolbox":
            # No preset concept at all; passing one is an "Unrecognized option" error.
            return []
        mapped = self._preset_map.get(str(x264_preset).strip().lower())
        if self.kind == "software":
            return ["-preset", str(x264_preset)]
        if not mapped:
            return []
        return ["-preset", mapped]


KNOWN_ENCODERS: dict[str, VideoEncoder] = {
    SOFTWARE_ENCODER: VideoEncoder(name=SOFTWARE_ENCODER, kind="software"),
    "h264_nvenc": VideoEncoder(
        name="h264_nvenc",
        kind="nvenc",
        _preset_map=_NVENC_PRESETS,
    ),
    "h264_qsv": VideoEncoder(
        name="h264_qsv",
        kind="qsv",
        _preset_map=_QSV_PRESETS,
    ),
    "h264_videotoolbox": VideoEncoder(
        name="h264_videotoolbox",
        kind="videotoolbox",
    ),
    "h264_vaapi": VideoEncoder(
        name="h264_vaapi",
        kind="vaapi",
        # VAAPI needs frames uploaded to the device and `-level` is rejected by several drivers.
        # Left selectable, but it is the one that most often needs `-vaapi_device` on the command
        # line, which is outside what this project builds.
        pix_fmt="nv12",
        accepts_level=False,
    ),
    "h264_v4l2m2m": VideoEncoder(
        name="h264_v4l2m2m",
        kind="v4l2",
        supported=False,
        unsupported_reason=(
            "no constant-quality mode - only -b:v - so using it would silently switch the "
            "pipeline from a quality target to a bitrate target"
        ),
    ),
}

#: The order ``auto`` tries hardware encoders in.
#:
#: NVENC first because it is the most common and the best characterised; VideoToolbox before QSV
#: because on the machine that has it (an Apple host) it is the only one present; VAAPI last
#: because it is the one most likely to need a device argument this project does not supply.
AUTO_ORDER: tuple[str, ...] = ("h264_nvenc", "h264_videotoolbox", "h264_qsv", "h264_vaapi")

#: Cache of probe results, keyed by encoder name. Populated by :func:`encoder_available`.
_PROBE_CACHE: dict[str, bool] = {}


def reset_probe_cache() -> None:
    """Forget cached probe results. For tests, and for a settings change at runtime."""
    _PROBE_CACHE.clear()


def compiled_encoders() -> frozenset[str]:
    """Encoder names this ffmpeg build lists.

    A cheap pre-filter only: see the module docstring on why this is not the availability test.
    Returning an empty set on failure makes every probe fall through to the real encode, which is
    the safe direction - it costs a frame, not a wrong answer.
    """
    try:
        proc = subprocess.run(
            [settings.ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        # Lines look like " V....D libx264   libx264 H.264 ...". The flags column is six chars.
        if len(parts) >= 2 and len(parts[0]) == 6:
            names.add(parts[1])
    return frozenset(names)


def encoder_available(name: str) -> bool:
    """Whether ``name`` can actually encode a frame on this machine (cached).

    The probe is a one-frame encode to ``null``. That is the only check that distinguishes
    "compiled in" from "usable" - and the distinction is not hypothetical: the ffmpeg this project
    develops against lists ``h264_v4l2m2m`` and fails on the first frame.
    """
    key = str(name)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]

    encoder = KNOWN_ENCODERS.get(key)
    if encoder is not None and not encoder.supported:
        _PROBE_CACHE[key] = False
        return False
    if key not in compiled_encoders():
        _PROBE_CACHE[key] = False
        return False

    args = [
        settings.ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25:duration=0.04",
        "-frames:v",
        "1",
        "-c:v",
        key,
    ]
    if encoder is not None and encoder.pix_fmt:
        args += ["-pix_fmt", encoder.pix_fmt]
    args += ["-f", "null", "-"]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False
        )
        ok = proc.returncode == 0
        if not ok:
            logger.debug(
                "O8: %s is compiled in but failed to encode: %s",
                key,
                (proc.stderr or "").strip()[:300],
            )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("O8: probing %s failed: %s", key, exc)
        ok = False
    _PROBE_CACHE[key] = ok
    return ok


@dataclass(frozen=True)
class EncoderChoice:
    """Which encoder will be used, and why it is not the one that was asked for."""

    encoder: VideoEncoder
    requested: str
    #: A marker for the clip record when the request could not be honoured, else ``""``.
    marker: str = ""

    @property
    def degraded(self) -> bool:
        return bool(self.marker)


def resolve_encoder(requested: str | None = None) -> EncoderChoice:
    """The encoder to use for ``requested`` (default: :data:`config.settings.video_encoder`).

    ``auto`` tries the hardware encoders in :data:`AUTO_ORDER` and falls back to ``libx264``. A
    *named* encoder that is unavailable also falls back, but records a marker: silently ignoring an
    explicit request is how someone spends a week believing their GPU is being used.

    An unknown name falls back too rather than raising - a typo in a setting should not fail a job
    after the transcription has been paid for.
    """
    name = str(
        requested
        if requested is not None
        else getattr(settings, "video_encoder", SOFTWARE_ENCODER) or SOFTWARE_ENCODER
    ).strip()
    software = KNOWN_ENCODERS[SOFTWARE_ENCODER]

    if name.lower() == "auto":
        for candidate in AUTO_ORDER:
            if encoder_available(candidate):
                return EncoderChoice(KNOWN_ENCODERS[candidate], requested="auto")
        # No marker: `auto` asked for "the best available", and software *is* available. Reporting
        # a degradation for the ordinary case would make the marker meaningless.
        return EncoderChoice(software, requested="auto")

    encoder = KNOWN_ENCODERS.get(name)
    if encoder is None:
        return EncoderChoice(
            software,
            requested=name,
            marker=f"encoder_unknown:{name}",
        )
    if not encoder.supported:
        return EncoderChoice(
            software,
            requested=name,
            marker=f"encoder_unsupported:{name}",
        )
    if encoder.name == SOFTWARE_ENCODER:
        return EncoderChoice(software, requested=name)
    if not encoder_available(encoder.name):
        return EncoderChoice(
            software,
            requested=name,
            marker=f"encoder_unavailable:{name}",
        )
    return EncoderChoice(encoder, requested=name)
