"""Property + unit tests for b-roll planning, providers, and the engine.

Covers tasks 5.4–5.10. Property tests use ``hypothesis`` with
``@settings(max_examples=100)``, one property per test, tagged with the design
property text and a ``Validates: Requirements ...`` docstring. Reuses the
``FakeWord`` helper from ``tests/conftest.py`` and the shared ``SpyAssetProvider``
/ ``RecordingDownloader`` doubles from ``tests/fakes.py``.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.conftest import FakeWord
from tests.fakes import RecordingDownloader, SpyAssetProvider
from worker.effects.broll import (
    BROLL_INTENSITY,
    AssetRef,
    Broll_Engine,
    BrollCue,
    ExternalProvider,
    LocalProvider,
    broll_asset_record,
    plan_broll_cues,
    resolve_asset,
)
from worker.effects.filler import plan_keep_intervals, rebase_words
from worker.models import ClipResult, ProcessingOptions, effective_options

# Tokens that exercise the deterministic content-word heuristic: stopwords,
# long content words, ALL-CAPS acronyms, numerals, and short fillers.
_TOKEN_POOL = [
    "the",
    "a",
    "and",
    "of",
    "to",  # stopwords
    "revolutionary",
    "strategy",
    "algorithm",  # long content words
    "growth",
    "leverage",
    "compound",
    "framework",
    "NASA",
    "CEO",
    "AI",  # ALL-CAPS acronyms
    "$5",
    "42",
    "100%",  # numerals / currency
    "go",
    "win",
    "big",
    "now",  # short words
]

_INTENSITIES = list(BROLL_INTENSITY.keys())


@st.composite
def _timeline_and_duration(draw):
    """A clip-relative, start-ordered word timeline plus its clip duration."""
    duration = draw(st.floats(min_value=1.0, max_value=60.0))
    n = draw(st.integers(min_value=0, max_value=12))
    words = []
    for _ in range(n):
        start = draw(st.floats(min_value=0.0, max_value=duration))
        dur = draw(st.floats(min_value=0.05, max_value=2.0))
        end = min(duration, start + dur)
        words.append(FakeWord(round(start, 3), round(end, 3), draw(st.sampled_from(_TOKEN_POOL))))
    words.sort(key=lambda w: w.start)
    return words, duration


# --------------------------------------------------------------------------- #
# 5.4 — Property 12
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 12: B-roll cues are well-formed and bounded
@settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
@given(data=_timeline_and_duration(), intensity=st.sampled_from(_INTENSITIES))
def test_p12_broll_cues_well_formed_and_bounded(data, intensity):
    """Validates: Requirements 7.1, 7.2, 7.3, 7.5, 21.5

    At most one cue per selected phrase, each cue timed to a real word start,
    every window bounded within ``[0, D]``, and zero cues when disabled.
    """
    words, duration = data
    cues = plan_broll_cues(words, duration, intensity=intensity)

    if intensity == "off":
        assert cues == []
        return

    word_starts = {round(max(0.0, min(w.start, duration)), 3) for w in words}
    seen_starts = set()
    for cue in cues:
        # Bounded within the clip duration.
        assert 0.0 <= cue.start <= cue.end <= duration + 1e-6
        # Timed to a real source word's start.
        assert cue.start in word_starts
        # At most one cue per phrase (unique start positions).
        assert cue.start not in seen_starts
        seen_starts.add(cue.start)


# --------------------------------------------------------------------------- #
# 5.5 — Property 13
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 13: B-roll intensity caps count and total on-screen time
@settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
@given(data=_timeline_and_duration(), intensity=st.sampled_from(_INTENSITIES))
def test_p13_intensity_caps_count_and_total_time(data, intensity):
    """Validates: Requirements 7.4

    The planned cue count never exceeds the intensity's count cap and the summed
    on-screen duration never exceeds the intensity's duration cap.
    """
    words, duration = data
    max_count, max_total = BROLL_INTENSITY[intensity]
    cues = plan_broll_cues(words, duration, intensity=intensity)

    assert len(cues) <= max_count
    total_onscreen = sum(c.end - c.start for c in cues)
    assert total_onscreen <= max_total + 1e-6


# --------------------------------------------------------------------------- #
# 5.6 — Property 14
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 14: No b-roll cue lands in a removed interval
@settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
@given(data=_timeline_and_duration(), intensity=st.sampled_from(_INTENSITIES[1:]))
def test_p14_no_cue_in_removed_interval(data, intensity):
    """Validates: Requirements 11.2, 11.3

    Planning on the post-filler (rebased) timeline never yields a cue window
    exceeding the final clip duration, and every cue sits on a real rebased word
    start — so no cue can land inside a removed interval.
    """
    words, duration = data

    # Simulate filler removal: keep intervals, then rebase words onto them.
    plan = plan_keep_intervals(words, duration)
    keeps = plan.keeps
    final_duration = round(sum(k.duration for k in keeps), 3)
    rebased = rebase_words(words, keeps)

    cues = plan_broll_cues(rebased, final_duration, intensity=intensity)

    rebased_starts = {round(max(0.0, min(w.start, final_duration)), 3) for w in rebased}
    for cue in cues:
        assert 0.0 <= cue.start <= cue.end <= final_duration + 1e-6
        assert cue.start in rebased_starts


# --------------------------------------------------------------------------- #
# 5.7 — Property 15
# --------------------------------------------------------------------------- #
def _good_external_asset():
    return AssetRef(
        path="/tmp/ext.png",
        kind="image",
        provider="external",
        source_id="sid-1",
        license="CC0",
        attribution="Photo by X",
    )


def _local_asset():
    return AssetRef(
        path="/tmp/local.png",
        kind="image",
        provider="local",
        license="local",
    )


# Feature: tier1-creator-output-upgrade, Property 15: Asset-sourcing mode semantics
@settings(max_examples=100)
@given(
    keyword=st.text(min_size=1, max_size=10),
    local_hit=st.booleans(),
)
def test_p15_asset_sourcing_mode_semantics(keyword, local_hit):
    """Validates: Requirements 8.2, 8.3, 8.4, 8.5

    ``off`` queries no provider; ``local_only`` queries only local (never the
    external provider / downloader); ``local_then_external`` queries external
    only on a local miss with a configured key; a missing key makes
    ``local_then_external`` behave as ``local_only``.
    """
    local_result = _local_asset() if local_hit else None

    def fresh(has_key=True):
        local = SpyAssetProvider(name="local", result=local_result)
        dl = RecordingDownloader(result=_good_external_asset())
        api_key = "byok-key" if has_key else ""
        external = ExternalProvider(api_key, "https://api.example", downloader=dl)
        return local, dl, external

    # off -> nothing queried.
    local, dl, external = fresh()
    assert resolve_asset(keyword, "off", local, external) is None
    assert local.searches == []
    assert dl.calls == []

    # local_only -> only local; external/downloader never touched.
    local, dl, external = fresh()
    resolve_asset(keyword, "local_only", local, external)
    assert local.searches == [keyword]
    assert dl.calls == []

    # local_then_external with a key -> external only on local miss.
    local, dl, external = fresh(has_key=True)
    resolve_asset(keyword, "local_then_external", local, external)
    assert local.searches == [keyword]
    if local_hit:
        assert dl.calls == []  # local hit => no external call
    else:
        assert len(dl.calls) == 1  # local miss => external queried

    # local_then_external with no key -> behaves as local_only.
    local, dl, external = fresh(has_key=False)
    resolve_asset(keyword, "local_then_external", local, external)
    assert local.searches == [keyword]
    assert dl.calls == []


# --------------------------------------------------------------------------- #
# 5.8 — Property 16
# --------------------------------------------------------------------------- #
# Feature: tier1-creator-output-upgrade, Property 16: Permissibility forces local-only and mutes added audio
@settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
@given(
    data=_timeline_and_duration(),
    mode=st.sampled_from(["local_only", "local_then_external", "off"]),
)
def test_p16_permissibility_forces_local_only_and_mutes_audio(data, mode):
    """Validates: Requirements 8.6, 19.1, 19.3

    Under ``permissibility_mode`` the effective options mute added audio and
    force ``local_only`` sourcing, and the engine performs no external provider
    or downloader call regardless of other settings.
    """
    words, duration = data
    opts = ProcessingOptions(
        broll=True,
        broll_intensity="heavy",
        asset_sourcing_mode=mode,
        permissibility_mode=True,
        music="upbeat",
    )

    eff = effective_options(opts)
    assert eff.music == ""
    assert eff.asset_sourcing_mode == "local_only"

    local = SpyAssetProvider(name="local", result=_local_asset())
    dl = RecordingDownloader(result=_good_external_asset())
    external = ExternalProvider("byok-key", "https://api.example", downloader=dl)

    engine = Broll_Engine(opts, local=local, external=external)
    cues = engine.plan(words, duration)
    engine.resolve(cues)

    # No external download attempted under permissibility mode.
    assert dl.calls == []
    assert external.search  # sanity: provider exists but was never routed to


# --------------------------------------------------------------------------- #
# 5.9 — Property 17
# --------------------------------------------------------------------------- #
@st.composite
def _cue_specs(draw):
    n = draw(st.integers(min_value=0, max_value=8))
    return [draw(st.sampled_from(["good", "none", "unknown"])) for _ in range(n)]


# Feature: tier1-creator-output-upgrade, Property 17: Unusable cues are dropped, others retained
@settings(max_examples=100)
@given(specs=_cue_specs())
def test_p17_unusable_cues_dropped_others_retained(specs):
    """Validates: Requirements 9.1, 9.2, 20.3

    Cues whose asset is missing (``None``), fails to resolve, or carries an
    unknown (empty) license are dropped; every other cue is retained with a
    usable asset.
    """
    cues: list[BrollCue] = []
    results: dict = {}
    for i, kind in enumerate(specs):
        kw = f"k{i}"
        cues.append(BrollCue(float(i), float(i) + 1.0, kw))
        if kind == "good":
            results[kw] = AssetRef(f"/g{i}.png", "image", "local", license="local")
        elif kind == "none":
            results[kw] = None
        else:  # unknown license
            results[kw] = AssetRef(f"/u{i}.png", "image", "external", license="")

    local = SpyAssetProvider(name="local", results=results)
    engine = Broll_Engine(
        ProcessingOptions(broll=True, asset_sourcing_mode="local_only"),
        local=local,
    )
    resolved = engine.resolve(cues)

    kept = {c.keyword for c in resolved}
    expected = {f"k{i}" for i, kind in enumerate(specs) if kind == "good"}
    assert kept == expected
    for cue in resolved:
        assert cue.asset is not None and cue.asset.license


# --------------------------------------------------------------------------- #
# 5.10 — Unit tests: mode dispatch, defaults, DI, license, provenance shape
# --------------------------------------------------------------------------- #
def _content_words():
    """A small timeline that yields at least one b-roll keyword."""
    return [
        FakeWord(0.0, 0.4, "the"),
        FakeWord(0.5, 1.2, "revolutionary"),
        FakeWord(4.0, 4.6, "strategy"),
        FakeWord(8.0, 8.5, "algorithm"),
    ]


def test_broll_defaults_off():
    """Validates: Requirements 16.2 — b-roll and external sourcing default OFF."""
    opts = ProcessingOptions()
    assert opts.broll is False
    assert opts.asset_sourcing_mode == "off"
    assert opts.permissibility_mode is False


def test_engine_plan_returns_empty_when_disabled():
    """Validates: Requirements 8.1, 8.5 — disabled / off sourcing plans nothing."""
    words = _content_words()
    # b-roll toggle off.
    engine = Broll_Engine(ProcessingOptions(broll=False, asset_sourcing_mode="local_only"))
    assert engine.plan(words, 10.0) == []
    # b-roll on but sourcing off.
    engine = Broll_Engine(ProcessingOptions(broll=True, asset_sourcing_mode="off"))
    assert engine.plan(words, 10.0) == []


def test_mode_dispatch_local_only_uses_local_provider():
    """Validates: Requirements 8.1, 12.4 — DI local provider is queried."""
    words = _content_words()
    local = SpyAssetProvider(name="local", result=_local_asset())
    dl = RecordingDownloader(result=_good_external_asset())
    external = ExternalProvider("byok-key", "https://api", downloader=dl)
    engine = Broll_Engine(
        ProcessingOptions(broll=True, broll_intensity="standard", asset_sourcing_mode="local_only"),
        local=local,
        external=external,
    )
    cues = engine.plan(words, 12.0)
    assert cues  # keywords selected
    resolved = engine.resolve(cues)
    assert resolved
    assert local.searches  # local provider was consulted
    assert dl.calls == []  # never routed to the external downloader


def test_broll_provider_di_wiring_local_then_external():
    """Validates: Requirements 8.3, 12.3 — external DI adapter fetches on miss."""
    words = _content_words()
    local = SpyAssetProvider(name="local", result=None)  # always misses
    dl = RecordingDownloader(result=_good_external_asset())
    external = ExternalProvider("byok-key", "https://api", downloader=dl)
    engine = Broll_Engine(
        ProcessingOptions(
            broll=True, broll_intensity="standard", asset_sourcing_mode="local_then_external"
        ),
        local=local,
        external=external,
    )
    resolved = engine.resolve(engine.plan(words, 12.0))
    assert resolved
    assert dl.calls  # external downloader was invoked on local miss
    # External provenance (provider/source_id/license/attribution) preserved.
    asset = resolved[0].asset
    assert asset.provider == "external"
    assert asset.source_id == "sid-1"
    assert asset.license == "CC0"
    assert asset.attribution == "Photo by X"


def test_unknown_license_asset_dropped():
    """Validates: Requirements 20.3 — unknown-license assets are dropped."""
    words = _content_words()
    unknown = AssetRef("/x.png", "image", "external", source_id="s", license="")
    local = SpyAssetProvider(name="local", result=None)
    dl = RecordingDownloader(result=unknown)
    external = ExternalProvider("byok-key", "https://api", downloader=dl)
    engine = Broll_Engine(
        ProcessingOptions(broll=True, asset_sourcing_mode="local_then_external"),
        local=local,
        external=external,
    )
    resolved = engine.resolve(engine.plan(words, 12.0))
    assert resolved == []  # unknown license => all cues dropped
    assert dl.calls  # the downloader was still consulted


def test_local_then_external_no_key_behaves_as_local_only():
    """Validates: Requirements 8.4 — missing key downgrades to local_only."""
    words = _content_words()
    local = SpyAssetProvider(name="local", result=None)
    dl = RecordingDownloader(result=_good_external_asset())
    external = ExternalProvider("", "https://api", downloader=dl)  # no key
    engine = Broll_Engine(
        ProcessingOptions(broll=True, asset_sourcing_mode="local_then_external"),
        local=local,
        external=external,
    )
    engine.resolve(engine.plan(words, 12.0))
    assert dl.calls == []  # no external call without a key


def test_broll_asset_record_shape_for_clip_result():
    """Validates: Requirements 12.3, 12.4, 16.2, 20.2 — provenance shape.

    A composited external asset's provenance is shaped for
    ``ClipResult.broll_assets`` with the documented keys and serialises cleanly.
    """
    cue = BrollCue(
        1.0,
        3.5,
        "strategy",
        asset=AssetRef(
            "/tmp/ext.mp4",
            "video",
            "external",
            source_id="sid-9",
            license="CC-BY",
            attribution="Clip by Y",
        ),
    )
    record = broll_asset_record(cue)
    assert set(record) == {
        "provider",
        "source_id",
        "license",
        "attribution",
        "keyword",
        "path",
    }
    assert record == {
        "provider": "external",
        "source_id": "sid-9",
        "license": "CC-BY",
        "attribution": "Clip by Y",
        "keyword": "strategy",
        "path": "/tmp/ext.mp4",
    }

    clip = ClipResult(
        id="c1",
        filename="c1.mp4",
        start=0.0,
        end=10.0,
        duration=10.0,
        broll_assets=[record],
    )
    assert clip.to_dict()["broll_assets"] == [record]


def test_local_provider_matches_by_stem(tmp_path):
    """Validates: Requirements 8.2, 12.2 — local match records source path/kind."""
    (tmp_path / "ocean_waves.jpg").write_bytes(b"img")
    (tmp_path / "city.mp4").write_bytes(b"vid")
    provider = LocalProvider(tmp_path)

    img = provider.search("ocean")
    assert img is not None
    assert img.kind == "image"
    assert img.provider == "local"
    assert img.license == "local"
    assert img.path.endswith("ocean_waves.jpg")

    vid = provider.search("city")
    assert vid is not None and vid.kind == "video"

    assert provider.search("mountains") is None  # no match, no network
