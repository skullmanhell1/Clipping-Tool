"""How the whisper model is constructed: CPU parallelism and device resolution.

Two levers that were never pulled.

``WhisperModel`` accepts ``cpu_threads`` and ``num_workers``; neither was ever passed, so
CTranslate2's parallelism was whatever the library guessed and there was no way to correct
it. Both are now settings, and both are part of the model cache key - they are *constructor*
arguments, so without them in the key a second thread count would be accepted and silently
ignored, which is the worst possible outcome for a performance setting.

No test here loads a real model. ``WhisperModel`` is replaced by a recorder, because a test
that downloads a model is a test that fails on an air-gapped runner and takes minutes when
it does not.
"""
from __future__ import annotations

import pytest

from config import settings
from worker import transcribe


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """The model cache is process-wide; a leaked entry would answer the next test."""
    transcribe._model_cache.clear()
    yield
    transcribe._model_cache.clear()


@pytest.fixture()
def recorded_models(monkeypatch):
    """Replace ``WhisperModel`` with a recorder and return the list of calls."""
    import faster_whisper

    calls: list[tuple[tuple, dict]] = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisperModel)
    # ``_resolve_device`` consults the host; pin it so these tests assert argument
    # plumbing rather than what hardware the runner happens to have.
    monkeypatch.setattr(settings, "whisper_device", "cpu")
    monkeypatch.setattr(settings, "whisper_compute_type", "int8")
    return calls


# --------------------------------------------------------------------------- #
# cpu_threads / num_workers reach the constructor                               #
# --------------------------------------------------------------------------- #
def test_the_default_passes_no_thread_count_at_all(monkeypatch, recorded_models):
    """``0`` must construct the model as it was constructed before this setting existed.

    ``0`` and absence mean the same thing to CTranslate2, so this is not a behavioural
    assertion - it is the guarantee that an install which sets nothing is byte-identical to
    0.11.0, asserted rather than assumed.
    """
    monkeypatch.setattr(settings, "whisper_cpu_threads", 0)
    monkeypatch.setattr(settings, "whisper_num_workers", 1)

    transcribe._get_model()

    (_args, kwargs), = recorded_models
    assert "cpu_threads" not in kwargs
    # num_workers is always passed because its default *is* 1, so the value and its
    # absence are indistinguishable to the library.
    assert kwargs == {"device": "cpu", "compute_type": "int8", "num_workers": 1}


def test_a_configured_thread_count_is_passed_through(monkeypatch, recorded_models):
    monkeypatch.setattr(settings, "whisper_cpu_threads", 6)
    monkeypatch.setattr(settings, "whisper_num_workers", 3)

    transcribe._get_model()

    (_args, kwargs), = recorded_models
    assert kwargs["cpu_threads"] == 6
    assert kwargs["num_workers"] == 3


def test_a_negative_thread_count_is_treated_as_unset(monkeypatch, recorded_models):
    """Nonsense must degrade to the library default rather than reach CTranslate2.

    ``cpu_threads=-1`` is not a documented value and there is no useful behaviour to give
    it, so it takes the same path as ``0``.
    """
    monkeypatch.setattr(settings, "whisper_cpu_threads", -1)

    transcribe._get_model()

    (_args, kwargs), = recorded_models
    assert "cpu_threads" not in kwargs


def test_model_kwargs_and_the_construction_cannot_diverge(monkeypatch, recorded_models):
    """The published argument set is the one actually used.

    ``model_kwargs`` exists so the arguments can be asserted without a model. That is only
    worth anything if it is the same dictionary the constructor receives - two hand-kept
    copies is the "one fact in two places" failure this repository has already shipped twice.
    """
    monkeypatch.setattr(settings, "whisper_cpu_threads", 5)
    monkeypatch.setattr(settings, "whisper_num_workers", 2)

    expected = transcribe.model_kwargs()
    transcribe._get_model()

    (_args, kwargs), = recorded_models
    assert kwargs == expected


# --------------------------------------------------------------------------- #
# The model cache key                                                           #
# --------------------------------------------------------------------------- #
def test_two_thread_counts_do_not_share_one_cached_model(monkeypatch, recorded_models):
    """Otherwise the second setting is accepted and does nothing, forever.

    The cache is process-wide and keyed on the model name, so before this the first
    transcription of a process fixed the thread count for every later one. Changing the
    setting would appear to work - no error, a transcript comes back - and have no effect.
    """
    monkeypatch.setattr(settings, "whisper_cpu_threads", 4)
    first = transcribe._get_model()

    monkeypatch.setattr(settings, "whisper_cpu_threads", 8)
    second = transcribe._get_model()

    assert first is not second
    assert len(recorded_models) == 2
    assert recorded_models[0][1]["cpu_threads"] == 4
    assert recorded_models[1][1]["cpu_threads"] == 8


def test_two_worker_counts_do_not_share_one_cached_model(monkeypatch, recorded_models):
    monkeypatch.setattr(settings, "whisper_num_workers", 1)
    first = transcribe._get_model()

    monkeypatch.setattr(settings, "whisper_num_workers", 4)
    second = transcribe._get_model()

    assert first is not second
    assert len(recorded_models) == 2


def test_the_same_settings_are_served_from_the_cache(monkeypatch, recorded_models):
    """The cache must still cache: loading is expensive relative to inference."""
    monkeypatch.setattr(settings, "whisper_cpu_threads", 4)

    assert transcribe._get_model() is transcribe._get_model()
    assert len(recorded_models) == 1


def test_the_cache_key_covers_every_constructor_argument():
    """A drift pin: an argument added to ``model_kwargs`` must be added to the key too.

    The failure mode this guards is silent - a new constructor setting that shares a cached
    model with its own previous value does nothing at all, and nothing raises.
    """
    import inspect

    source = inspect.getsource(transcribe._get_model)
    for name in ("cpu_threads", "num_workers", "device", "compute_type"):
        assert name in source, f"{name} is constructed but not part of the cache key"
