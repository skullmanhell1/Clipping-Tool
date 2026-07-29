"""Capability probe / report module for the av-engines-foundation spec
(``worker/engines/capabilities.py``).

Covers the design's numbered properties for the capability layer:

* **P10** — probing is total, offline, and shaped (task 6.3).
* **P11** — the report caches, is deterministic, serialises, and invalidates (task 6.4).

plus the per-kind unit tests (task 6.5), which stub each collaborator
(``importlib.util.find_spec``, ``shutil.which``, the ``ffmpeg -filters`` listing,
``worker.captions.font_available``, ``settings.<name>_api_key``,
``worker.llm_client.llm_available``, a registered model locator) and pin
``parse_capability_id``.

Generators come from the shared ``tests/strategies.py`` module (``st_capability_id``,
``st_availability_map``) and the probers are ``tests.fakes`` doubles
(``StaticProber``, ``CountingProber``, ``RaisingProber``) — never redefined here — so
the sibling engine specs exercise the same input space and the same doubles.

Global state: ``get_report()`` is a process singleton and ``MODEL_LOCATORS`` is
module-level mutable state. The autouse fixture resets both around every test
*function*, but hypothesis runs ~100 examples inside a single function call, so each
property body also calls ``reset_report()`` itself and any locator registration is
undone in a ``finally``.

Everything here is pure and offline: no ffmpeg process, no font enumeration, no
network — the probe environment used by the properties installs a socket guard that
raises if anything so much as constructs a socket.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import shutil
import subprocess
from typing import Any, List
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from config import settings as app_settings
from tests.fakes import CountingProber, RaisingProber, StaticProber
from tests.strategies import st_availability_map, st_capability_id
from worker.engines import capabilities as capabilities_module
from worker.engines.capabilities import (
    LLM_CAPABILITY,
    MAX_DETAIL_LENGTH,
    MODEL_LOCATORS,
    Capability_Kind,
    Capability_Report,
    Capability_Status,
    default_prober,
    get_report,
    parse_capability_id,
    reset_report,
)

#: Exception types an injected prober realistically explodes with; P10 asserts the
#: class name of each survives into ``detail``.
PROBE_EXCEPTIONS = (
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    OSError,
    ImportError,
    TimeoutError,
    MemoryError,
    subprocess.TimeoutExpired,
)


def build_exception(exc_type: type) -> BaseException:
    """Instantiate ``exc_type`` with whatever arguments it demands."""
    if exc_type is subprocess.TimeoutExpired:
        return exc_type(cmd=["ffmpeg", "-filters"], timeout=20.0)
    return exc_type("probe exploded")

#: A canned ``<ffmpeg_binary> -hide_banner -filters`` listing in ffmpeg's real column
#: format (``<flags> <name> <pads> <description>``), so the probe's own parser runs
#: for real without spawning a process.
FFMPEG_FILTER_LISTING = "\n".join(
    [
        "Filters:",
        "  T.. atempo            A->A       Adjust audio tempo.",
        "  ... loudnorm          A->A       EBUR128 loudness normalization.",
        "  ..C scale             V->V       Scale the input video size.",
        "  T.. drawtext          V->V       Draw text on top of video frames.",
        # Every-flag-set rows print a dot-free flags group. Real ffmpeg emits these
        # for 124 of its 486 filters, so the canned listing must contain them or the
        # parser's hardest case goes unexercised (see ALL_FLAGS_FILTER).
        "  TSC highpass          A->A       Apply a high-pass filter with 3dB point frequency.",
        "  TSC lowpass           A->A       Apply a low-pass filter with 3dB point frequency.",
        # A variadic-input pad spec ("N->A"), so pad parsing is not tied to "A->A".
        "  ..C amix              N->A       Audio mixing.",
        "",
    ]
)

#: Filter names the canned listing contains / cannot contain.
LISTED_FILTER = "atempo"
UNLISTED_FILTER = "definitely_not_a_real_filter"

#: A filter the canned listing reports with *every* flag set (``TSC``) — the case an
#: ``isalnum()``-based flags test misparses, recording ``"TSC"`` as the filter name and
#: losing ``highpass`` entirely.
ALL_FLAGS_FILTER = "highpass"

#: The flags group of :data:`ALL_FLAGS_FILTER`, which must never be mistaken for a name.
ALL_FLAGS_GROUP = "TSC"


class Network_Guard_Error(RuntimeError):
    """Raised by the socket guard; its class name is the tell-tale in a probe detail."""


class Network_Guard:
    """Stands in for every ``socket`` entry point and refuses to be used.

    Every attempt is recorded *and* raises, so a probe that reached the network is
    caught either by ``attempts`` being non-empty or — because ``default_prober``
    swallows errors — by ``Network_Guard_Error`` showing up in a ``detail`` string.
    """

    def __init__(self) -> None:
        self.attempts: List[Any] = []
        self.ffmpeg_runs: List[List[str]] = []

    def __call__(self, *args: Any, **kwargs: Any):
        self.attempts.append((args, kwargs))
        raise Network_Guard_Error("network access attempted during capability probe")


@contextlib.contextmanager
def offline_probe_environment(*, font_available: bool = True):
    """Run probes with the network guarded and the two process/host collaborators stubbed.

    * ``socket.socket`` / ``socket.create_connection`` / ``socket.getaddrinfo`` are
      replaced by a :class:`Network_Guard` — the socket guard P10 asks for.
    * ``subprocess.run`` returns the canned :data:`FFMPEG_FILTER_LISTING`, so the
      ffmpeg-filter probe stays fast and machine-independent (a host with ffmpeg
      installed would otherwise spawn one process per example) while the probe's own
      settings lookup and listing parser still run.
    * ``worker.captions.font_available`` is stubbed so the font probe neither shells
      out to ``fc-list`` nor pollutes the module-level font cache other tests read.

    The collaborators are imported before the guard goes up, so importing them can
    never be mistaken for a network attempt.
    """
    from worker import captions, llm_client  # noqa: F401 - pre-warm before guarding

    guard = Network_Guard()

    def _fake_run(command, *args: Any, **kwargs: Any):
        recorded = list(command) if isinstance(command, (list, tuple)) else [command]
        guard.ffmpeg_runs.append([str(part) for part in recorded])
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=FFMPEG_FILTER_LISTING, stderr=""
        )

    with mock.patch.object(captions, "font_available", lambda name: font_available), \
         mock.patch("subprocess.run", _fake_run), \
         mock.patch("socket.socket", guard), \
         mock.patch("socket.create_connection", guard), \
         mock.patch("socket.getaddrinfo", guard):
        yield guard


@pytest.fixture(autouse=True)
def clean_capability_state():
    """Reset the process-wide report and the model-locator registry around each test.

    Both are module-level mutable state (Req 22.1/22.2): the singleton report is
    dropped before *and* after, and ``MODEL_LOCATORS`` is snapshotted and restored so
    a registration made by one test can never leak into another.
    """
    reset_report()
    snapshot = dict(MODEL_LOCATORS)
    yield
    MODEL_LOCATORS.clear()
    MODEL_LOCATORS.update(snapshot)
    reset_report()


def assert_shaped(status: Any, capability_id: str) -> None:
    """A ``Capability_Status`` with a real ``bool`` and a short, single-line ``str``."""
    assert isinstance(status, Capability_Status)
    assert type(status.available) is bool
    assert isinstance(status.detail, str)
    assert isinstance(status.capability_id, str)
    assert status.capability_id == capability_id
    assert len(status.detail) <= MAX_DETAIL_LENGTH
    assert "\n" not in status.detail
    assert "\x00" not in status.detail


# Feature: av-engines-foundation, Property 10: Probing is total, offline, and shaped —
# *For any* string Capability_Id — well-formed or not — `default_prober` returns a
# `Capability_Status` with a `bool` `available` and a `str` `detail` without raising;
# *for any* exception raised by an injected prober, the status is unavailable with the
# exception class name in `detail`; and *for any* set of Capability_Ids, probing
# performs zero network calls (socket guard). Model capabilities with no registered
# locator report unavailable.
@settings(max_examples=100, deadline=None)
@given(capability_id=st_capability_id(), exc_type=st.sampled_from(PROBE_EXCEPTIONS))
def test_p10_probing_is_total_offline_and_shaped(capability_id, exc_type):
    """Validates: Requirements 5.2, 5.3, 5.6, 21.5"""
    # The autouse fixture runs once per test *function* while hypothesis runs many
    # examples inside it — so each example resets the shared singleton itself.
    reset_report()

    with offline_probe_environment() as guard:
        # Totality and shape for an arbitrary id (Req 5.2).
        status = default_prober(capability_id)
        assert_shaped(status, capability_id)
        # The serialisable form keeps the same shape and round-trips.
        payload = status.to_dict()
        assert payload == {
            "capability_id": capability_id,
            "available": status.available,
            "detail": status.detail,
        }
        assert Capability_Status.from_dict(payload) == status

        # An injected prober that raises reports unavailable, naming the class (Req 5.3).
        raiser = RaisingProber(build_exception(exc_type))
        report = Capability_Report(prober=raiser)
        raised = report.status(capability_id)
        assert_shaped(raised, capability_id)
        assert raised.available is False
        assert exc_type.__name__ in raised.detail
        assert raiser.calls == [capability_id]

        # A model capability with no registered locator is unavailable (Req 21.5).
        _, drawn_name = parse_capability_id(capability_id)
        for model_name in ("htdemucs_unregistered", drawn_name):
            model_id = f"{Capability_Kind.MODEL.value}:{model_name}"
            parsed_kind, parsed_name = parse_capability_id(model_id)
            assert parsed_kind == Capability_Kind.MODEL.value
            assert parsed_name not in MODEL_LOCATORS
            model_status = default_prober(model_id)
            assert_shaped(model_status, model_id)
            assert model_status.available is False
            if parsed_name:
                assert "no locator registered" in model_status.detail

        # Zero network access, whichever kind was drawn (Req 5.6): the guard was
        # never touched, and no probe swallowed a guard error either.
        assert guard.attempts == []
        for detail in (status.detail, raised.detail, model_status.detail):
            assert Network_Guard_Error.__name__ not in detail


# Feature: av-engines-foundation, Property 11: The report caches, is deterministic,
# serialises, and invalidates — *For any* set of Capability_Ids and *for any* injected
# availability map, a counting prober is invoked at most once per id however often
# `status()` is called; two `to_dict()` calls are equal; `to_dict()` is
# JSON-round-trippable with sorted keys; `available(id)` equals the injected map value;
# and after `invalidate()` the next `status()` re-probes exactly once.
@settings(max_examples=100, deadline=None)
@given(
    availability=st_availability_map(),
    extra_ids=st.lists(st_capability_id(), max_size=4),
)
def test_p11_report_caches_is_deterministic_serialises_and_invalidates(
    availability, extra_ids
):
    """Validates: Requirements 5.7, 6.1, 6.2, 6.3, 6.4, 6.5, 20.2"""
    # Per-example isolation of the process singleton (see P10's note above).
    reset_report()
    try:
        ids = list(availability) + list(extra_ids)
        unique = set(ids)
        expected = {cid: bool(availability.get(cid, False)) for cid in unique}

        counting = CountingProber(StaticProber(availability))
        report = Capability_Report(prober=counting)          # injected prober (Req 5.7)

        # However often status() is called, the prober runs at most once per id
        # (Reqs 6.1, 6.2) and always answers the injected map (Req 6.3).
        for _ in range(3):
            for cid in ids:
                status = report.status(cid)
                assert isinstance(status, Capability_Status)
                assert status.capability_id == cid
                assert status.available is expected[cid]
                assert report.available(cid) is expected[cid]
        for cid in unique:
            assert counting.count_for(cid) == 1
        assert counting.total == len(unique)

        # Serialisation is stable, sorted, and JSON round-trippable (Reqs 6.4, 20.2).
        first = report.to_dict()
        second = report.to_dict()
        assert first == second
        assert list(first) == sorted(first)
        assert set(first) == unique
        encoded = json.dumps(first, sort_keys=True)
        decoded = json.loads(encoded)
        assert decoded == first
        assert list(decoded) == sorted(decoded)
        for cid, entry in first.items():
            assert entry["available"] is expected[cid]
            assert Capability_Status.from_dict(entry) == report.status(cid)
        assert counting.total == len(unique)          # serialising never re-probes

        if ids:
            # Invalidating one id re-probes it exactly once, and only it (Req 6.5).
            target = ids[0]
            before_total = counting.total
            report.invalidate(target)
            assert target not in report.to_dict()
            for _ in range(3):
                assert report.status(target).available is expected[target]
            assert counting.count_for(target) == 2
            assert counting.total == before_total + 1

        # Invalidating everything empties the cache and re-probes each id once.
        before_total = counting.total
        report.invalidate()
        assert report.to_dict() == {}
        for cid in ids:
            report.status(cid)
            report.status(cid)
        assert counting.total == before_total + len(unique)

        # The process-wide report honours the prober injected at first construction
        # and caches identically (Reqs 6.1, 5.7).
        singleton_prober = CountingProber(StaticProber(availability))
        singleton = get_report(singleton_prober)
        assert get_report() is singleton
        for cid in ids:
            assert singleton.status(cid).available is expected[cid]
            assert singleton.available(cid) is expected[cid]
        assert singleton_prober.total == len(unique)
    finally:
        reset_report()


# --------------------------------------------------------------------------- #
# Unit tests (task 6.5): one per capability kind, collaborator stubbed         #
# --------------------------------------------------------------------------- #
def test_python_pkg_kind_dispatches_to_find_spec(monkeypatch):
    """``python_pkg:<module>`` asks ``importlib.util.find_spec`` and nothing else.

    Validates: Requirements 5.1
    """
    asked: List[str] = []

    def fake_find_spec(name):
        asked.append(name)
        return object() if name == "demucs" else None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    found = default_prober("python_pkg:demucs")
    assert found.available is True
    assert "demucs" in found.detail

    missing = default_prober("python_pkg:not_installed_pkg")
    assert missing.available is False
    assert "not_installed_pkg" in missing.detail

    assert asked == ["demucs", "not_installed_pkg"]

    # No module name at all is unavailable without consulting the collaborator.
    assert default_prober("python_pkg:").available is False
    assert asked == ["demucs", "not_installed_pkg"]


def test_binary_kind_dispatches_to_shutil_which(monkeypatch):
    """``binary:<exe>`` asks ``shutil.which`` and reports the resolved path.

    Validates: Requirements 5.1
    """
    asked: List[str] = []

    def fake_which(name, *args, **kwargs):
        asked.append(name)
        return "/usr/local/bin/ffprobe" if name == "ffprobe" else None

    monkeypatch.setattr(shutil, "which", fake_which)

    found = default_prober("binary:ffprobe")
    assert found.available is True
    assert "/usr/local/bin/ffprobe" in found.detail

    missing = default_prober("binary:nosuchtool")
    assert missing.available is False
    assert "nosuchtool" in missing.detail

    assert asked == ["ffprobe", "nosuchtool"]


def test_ffmpeg_filter_kind_invokes_the_configured_binary(monkeypatch):
    """``ffmpeg_filter:<name>`` shells out to ``settings.ffmpeg_binary``, never a
    hard-coded ``"ffmpeg"``, and answers from that binary's own listing.

    Validates: Requirements 5.1, 5.4
    """
    sentinel = "/opt/sentinel/ffmpeg-build-7x"
    monkeypatch.setattr(app_settings, "ffmpeg_binary", sentinel)

    commands: List[List[str]] = []

    def fake_run(command, *args, **kwargs):
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout=FFMPEG_FILTER_LISTING, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    listed = default_prober(f"ffmpeg_filter:{LISTED_FILTER}")
    assert listed.available is True
    assert LISTED_FILTER in listed.detail

    unlisted = default_prober(f"ffmpeg_filter:{UNLISTED_FILTER}")
    assert unlisted.available is False
    assert UNLISTED_FILTER in unlisted.detail

    # The sentinel binary — not "ffmpeg" — is what was actually executed (Req 5.4).
    assert len(commands) == 2
    for command in commands:
        assert command[0] == sentinel
        assert command[0] != "ffmpeg"
        assert "-filters" in command


def test_ffmpeg_filter_kind_parses_rows_whose_flags_group_has_no_dot(monkeypatch):
    """A filter listed with *every* flag set (``TSC highpass``) is found, and its flags
    group is never mistaken for a filter name.

    Regression: the parser previously identified the flags column with
    ``not parts[0].isalnum()``. That holds for ``T..``/``..C`` but *fails* for a
    dot-free group like ``TSC``, so the row fell through to a bare-name branch which
    recorded ``"TSC"`` and dropped ``highpass``. On real ffmpeg 7.0 this hid 124 of
    486 filters — including ``highpass``/``lowpass``, both required by the stem
    inpainting ffmpeg backend, which therefore reported ``unavailable`` on every host
    no matter how ffmpeg was built. No canned listing exercised an all-flags row, so
    the whole suite passed while the feature could not run in production.

    Validates: Requirements 5.1, 5.4
    """
    monkeypatch.setattr(app_settings, "ffmpeg_binary", "/opt/sentinel/ffmpeg")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, *a, **k: subprocess.CompletedProcess(
            args=command, returncode=0, stdout=FFMPEG_FILTER_LISTING, stderr=""
        ),
    )

    found = default_prober(f"ffmpeg_filter:{ALL_FLAGS_FILTER}")
    assert found.available is True, (
        f"{ALL_FLAGS_FILTER!r} is listed with flags {ALL_FLAGS_GROUP!r} and must be found"
    )
    assert ALL_FLAGS_FILTER in found.detail

    # The flags group itself is not a filter, so it must not be probeable as one.
    flags_as_name = default_prober(f"ffmpeg_filter:{ALL_FLAGS_GROUP}")
    assert flags_as_name.available is False, (
        f"{ALL_FLAGS_GROUP!r} is a flags column, not a filter name"
    )


def test_ffmpeg_filter_parser_keeps_every_listed_name_and_only_those(monkeypatch):
    """The parser returns exactly the listing's filter names — no flags groups, no
    banner text, and nothing dropped.

    Guards the parser as a whole rather than one filter: an off-by-one in the column
    logic shows up here as a set difference, whichever row it affects.

    Validates: Requirements 5.4
    """
    monkeypatch.setattr(app_settings, "ffmpeg_binary", "/opt/sentinel/ffmpeg")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, *a, **k: subprocess.CompletedProcess(
            args=command, returncode=0, stdout=FFMPEG_FILTER_LISTING, stderr=""
        ),
    )

    assert capabilities_module._ffmpeg_filter_names() == {
        "atempo",
        "loudnorm",
        "scale",
        "drawtext",
        "highpass",
        "lowpass",
        "amix",
    }


def test_font_kind_dispatches_to_captions_font_available(monkeypatch):
    """``font:<family>`` reuses ``worker.captions.font_available``.

    Validates: Requirements 5.1, 5.5
    """
    from worker import captions

    asked: List[str] = []

    def fake_font_available(name):
        asked.append(name)
        return name == "Impact"

    monkeypatch.setattr(captions, "font_available", fake_font_available)

    installed = default_prober("font:Impact")
    assert installed.available is True
    assert "Impact" in installed.detail

    absent = default_prober("font:No Such Family")
    assert absent.available is False
    assert "No Such Family" in absent.detail

    # The family name reaches the helper case-preserved and untouched.
    assert asked == ["Impact", "No Such Family"]


def test_provider_key_kind_reads_the_settings_api_key(monkeypatch):
    """``provider_key:<provider>`` reads ``settings.<provider>_api_key``.

    Validates: Requirements 5.1
    """
    monkeypatch.setattr(app_settings, "openai_api_key", "sk-configured")
    monkeypatch.setattr(app_settings, "broll_provider_api_key", None)

    configured = default_prober("provider_key:openai")
    assert configured.available is True
    assert "openai_api_key" in configured.detail

    unset = default_prober("provider_key:broll")
    assert unset.available is False
    assert "broll_provider_api_key" in unset.detail

    # A blank key counts as unconfigured, and an unknown provider has no setting.
    monkeypatch.setattr(app_settings, "openai_api_key", "   ")
    assert default_prober("provider_key:openai").available is False
    unknown = default_prober("provider_key:no_such_provider")
    assert unknown.available is False
    assert "no_such_provider_api_key" in unknown.detail


def test_llm_kind_dispatches_to_llm_client_llm_available(monkeypatch):
    """The bare ``llm`` capability reuses ``worker.llm_client.llm_available``.

    Validates: Requirements 5.1, 5.5
    """
    from worker import llm_client

    calls: List[int] = []

    def fake_llm_available():
        calls.append(1)
        return True

    monkeypatch.setattr(llm_client, "llm_available", fake_llm_available)
    available = default_prober(LLM_CAPABILITY)
    assert available.available is True
    assert "credentials" in available.detail

    monkeypatch.setattr(llm_client, "llm_available", lambda: False)
    assert default_prober(LLM_CAPABILITY).available is False
    assert calls == [1]


def test_model_kind_dispatches_to_the_registered_locator(tmp_path):
    """``model:<name>`` consults ``MODEL_LOCATORS``; an absent locator or an absent
    local file is unavailable (no download).

    Validates: Requirements 5.1
    """
    present = tmp_path / "htdemucs.th"
    present.write_bytes(b"weights")
    calls: List[str] = []

    # Unregistered before anything is added.
    assert default_prober("model:htdemucs").available is False

    try:
        MODEL_LOCATORS["htdemucs"] = lambda: (calls.append("htdemucs"), present)[1]
        MODEL_LOCATORS["absent"] = lambda: (calls.append("absent"), None)[1]
        MODEL_LOCATORS["dangling"] = lambda: tmp_path / "not-downloaded.th"

        found = default_prober("model:htdemucs")
        assert found.available is True
        assert str(present) in found.detail

        missing = default_prober("model:absent")
        assert missing.available is False
        assert "absent" in missing.detail

        dangling = default_prober("model:dangling")
        assert dangling.available is False

        assert calls == ["htdemucs", "absent"]
    finally:
        for name in ("htdemucs", "absent", "dangling"):
            MODEL_LOCATORS.pop(name, None)

    assert default_prober("model:htdemucs").available is False


def test_parse_capability_id_handles_llm_and_unknown_kinds():
    """``"llm"`` needs no name; unknown kinds parse as ``("", id)``.

    Validates: Requirements 5.1, 5.5
    """
    # The one nameless capability, surrounding whitespace and case tolerated.
    assert parse_capability_id(LLM_CAPABILITY) == (Capability_Kind.LLM.value, "")
    assert parse_capability_id("  llm  ") == (Capability_Kind.LLM.value, "")
    assert parse_capability_id("LLM") == (Capability_Kind.LLM.value, "")

    # "llm" is a known kind, so a trailing name parses as that kind's (ignored) name.
    assert parse_capability_id("llm:extra") == (Capability_Kind.LLM.value, "extra")

    # Known kinds split into (kind, name), kind case-insensitively.
    assert parse_capability_id("python_pkg:demucs") == ("python_pkg", "demucs")
    assert parse_capability_id("PYTHON_PKG:demucs") == ("python_pkg", "demucs")
    assert parse_capability_id("font: Inter Bold ") == ("font", "Inter Bold")
    assert parse_capability_id("python_pkg:") == ("python_pkg", "")

    # Unknown or structurally broken ids report no kind and echo the id back.
    for identifier in ("", ":", "unknown_kind:demucs", "binary", ":demucs", "🎬:emoji"):
        assert parse_capability_id(identifier) == ("", identifier)

    # An unknown kind probes as unavailable rather than raising (Req 5.2).
    unknown = default_prober("unknown_kind:demucs")
    assert unknown.available is False
    assert "unknown capability kind" in unknown.detail


def test_default_prober_never_raises_for_hostile_ids():
    """The documented totality contract, spelled out on concrete hostile inputs.

    Validates: Requirements 5.2, 5.3
    """
    with offline_probe_environment():
        for identifier in ("", " ", ":", "::", "model:\x00null", "x" * 500, "🎬"):
            status = default_prober(identifier)
            assert isinstance(status, Capability_Status)
            assert status.available is False
            assert isinstance(status.detail, str)
            assert len(status.detail) <= MAX_DETAIL_LENGTH


def test_capability_report_wraps_a_raising_prober():
    """An injected prober that raises becomes an unavailable status, not an exception.

    Validates: Requirements 5.3, 5.7
    """
    raiser = RaisingProber(OSError("probe blew up"))
    report = Capability_Report(prober=raiser)
    status = report.status("python_pkg:demucs")
    assert status.available is False
    assert "OSError" in status.detail
    # The failure is cached like any other answer: one probe, then the cache.
    assert report.status("python_pkg:demucs") == status
    assert raiser.calls == ["python_pkg:demucs"]


def test_default_prober_is_used_when_no_prober_is_injected(monkeypatch):
    """With no injected prober the report falls back to ``default_prober``.

    Validates: Requirements 5.1, 5.7
    """
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    report = Capability_Report()
    assert report.available("python_pkg:json") is True
    assert report.to_dict()["python_pkg:json"]["available"] is True
