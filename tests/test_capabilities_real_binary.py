"""Capability probes checked against the **real** ffmpeg binary.

Every other capability test in this suite injects a prober or a canned
``ffmpeg -filters`` listing. That isolation is usually right, but it left one whole class
of defect invisible: if the probe's understanding of ffmpeg's output is wrong, every
mocked test still passes while the feature is dead on every real machine.

That is not hypothetical. ``_ffmpeg_filter_names`` identified the flags column with
``not parts[0].isalnum()``, which fails for a filter with every flag set (``TSC
highpass``). 124 of ffmpeg 7.0's 486 filters became invisible, which made the
``stem_inpainting`` ffmpeg backend permanently unavailable — and 598 tests passed,
because no canned listing contained an all-flags row.

The tests here close that gap by cross-checking the probe against an **independent**
mechanism: ``ffmpeg -h filter=<name>`` interrogates one filter directly and shares no
parsing code with the ``-filters`` table. Agreement between two independent sources is
what makes a parser bug detectable.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker.engines import loader  # noqa: F401 - registers the built-in engines
from worker.engines.capabilities import _ffmpeg_filter_names, default_prober
from worker.engines.registry import get_registry

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg binary on PATH; real-binary checks need one"
)

#: Filters ffmpeg 7.x reports with *every* flag set, i.e. a dot-free flags group.
#: These are the exact shape that the previous parser dropped. Kept as an explicit list
#: so the regression is named rather than merely covered by chance.
ALL_FLAGS_FILTERS = ("highpass", "lowpass", "bass", "treble", "equalizer", "biquad")

#: Filters with a dot-bearing flags group, which the old parser handled correctly. Their
#: presence keeps the comparison honest: the fix must not have broken the easy case.
SOME_FLAGS_FILTERS = ("aeval", "amix", "volume", "atrim", "asplit", "afade")


def _filter_exists_independently(name: str) -> bool:
    """Whether ffmpeg knows ``name``, via ``-h filter=`` rather than the table.

    Deliberately a different code path from :func:`_ffmpeg_filter_names`: it queries one
    filter instead of parsing a listing, so the two cannot share a bug. ffmpeg exits 0
    either way, so the answer is in the text.
    """
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-h", f"filter={name}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return "Unknown filter" not in output and f"Filter {name}" in output


def _declared_filter_capabilities() -> list[str]:
    """Every ``ffmpeg_filter:<name>`` capability declared by a registered engine.

    Read from the registry rather than hard-coded, so a filter added to an engine is
    covered here automatically instead of silently going unverified.
    """
    names: set[str] = set()
    for engine in get_registry().all():
        declared = tuple(getattr(engine, "required_capabilities", ())) + tuple(
            getattr(engine, "optional_capabilities", ())
        )
        for capability in declared:
            text = str(capability)
            if text.startswith("ffmpeg_filter:"):
                names.add(text.split(":", 1)[1])
    return sorted(names)


# ---------------------------------------------------------------------------
# The listing parser agrees with ffmpeg itself
# ---------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.parametrize("name", ALL_FLAGS_FILTERS + SOME_FLAGS_FILTERS)
def test_the_probe_agrees_with_ffmpeg_about_each_filter(name):
    """Two independent sources must give the same answer for the same filter.

    A disagreement means the probe is misreading ffmpeg's output — precisely the failure
    that made a whole feature unreachable while the suite stayed green.
    """
    truth = _filter_exists_independently(name)
    probed = default_prober(f"ffmpeg_filter:{name}").available
    assert probed == truth, (
        f"{name!r}: 'ffmpeg -h filter=' says exists={truth} but the capability probe "
        f"says available={probed}"
    )


@requires_ffmpeg
def test_the_parser_finds_as_many_filters_as_ffmpeg_lists():
    """The parsed name count matches the number of filter rows ffmpeg printed.

    A count comparison catches wholesale loss that a per-filter spot check can miss: the
    old parser dropped roughly a quarter of all filters, none of which any test named.
    """
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    listing = (proc.stdout or "") + (proc.stderr or "")
    # Count rows that carry a pad spec, which is what makes a line a filter row.
    rows = [line for line in listing.splitlines() if "->" in line and len(line.split()) >= 4]

    parsed = _ffmpeg_filter_names()
    assert len(parsed) == len(rows), (
        f"ffmpeg printed {len(rows)} filter rows but the parser produced " f"{len(parsed)} names"
    )


@requires_ffmpeg
def test_no_flags_group_is_mistaken_for_a_filter_name():
    """No parsed name looks like a flags column.

    ``TSC`` and friends are three upper-case characters. The old parser recorded them as
    filter names; a filter genuinely called that does not exist.
    """
    bogus = sorted(n for n in _ffmpeg_filter_names() if len(n) == 3 and n.isupper())
    assert bogus == [], f"flags groups captured as filter names: {bogus}"


@requires_ffmpeg
def test_an_invented_filter_is_reported_unavailable():
    """The probe says no when the answer is no, so availability means something."""
    assert default_prober("ffmpeg_filter:definitely_not_a_real_filter").available is False


# ---------------------------------------------------------------------------
# Every capability the engines actually rely on
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_every_filter_an_engine_declares_agrees_with_the_binary():
    """Each engine-declared filter is checked against ffmpeg, in one report.

    Asserting over the whole set at once means a failure names every mismatching filter
    rather than stopping at the first, which is what made the original bug's scale clear.
    """
    declared = _declared_filter_capabilities()
    assert declared, "expected the registered engines to declare ffmpeg filters"

    mismatches = {
        name: (truth, probed)
        for name in declared
        for truth, probed in [
            (
                _filter_exists_independently(name),
                default_prober(f"ffmpeg_filter:{name}").available,
            )
        ]
        if truth != probed
    }
    assert not mismatches, f"probe disagrees with ffmpeg for: {mismatches}"


@requires_ffmpeg
def test_the_filters_the_stem_ffmpeg_backend_needs_are_actually_available():
    """``highpass`` and ``lowpass`` resolve on a normal ffmpeg build.

    This is the concrete consequence the bug had: both are ``TSC`` filters, both are
    required by the stem ffmpeg backend, so the engine reported
    ``unavailable:ffmpeg_filter:highpass`` on every host and silently did nothing.
    """
    for name in ("highpass", "lowpass"):
        status = default_prober(f"ffmpeg_filter:{name}")
        assert (
            status.available is True
        ), f"{name} unavailable on a real ffmpeg build: {status.detail}"


# ---------------------------------------------------------------------------
# The other probe kinds, against reality
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_the_binary_probe_matches_the_path_lookup():
    """``binary:<name>`` reflects what is genuinely installed."""
    assert default_prober("binary:ffmpeg").available is (FFMPEG is not None)
    assert default_prober("binary:ffprobe").available is (FFPROBE is not None)
    assert default_prober("binary:definitely-not-installed-xyz").available is False


def test_the_python_package_probe_matches_importlib():
    """``python_pkg:<module>`` agrees with ``importlib.util.find_spec``.

    Needs no binary, so it runs everywhere. ``pathlib`` is always importable; the second
    name never is.
    """
    for module in ("pathlib", "definitely_not_an_installed_module_xyz"):
        expected = importlib.util.find_spec(module) is not None
        assert default_prober(f"python_pkg:{module}").available is expected


def test_probing_every_declared_capability_never_raises():
    """No engine declares a capability the prober cannot evaluate.

    A probe that raises would surface as an engine failure at run time rather than as a
    clean ``unavailable``, so this asserts total behaviour across the real registry.
    """
    engines = get_registry().all()
    assert engines, "expected the loader to have registered engines"

    for engine in engines:
        declared = tuple(getattr(engine, "required_capabilities", ())) + tuple(
            getattr(engine, "optional_capabilities", ())
        )
        for capability in declared:
            status = default_prober(str(capability))
            assert isinstance(status.available, bool)
            assert status.capability_id == str(capability)
