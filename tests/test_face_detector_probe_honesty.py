"""`/api/info` must not advertise a detector the render path cannot build.

The probe behind `face_detectors` used to do `import mediapipe` plus a model-size check. Both pass
in an image that cannot construct a graph: `libmediapipe.so` dlopen's libEGL/libGLESv2 only when a
detector is created, so the import succeeds and `create_from_options` raises
`OSError: libGLESv2.so.2`.

The consequence was not a cosmetic wrong field. `scripts/docker_smoke.sh` asserts on exactly this
value, so the smoke test certified an image in which **10 of 10 clips** silently fell back to Haar,
each carrying `face_detector_substituted:mediapipe:haar`. The Dockerfile was missing the two
libraries `.github/workflows/ci.yml` installs and explains at length -- so CI exercised the real
detector, the shipped container never did, and nothing could tell the difference, because the only
thing that reveals it is footage with a face in it.

These tests pin the property that matters: the answer comes from *constructing* a detector, so the
probe cannot drift back to import-only without failing here.
"""

from __future__ import annotations

import api.main as main


def _entry(domains, name):
    return next(d for d in domains if d["name"] == name)


def test_both_backends_are_advertised():
    """The values are part of the contract, available or not."""
    names = [d["name"] for d in main._face_detector_domains()]
    assert names == ["haar", "mediapipe"]


def test_mediapipe_reports_unavailable_when_a_detector_cannot_be_built(monkeypatch):
    """The exact defect: model present, import fine, construction impossible."""
    monkeypatch.setattr("worker.effects.reframe._mediapipe_detector", lambda *_a, **_kw: None)

    entry = _entry(main._face_detector_domains(), "mediapipe")

    assert entry["available"] is False
    assert "construct" in entry.get("detail", ""), (
        f"the reason must name construction, got {entry.get('detail')!r}"
    )


def test_mediapipe_reports_available_when_one_can_be_built(monkeypatch):
    """The parity case, so the test above cannot pass by the probe always saying False."""
    closed: list[bool] = []
    monkeypatch.setattr(
        "worker.effects.reframe._mediapipe_detector",
        lambda *_a, **_kw: (lambda _frame: [], lambda: closed.append(True)),
    )

    entry = _entry(main._face_detector_domains(), "mediapipe")

    assert entry["available"] is True
    assert "detail" not in entry
    assert closed, "the probe built a detector and never closed it, leaking a task graph per call"


def test_a_missing_model_is_still_reported_as_such(monkeypatch):
    """The pre-existing check must survive: a missing model is a different fault from a broken one."""
    monkeypatch.setattr("worker.face_models.resolve_model", lambda _b: None)

    entry = _entry(main._face_detector_domains(), "mediapipe")

    assert entry["available"] is False
    assert "model" in entry.get("detail", "")


def test_a_probe_failure_never_takes_the_endpoint_down(monkeypatch):
    """`/api/info` must answer even when a probe explodes; it is a status endpoint."""

    def _boom(*_a, **_kw):
        raise RuntimeError("something in the vision stack fell over")

    monkeypatch.setattr("worker.effects.reframe._mediapipe_detector", _boom)

    domains = main._face_detector_domains()

    entry = _entry(domains, "mediapipe")
    assert entry["available"] is False
    assert entry.get("detail")
    # haar is independent and must still be reported.
    assert _entry(domains, "haar")["name"] == "haar"


def test_the_close_callback_failing_does_not_fail_the_probe(monkeypatch):
    """Cleanup is best-effort: a detector that built is available even if closing complains."""

    def _built(*_a, **_kw):
        def _close():
            raise RuntimeError("close failed")

        return (lambda _frame: []), _close

    monkeypatch.setattr("worker.effects.reframe._mediapipe_detector", _built)

    assert _entry(main._face_detector_domains(), "mediapipe")["available"] is True
