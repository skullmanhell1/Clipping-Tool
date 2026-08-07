"""Colour decisions for delivery: HDR detection, tone-mapping, range, and output tags.

Implements **O13** (HDR -> SDR tone-mapping), **O14** (colour metadata on delivered files) and
**O15** (colour range resolution). These three are grouped because they are one decision made
once: *what colour is the thing we are about to deliver, and does the file say so?*

They are also the only items in ``clip-signal-fidelity`` that fix output which is **currently
wrong** rather than merely improvable, which is why the spec has them jump the queue ahead of the
measurement gate. An HDR source rendered through a pipeline with no tone-map does not look
slightly worse -- PQ-coded values interpreted as Rec.709 come out grey, flat and desaturated, and
the brighter the original the worse it reads.

Design notes that are load-bearing:

**Classification is conservative and tri-state.** ``HDR``, ``SDR`` and ``UNKNOWN`` are three
different answers and the third is not a synonym for the second. The failure modes are
asymmetric: tone-mapping a mislabelled SDR source visibly destroys it, while failing to
tone-map an HDR one leaves it as it is today. So anything we cannot positively identify is left
alone. This is the same refusal ``worker/language.py`` makes for Han script and
``worker/script_support.py`` makes for an unrenderable caption -- report that the answer is
unknown rather than substitute a plausible one.

**HDR is read only from the transfer function.** Never from bit depth, never from resolution.
10-bit Rec.709 is ordinary, 4K SDR is the norm, and either inference would misfire on a large
class of perfectly normal footage. PQ (``smpte2084``) and HLG (``arib-std-b67``) are the two
signals that mean HDR, and an unrecognised transfer is ``UNKNOWN`` rather than a guess.

**Tags describe what was delivered, not what arrived.** After a tone-map the file is Rec.709 and
must say so. Copying the source's ``smpte2084`` onto tone-mapped output is *worse* than writing
no tags at all, because a player that reads it will confidently apply an HDR EOTF to SDR
content. Equally, this module does not claim ``bt709`` for a Rec.601 source it passed through
untouched -- if we did not convert it, we tag what it is.

**Range conversion does not need ``zscale``.** ``scale=in_range=..:out_range=..`` is in every
ffmpeg build, so full-range footage is correctly squeezed to limited even on a build with no
``libzimg``. Only the tone-map itself depends on optional filters. That split is deliberate: it
means the most common defect (full-range phone footage crushing its blacks) is fixed
unconditionally, while the rarer one degrades with a marker.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-checking only. The runtime import stays inside `tonemap_filters_missing`, because
    # `capabilities` shells out to `ffmpeg -filters` on first use and this module is imported by
    # the pipeline at module scope. `worker/ffmpeg_utils.py` guards its `VideoEncoder` import the
    # same way and for the same reason.
    from worker.engines.capabilities import Capability_Status

#: A capability prober: takes a capability id, returns its status. Matches
#: ``worker.engines.capabilities.Prober``, restated here rather than imported so this module has
#: no runtime dependency on that one.
Prober = Callable[[str], "Capability_Status"]

#: PQ. The transfer function used by HDR10, HDR10+ and Dolby Vision's base layer.
HDR_TRANSFER_PQ = "smpte2084"
#: HLG. What phones and broadcast use; ``arib-std-b67`` is ffprobe's spelling.
HDR_TRANSFER_HLG = "arib-std-b67"

#: The *only* two values that make a source HDR (R2.1, R1.6).
HDR_TRANSFERS: frozenset[str] = frozenset({HDR_TRANSFER_PQ, HDR_TRANSFER_HLG})

#: Transfer functions we positively recognise as SDR.
#:
#: This set exists so that "SDR" and "unknown" stay distinguishable (R1.7). Without it every
#: non-HDR value would collapse into one bucket and the marker could not tell an operator whether
#: the source said Rec.709 or said nothing at all -- which is exactly the question you ask when a
#: clip comes back looking wrong.
#:
#: ``bt2020-10``/``bt2020-12`` are here on purpose: they are *wide gamut*, not high dynamic
#: range. Wide-gamut SDR is a real and increasingly common combination, and tone-mapping it
#: would be the mislabelled-SDR failure this module is built to avoid.
SDR_TRANSFERS: frozenset[str] = frozenset(
    {
        "bt709",
        "smpte170m",
        "smpte240m",
        "bt470m",
        "bt470bg",
        "gamma22",
        "gamma28",
        "linear",
        "log100",
        "log316",
        "iec61966-2-1",
        "iec61966-2-4",
        "bt1361e",
        "bt2020-10",
        "bt2020-12",
    }
)

#: ffprobe's spellings for a full-range (``pc``/JPEG) and limited-range (``tv``/MPEG) stream.
RANGE_FULL = "pc"
RANGE_LIMITED = "tv"

#: What we deliver when the source does not say (R3.7).
#:
#: Limited, because it is what H.264 in a broadcast/streaming context means by default and what
#: every platform expects. The choice is recorded on the clip rather than left implicit, because
#: "we assumed limited" is precisely the fact an operator needs when the blacks look wrong.
DEFAULT_DELIVERY_RANGE = RANGE_LIMITED

#: Rec.709. The delivery target for anything tone-mapped (R2.1).
BT709 = "bt709"

#: Filters the tone-map chain needs, in the order a reader would look for them.
#:
#: ``zscale`` requires ``libzimg`` at ffmpeg build time and is genuinely absent from some builds
#: -- the Dockerfile deliberately does not pin an ffmpeg, so this cannot be assumed. ``tonemap``
#: is the CPU tone-mapping filter and ships with ``--enable-gpl`` builds.
TONEMAP_REQUIRED_FILTERS: tuple[str, ...] = ("zscale", "tonemap")

#: Tone-mapping operators ``tonemap`` accepts.
#:
#: ``hable`` is the default: it preserves shadow and highlight detail at the cost of a little
#: contrast, which is the right trade for footage a viewer has never seen the original of.
#: ``reinhard`` flattens highlights more; ``mobius`` sits between the two; ``clip`` is included
#: because it is occasionally what someone wants and excluding it would just mean they patch the
#: module. The right operator is content-dependent and genuinely contested, which is why this is
#: a setting rather than a constant (R2.10).
TONEMAP_OPERATORS: tuple[str, ...] = ("hable", "mobius", "reinhard", "clip", "linear")

#: Default operator and target peak luminance in nits (R2.10).
DEFAULT_TONEMAP_OPERATOR = "hable"
DEFAULT_TONEMAP_TARGET_NITS = 100

#: Working pixel format for the tone-map.
#:
#: ``tonemap`` operates on linear light and requires floating-point input; without this the
#: filter graph fails to configure rather than producing a wrong-looking result, so it is not
#: optional. ``gbrpf32le`` is the format the filter documents.
TONEMAP_WORKING_FORMAT = "gbrpf32le"


class Dynamic_Range(str, Enum):
    """What we were able to determine about a source's dynamic range.

    Three states rather than a boolean, because ``UNKNOWN`` drives different behaviour from
    ``SDR``: both decline the tone-map, but only one of them is a positive finding, and only
    one of them should make an operator go and look at the source.
    """

    HDR = "hdr"
    SDR = "sdr"
    UNKNOWN = "unknown"


def classify_transfer(transfer: str) -> Dynamic_Range:
    """Classify a source from its reported transfer function alone (R1.6, R1.7).

    An empty value means ffprobe reported no transfer function, which is ``UNKNOWN`` -- not
    SDR. Untagged content very often *is* Rec.709, but "probably Rec.709" and "reported
    Rec.709" are different facts and only one of them is evidence.
    """
    value = (transfer or "").strip().lower()
    if not value:
        return Dynamic_Range.UNKNOWN
    if value in HDR_TRANSFERS:
        return Dynamic_Range.HDR
    if value in SDR_TRANSFERS:
        return Dynamic_Range.SDR
    return Dynamic_Range.UNKNOWN


def normalise_range(value: str) -> str:
    """Map ffprobe's range spellings onto ``pc``/``tv``, or ``""`` when it said nothing.

    ffprobe reports ``color_range`` as ``pc``/``tv`` on most builds but ``full``/``limited`` on
    some, and ``unknown`` when the stream is silent. Collapsing those here keeps every caller
    from having to know all three vocabularies.
    """
    text = (value or "").strip().lower()
    if text in {RANGE_FULL, "full", "jpeg"}:
        return RANGE_FULL
    if text in {RANGE_LIMITED, "limited", "mpeg"}:
        return RANGE_LIMITED
    return ""


def resolve_operator(requested: str) -> str:
    """Return a supported tone-mapping operator, falling back to the default.

    An unrecognised value applies the documented default rather than raising: a mistyped
    operator should not turn a deliverable clip into a failed job (R2.5, and R12's rule that
    unrecognised option values apply the default without raising).
    """
    value = (requested or "").strip().lower()
    return value if value in TONEMAP_OPERATORS else DEFAULT_TONEMAP_OPERATOR


def tonemap_chain(
    *,
    operator: str = DEFAULT_TONEMAP_OPERATOR,
    target_nits: int = DEFAULT_TONEMAP_TARGET_NITS,
    out_range: str = DEFAULT_DELIVERY_RANGE,
) -> str:
    """The HDR -> SDR Rec.709 filter chain. A pure string builder with no probe in it.

    Kept probe-free for the reason ``background_chain`` documents: the tests assert this
    function's output directly, so a capability lookup hidden inside it would make the chain
    untestable without also faking a prober.

    The chain, and why each link is there:

    1. ``zscale=t=linear:npl=<nits>`` -- decode the PQ/HLG curve to linear light. ``npl``
       (nominal peak luminance) is what tells the tone-mapper how bright the source's white
       actually was; getting it wrong is the difference between a natural result and a washed
       one.
    2. ``format=gbrpf32le`` -- ``tonemap`` requires floating-point linear input and fails to
       configure without it.
    3. ``zscale=p=bt709`` -- convert primaries *before* tone-mapping, so the operator is
       compressing the range of the gamut we are actually delivering rather than of BT.2020.
    4. ``tonemap=tonemap=<op>:desat=0`` -- the range compression itself. ``desat=0`` disables
       the filter's highlight desaturation, which on faces reads as a colour shift rather than
       as a highlight roll-off.
    5. ``zscale=t=bt709:m=bt709:r=<range>`` -- re-encode to the Rec.709 transfer and matrix at
       the delivery range.
    6. ``format=yuv420p`` -- back to the project's output pixel format. This does not contradict
       ``O1``: it is the same value ``OUTPUT_PIX_FMT`` names, restated at the end of a chain that
       necessarily left it.
    """
    op = resolve_operator(operator)
    peak = max(1, int(target_nits))
    rng = normalise_range(out_range) or DEFAULT_DELIVERY_RANGE
    return ",".join(
        (
            f"zscale=t=linear:npl={peak}",
            f"format={TONEMAP_WORKING_FORMAT}",
            f"zscale=p={BT709}",
            f"tonemap=tonemap={op}:desat=0",
            f"zscale=t={BT709}:m={BT709}:r={rng}",
            "format=yuv420p",
        )
    )


def range_convert_chain(*, source_range: str, out_range: str = DEFAULT_DELIVERY_RANGE) -> str:
    """Squeeze full-range video to limited (O15), or return ``""`` when nothing is needed.

    Uses ``scale`` rather than ``zscale`` deliberately. ``scale`` is in every ffmpeg build, so
    the most common colour defect in the wild -- full-range phone footage delivered as though it
    were limited, which crushes blacks and clips highlights -- is fixed unconditionally instead
    of depending on whether the host's ffmpeg happens to carry ``libzimg``.

    No dimensions are given, so this performs a range conversion and no resampling: geometry is
    untouched (R5.4's rule, honoured here even though this is not a scaling change).
    """
    src = normalise_range(source_range)
    dst = normalise_range(out_range) or DEFAULT_DELIVERY_RANGE
    if not src or src == dst:
        return ""
    return f"scale=in_range={src}:out_range={dst}"


@dataclass(frozen=True)
class Colour_Plan:
    """What to do about colour for one clip, decided once and applied once.

    ``filters`` is the video filter chain to insert **before** any scaling or grade (R2.2), and
    it is empty whenever nothing needs doing -- which is the common case, and the reason a
    library of SDR clips renders byte-identically to before this existed.

    ``tags`` is the output argv describing what will actually be delivered (R3.1, R3.2).

    ``markers`` are ``ClipResult.effects_applied`` entries. They name the *resolved* outcome,
    never the request: an operator reading ``tone_map_degraded:zscale`` learns something, where
    a marker echoing back the setting they already know they set learns them nothing.
    """

    source_range: Dynamic_Range = Dynamic_Range.UNKNOWN
    filters: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    tone_mapped: bool = False
    delivered_range: str = DEFAULT_DELIVERY_RANGE
    #: Set once the plan has been consumed by a pass, so no later pass can apply it again
    #: (R2.8). Enforced by :meth:`consumed`, not by a mutable flag on a frozen record.
    _consumed: bool = field(default=False, repr=False)

    @property
    def has_filters(self) -> bool:
        return bool(self.filters)

    @property
    def filter_chain(self) -> str:
        """The filters as one comma-joined ffmpeg chain fragment."""
        return ",".join(self.filters)

    def consumed(self) -> Colour_Plan:
        """The same plan with its filters spent.

        This is how "at most one tone-map per clip" (R2.8) is enforced. The pipeline runs three
        passes; applying the chain at the cut *and* at the composite would compress the range
        twice and produce a muddy, flat picture -- worse than not tone-mapping at all, and
        far harder to diagnose because it still looks like a plausible image.

        The tags are deliberately **kept**. They describe the delivered file, so every pass that
        writes one should carry them; it is only the pixel conversion that must happen once.
        """
        return Colour_Plan(
            source_range=self.source_range,
            filters=(),
            tags=self.tags,
            markers=self.markers,
            tone_mapped=self.tone_mapped,
            delivered_range=self.delivered_range,
            _consumed=True,
        )


def colour_tag_args(
    *, transfer: str, primaries: str, matrix: str, colour_range: str
) -> tuple[str, ...]:
    """Build the output colour-tag argv, omitting anything we cannot honestly state.

    Each flag is emitted only when there is a value for it. A partially-tagged file is better
    than a confidently mis-tagged one: a player faced with a missing tag falls back to its
    Rec.709 assumption, which is right far more often than a wrong explicit value, and unlike a
    wrong value it leaves no trace that we asserted anything.
    """
    args: list[str] = []
    if matrix:
        args += ["-colorspace", matrix]
    if primaries:
        args += ["-color_primaries", primaries]
    if transfer:
        args += ["-color_trc", transfer]
    if colour_range:
        args += ["-color_range", colour_range]
    return tuple(args)


def tonemap_filters_missing(prober: Optional["Prober"] = None) -> str:
    """Return the first tone-map filter this ffmpeg lacks, or ``""`` if all are present.

    Routed through ``worker.engines.capabilities`` rather than probing here, because that
    module exists precisely so there is one filter probe in the project. Its docstring records
    the incident: an earlier hand-rolled probe misparsed ``ffmpeg -filters`` and **hid 124 of
    486 filters**, which silently disabled features on builds that had them.

    Note the deliberate deviation from ``background_style_available``, which answers "available"
    when the probe itself fails. That fail-open default is right there, because the fallback is
    another working background. Here it is wrong: claiming ``zscale`` exists when we do not know
    produces a filter-graph configuration error and a **failed job**, and R2.5 forbids failing a
    job over tone-mapping. So a probe that cannot run answers "missing", which degrades to an
    untone-mapped clip plus a marker -- the outcome R2.4 asks for.

    ``prober`` builds a **fresh** report rather than passing through to ``get_report``, and that
    is not a stylistic choice. ``get_report(prober)`` honours its argument *only on first
    construction* -- its own docstring says so -- and returns the process-wide singleton
    otherwise. So in any process where something has already probed a capability, an injected
    prober is accepted and silently ignored. That is worse than not supporting injection at all:
    a test that thinks it has removed ``zscale`` gets the real answer and passes for the wrong
    reason. Found exactly that way, by two tests here failing against a report the real ffmpeg
    had already populated.

    With no ``prober``, the shared report is used as intended, so production still costs one
    ``ffmpeg -filters`` per process rather than one per clip.
    """
    try:
        from worker.engines.capabilities import Capability_Report, get_report

        report = Capability_Report(prober) if prober is not None else get_report()
        for name in TONEMAP_REQUIRED_FILTERS:
            if not report.status(f"ffmpeg_filter:{name}").available:
                return name
    except Exception:
        # Fail closed: see the docstring. The first required filter is named because the
        # marker has to say *something* actionable, and "we could not probe" is not a
        # capability name a reader can look up.
        return TONEMAP_REQUIRED_FILTERS[0]
    return ""


def plan_colour(
    *,
    transfer: str,
    primaries: str,
    matrix: str,
    source_range: str,
    tone_map_enabled: bool = True,
    operator: str = DEFAULT_TONEMAP_OPERATOR,
    target_nits: int = DEFAULT_TONEMAP_TARGET_NITS,
    delivery_range: str = DEFAULT_DELIVERY_RANGE,
    prober: Optional["Prober"] = None,
) -> Colour_Plan:
    """Decide the whole colour treatment for one clip.

    Takes the probed fields as plain strings rather than a ``MediaInfo`` so it can be tested
    without constructing one, and so ``MediaInfo`` does not become a dependency of the
    decision logic.

    The order of the branches is the substance:

    * **HDR and tone-mapping wanted and possible** -> convert, tag Rec.709, mark it applied
      naming the detected transfer (R2.9).
    * **HDR but a filter is missing** -> deliver untone-mapped, tag what actually arrived, mark
      the degradation naming the missing capability (R2.4). Not a failure (R2.5).
    * **HDR but tone-mapping switched off** -> same, with a marker that says the operator chose
      this, so it cannot be mistaken for the degradation above.
    * **SDR or unknown** -> never tone-map (R2.6, R2.7). Resolve range only.
    """
    classification = classify_transfer(transfer)
    src_range = normalise_range(source_range)
    out_range = normalise_range(delivery_range) or DEFAULT_DELIVERY_RANGE

    filters: list[str] = []
    markers: list[str] = []
    tone_mapped = False

    if classification is Dynamic_Range.HDR:
        detected = (transfer or "").strip().lower()
        if not tone_map_enabled:
            markers.append("tone_map_skipped:disabled")
        else:
            missing = tonemap_filters_missing(prober)
            if missing:
                # Naming the capability id rather than just the filter, so the marker matches
                # what `capabilities.py` calls it and can be grepped for in one step.
                markers.append(f"tone_map_degraded:ffmpeg_filter:{missing}")
            else:
                filters.append(
                    tonemap_chain(operator=operator, target_nits=target_nits, out_range=out_range)
                )
                tone_mapped = True
                markers.append(f"tone_map:{resolve_operator(operator)}:{detected}")

    if tone_mapped:
        # The chain already ends at Rec.709 limited/full as asked, so the tags describe the
        # conversion's output. This is R3.2's whole point: the source said `smpte2084` and the
        # file no longer does.
        tags = colour_tag_args(
            transfer=BT709, primaries=BT709, matrix=BT709, colour_range=out_range
        )
        delivered_range = out_range
    else:
        # Nothing converted the primaries or matrix, so we state what arrived rather than
        # claiming Rec.709. An absent value stays absent -- see `colour_tag_args`.
        conversion = range_convert_chain(source_range=src_range, out_range=out_range)
        if conversion:
            filters.append(conversion)
            markers.append(f"colour_range_converted:{src_range}:{out_range}")
            delivered_range = out_range
        elif src_range:
            delivered_range = src_range
        else:
            # R3.7: the source was silent, so record which default we applied. Unlike a guard,
            # this genuinely is a choice the pipeline made on the operator's behalf, and it is
            # the first thing worth knowing when the blacks look wrong.
            markers.append(f"colour_range_assumed:{out_range}")
            delivered_range = out_range
        tags = colour_tag_args(
            transfer=(transfer or "").strip().lower(),
            primaries=(primaries or "").strip().lower(),
            matrix=(matrix or "").strip().lower(),
            colour_range=delivered_range,
        )

    return Colour_Plan(
        source_range=classification,
        filters=tuple(filters),
        tags=tags,
        markers=tuple(markers),
        tone_mapped=tone_mapped,
        delivered_range=delivered_range,
    )


def prepend_filters(plan: Colour_Plan, existing: str) -> str:
    """Put the colour chain in front of ``existing``, which may be empty.

    A helper rather than string arithmetic at each call site, because the *order* is the
    requirement (R2.2) and an inlined ``f"{a},{b}"`` at six sites is six chances to get it
    backwards. Tone-mapping after a grade applies the grade to PQ-coded values, and
    tone-mapping after a scale interpolates perceptual quantities rather than light -- which
    produces haloes that survive to the delivered file.
    """
    chain = plan.filter_chain
    if not chain:
        return existing
    if not existing:
        return chain
    return f"{chain},{existing}"


def merge_markers(applied: Sequence[str] | None, plan: Colour_Plan) -> list[str]:
    """Append the plan's markers to ``applied`` without duplicating any already present."""
    out = list(applied or [])
    for marker in plan.markers:
        if marker not in out:
            out.append(marker)
    return out
