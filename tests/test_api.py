"""End-to-end API tests for Phase 3 publishing endpoints."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config import settings
from worker.jobs import get_manager
from worker.models import ClipResult, Job, JobStatus, ProcessingOptions


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def seeded_job():
    """Insert a completed job with one real clip file on disk."""
    manager = get_manager()
    job = Job(input_type="file", source="seed.mp4", options=ProcessingOptions())
    clip = ClipResult(
        id="clipA", filename="clipA.mp4", start=0.0, end=12.0, duration=12.0,
        title="Amazing moment", description="The description",
        hashtags=["#one", "#two"], hook_text="Hook!", cta="Subscribe",
        mentions=["@handle"], thumbnail_text="WOW", score=91.0,
    )
    job.clips = [clip]
    job.status = JobStatus.COMPLETED
    manager.store.add(job)

    clip_dir = Path(settings.clips_dir) / job.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / clip.filename).write_bytes(b"FAKEVIDEODATA")
    return job, clip


def test_publisher_statuses(client):
    resp = client.get("/api/publishers")
    assert resp.status_code == 200
    platforms = resp.json()["platforms"]
    assert set(platforms) == {"whop", "youtube", "tiktok", "instagram", "x"}
    for status in platforms.values():
        assert "configured" in status
        assert "message" in status


def test_campaign_create_and_list(client):
    routes = {"youtube": {"account_id": "chanX", "target_type": "", "target_id": ""}}
    resp = client.post("/api/campaigns", json={"name": "My Campaign", "routes": routes})
    assert resp.status_code == 200
    campaign_id = resp.json()["id"]
    assert campaign_id

    listing = client.get("/api/campaigns").json()["campaigns"]
    assert any(c["id"] == campaign_id for c in listing)


def test_campaign_requires_routes(client):
    resp = client.post("/api/campaigns", json={"name": "Empty", "routes": {}})
    assert resp.status_code == 400


def test_download_returns_zip_with_metadata(client, seeded_job):
    job, clip = seeded_job
    resp = client.get(f"/api/clips/{job.id}/{clip.filename}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    names = archive.namelist()
    assert "clipA.mp4" in names
    assert "clipA_metadata.txt" in names

    metadata = archive.read("clipA_metadata.txt").decode()
    assert "Amazing moment" in metadata
    assert "#one #two" in metadata
    assert "Subscribe" in metadata


def test_video_only_download(client, seeded_job):
    job, clip = seeded_job
    resp = client.get(f"/api/clips/{job.id}/{clip.filename}/video")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == b"FAKEVIDEODATA"


def test_publish_clip_creates_attempts(client, seeded_job):
    # No platform credentials are set, so the background scheduler fails the
    # attempt fast (not configured) without any network call — we only assert
    # that the attempt was recorded and is visible in history.
    job, clip = seeded_job

    resp = client.post(
        f"/api/jobs/{job.id}/clips/{clip.id}/publish",
        json={"platforms": ["youtube"], "mode": "review",
              "routes": {"youtube": {"account_id": "chan1"}}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attempt_ids"]) == 1
    assert body["attempts"][0]["platform"] == "youtube"

    history = client.get("/api/history").json()
    assert any(a["platform"] == "youtube" for a in history["publish_attempts"])


def test_publish_unknown_job_404(client):
    resp = client.post("/api/jobs/nope/clips/none/publish",
                       json={"platforms": ["youtube"], "mode": "review"})
    assert resp.status_code == 404


def test_info_reports_platforms(client):
    resp = client.get("/api/info")
    assert resp.status_code == 200
    assert "platforms" in resp.json()



# ---------------------------------------------------------------------------
# Tier 1 — Creator Output Upgrade: /api/info superset + option passthrough
# ---------------------------------------------------------------------------
def test_info_advertises_tier1_option_lists(client):
    """`/api/info` advertises the new Tier 1 preset + sourcing-mode lists."""
    effects = client.get("/api/info").json()["effects"]

    # Caption presets include the legacy templates plus new animated presets.
    assert "caption_presets" in effects
    for name in ("karaoke", "pop", "typewriter", "hormozi", "boxed", "minimal"):
        assert name in effects["caption_presets"]

    # Asset sourcing modes are advertised exactly (Req 8.7).
    assert effects["asset_sourcing_modes"] == ["off", "local_only", "local_then_external"]

    # B-roll intensities and caption animations are advertised.
    assert effects["broll_intensities"] == ["off", "subtle", "standard", "heavy"]
    assert effects["caption_animations"] == ["none", "pop", "typewriter", "karaoke_fill"]

    # broll_providers is present (empty when none configured).
    assert isinstance(effects["broll_providers"], list)


def test_info_retains_existing_effects_keys(client):
    """New lists are additive — all pre-existing effects keys remain (Req 22.3)."""
    body = client.get("/api/info").json()
    effects = body["effects"]

    # Every pre-existing effects key is still present (superset guarantee).
    for key in (
        "music_moods",
        "color_presets",
        "emoji_intensities",
        "emoji_modes",
        "caption_templates",
        "caption_positions",
    ):
        assert key in effects, f"pre-existing effects key missing: {key}"

    # Pre-existing values are unchanged.
    assert effects["caption_templates"] == ["karaoke", "boxed", "minimal"]
    # C13 expanded this from three positions to nine. The guard's substance is that the
    # pre-existing values are still offered and still mean the same thing, not that the list is
    # frozen at three - a client that only knows the original names is unaffected, which is what
    # this now asserts. Updated deliberately.
    assert set(["bottom", "center", "top"]) <= set(effects["caption_positions"])
    assert effects["caption_positions"][0] == "bottom"

    # New top-level broll_available flag is exposed as a bool.
    assert isinstance(body["broll_available"], bool)


def test_options_model_threads_new_fields_into_from_dict():
    """OptionsModel -> to_options carries the new Tier 1 fields into ProcessingOptions."""
    from api.main import OptionsModel

    opts = OptionsModel(
        caption_preset="pop",
        broll=True,
        broll_intensity="subtle",
        asset_sourcing_mode="local_only",
        visual_selection=True,
        selection_prompt="find X",
        caption_keyword_highlight=True,
        permissibility_mode=True,
    ).to_options()

    assert opts.caption_preset == "pop"
    assert opts.broll is True
    assert opts.broll_intensity == "subtle"
    assert opts.asset_sourcing_mode == "local_only"
    assert opts.visual_selection is True
    assert opts.selection_prompt == "find X"
    assert opts.caption_keyword_highlight is True
    assert opts.permissibility_mode is True


def test_url_job_carries_new_fields_through(client):
    """A URL job submitted with new option fields reflects them on the stored job."""
    resp = client.post(
        "/api/jobs/url",
        json={
            "url": "https://example.com/video",
            "options": {
                "caption_preset": "pop",
                "broll": True,
                "visual_selection": True,
                "selection_prompt": "find X",
            },
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    assert job.options.caption_preset == "pop"
    assert job.options.broll is True
    assert job.options.visual_selection is True
    assert job.options.selection_prompt == "find X"



# ---------------------------------------------------------------------------
# v0.8.0 — Speaker Diarisation & Multi-Speaker Reframe:
#          /api/info superset + upload option passthrough
# ---------------------------------------------------------------------------
def test_info_advertises_reframe_option_lists(client):
    """`/api/info` advertises the new reframe layout + intensity lists in
    addition to the pre-existing effects lists (superset guarantee)."""
    effects = client.get("/api/info").json()["effects"]

    # New v0.8.0 lists (Reqs 7.4, 10.6, 17.5, 18.1).
    assert effects["reframe_layouts"] == ["follow_active", "split_screen"]
    assert effects["reframe_intensities"] == ["subtle", "standard", "heavy"]

    # Additive — pre-existing effects keys/values remain present (superset).
    assert "caption_presets" in effects
    assert effects["caption_templates"] == ["karaoke", "boxed", "minimal"]


def test_upload_threads_reframe_fields_into_from_dict(client):
    """The new v0.8.0 upload Form fields reach ProcessingOptions.from_dict and
    land on the stored job's options (interception via the job store, mirroring
    the Tier 1 passthrough test)."""
    resp = client.post(
        "/api/upload",
        files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")},
        data={
            "speaker_reframe": "true",
            "diarization": "true",
            "reframe_layout": "split_screen",
            "reframe_intensity": "heavy",
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    assert job.options.speaker_reframe is True
    assert job.options.diarization is True
    assert job.options.reframe_layout == "split_screen"
    assert job.options.reframe_intensity == "heavy"


def test_upload_unknown_reframe_layout_falls_back_to_default(client):
    """An unknown `reframe_layout` submitted via the upload Form falls back to
    the documented default through the API path (Req 18.5)."""
    resp = client.post(
        "/api/upload",
        files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")},
        data={"speaker_reframe": "true", "reframe_layout": "bogus"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    assert job.options.reframe_layout == "follow_active"



# ---------------------------------------------------------------------------
# Advanced AV engines foundation (task 12.3): `/api/info` engine surface and
# junk engine-option tolerance (Reqs 20.1, 20.2, 20.3, 20.5, 20.6).
# ---------------------------------------------------------------------------

#: Every top-level `/api/info` key that existed **before** this spec. Pinned
#: explicitly (not derived) so a future regression that drops one fails here.
PREEXISTING_INFO_KEYS = frozenset(
    {
        "app_name",
        "environment",
        "version",
        "aspect_ratios",
        "clip_lengths",
        "clip_counts",
        "platforms",
        "strategies",
        "regeneratable_fields",
        "llm_available",
        "effects",
        "broll_available",
        "storage_backend",
        "retention_choices",
    }
)


@pytest.fixture
def engine_registry():
    """The **default** engine registry, emptied for the test and then *restored*.

    `/api/info` reads the process-wide default registry via `get_registry()`, so
    a test that needs a registered engine visible to the endpoint must register
    it there rather than into an isolated instance.

    Teardown used to assert the registry was **empty** afterwards, on the
    assumption that nothing populates the default registry unless a test does.
    That assumption no longer holds: `worker/engines/loader.py` is imported at
    module scope by `api.main` (and by `worker.pipeline`), so importing the app
    registers the shipped AV engines — registered but Feature_Flag-off. Emptying
    the registry and walking away would therefore *remove* the production
    registration for the remainder of the process, which is a leak in the other
    direction.

    So the fixture snapshots the registrations it found, empties the registry for
    the duration of the test (the endpoint's no-engine-registered case is still a
    case worth covering), and replays the snapshot verbatim on the way out —
    "leave it exactly as found", whatever that was. The leak assertion keeps the
    same strength, restated against the snapshot instead of against zero.
    """
    from worker.engines.capabilities import reset_report
    from worker.engines.registry import get_registry, reset_registry

    saved = list(get_registry().records())
    saved_ids = [record.engine_id for record in saved]

    reset_registry()
    reset_report()
    try:
        yield get_registry()
    finally:
        reset_registry()
        for record in saved:
            get_registry().register(record.engine, priority=record.priority)
        reset_report()
        assert [record.engine_id for record in get_registry().records()] == saved_ids, (
            "default engine registry leaked out of the test"
        )


def test_info_exposes_engine_keys_and_retains_preexisting_keys(client, engine_registry):
    """Validates: Requirements 20.1, 20.2, 20.6

    `engines` / `capabilities` are additive: with **no engine registered** the list
    is empty and the capability mapping is empty (no probe performed), while every
    pre-existing v0.8.0 top-level key is still present.

    The empty registry is created by the `engine_registry` fixture and is no longer
    the state of a stock install: `api.main` imports `worker/engines/loader.py` at
    module scope, so the shipped AV engines are registered (Feature_Flag-off) as
    soon as the app is imported. This test therefore covers the *no engine
    registered* case — inert additive keys, zero capability probes — not "what a
    fresh install returns". The advertisement of a registered engine is covered by
    `test_info_advertises_registered_engine_flag_and_default_off` below.
    """
    body = client.get("/api/info").json()

    # Additive keys are present and inert with an empty registry.
    assert body["engines"] == []
    assert isinstance(body["capabilities"], dict)
    assert body["capabilities"] == {}

    # Superset guarantee: no pre-existing key was dropped or renamed.
    missing = PREEXISTING_INFO_KEYS - set(body)
    assert not missing, f"pre-existing /api/info keys missing: {sorted(missing)}"

    # The registry really was untouched by serving the endpoint.
    assert len(engine_registry) == 0


def test_info_advertises_registered_engine_flag_and_default_off(client, engine_registry):
    """Validates: Requirements 20.1, 20.3, 20.6

    A registered engine shows up in `/api/info`'s `engines` list with the
    `<engine_id>_enabled` flag name the UI binds its toggle to, and is never
    enabled by default.
    """
    from tests.fakes import FakeEngine
    from worker.engines.base import Engine_Stage

    engine_registry.register(FakeEngine("stem_separation", Engine_Stage.AUDIO, priority=42))

    body = client.get("/api/info").json()
    rows = body["engines"]
    assert len(rows) == 1
    row = rows[0]

    assert row["id"] == "stem_separation"
    assert row["flag"] == "stem_separation_enabled"
    assert row["enabled_by_default"] is False
    assert row["stage"] == "audio"
    assert row["priority"] == 42
    assert row["requires_network"] is False
    # No declared capabilities => nothing missing, engine advertised available.
    assert row["missing"] == []
    assert row["available"] is True
    # The capability mapping stays serialisable (empty: nothing was declared).
    assert body["capabilities"] == {}

    # Pre-existing keys survive the registered-engine case too.
    assert PREEXISTING_INFO_KEYS <= set(body)


def test_upload_with_unrecognised_engine_options_still_creates_job(client):
    """Validates: Requirements 20.5

    Unrecognised engine option values submitted by a newer UI are ignored, not
    rejected: the upload must still create a job (never a 422) and the stored
    options keep their documented defaults.
    """
    resp = client.post(
        "/api/upload",
        files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")},
        data={
            # Flags for engines that no spec has registered yet.
            "stem_separation_enabled": "true",
            "kinetic_typography_enabled": "not-a-bool",
            # Junk engine option payloads.
            "stem_separation_model": "{}",
            "kinetic_typography_intensity": "🎬",
            "engine_unknown_option": "-1e400",
        },
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["jobs"][0]["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    # Unknown keys landed nowhere and the pre-existing defaults are intact.
    assert not hasattr(job.options, "stem_separation_enabled")
    assert job.options.aspect == "9:16"
    assert job.options.captions is True



# ---------------------------------------------------------------------------
# Kinetic typography engine (task 13.6): `/api/info` advertisement and
# `/api/upload` acceptance (Reqs 17.2, 17.3, 17.4, 17.7).
# ---------------------------------------------------------------------------
@pytest.fixture
def kinetic_registry(engine_registry):
    """The default registry holding **exactly** the real kinetic engine.

    Registration is explicit rather than inherited from
    ``worker/engines/loader.py``'s import-time side effect: several tests in the
    suite call ``reset_registry()`` without restoring, and a later import cannot
    re-register (the module is cached and the loader guards on
    ``find(ENGINE_ID) is None``), so whether the production registration is still
    live depends on test ordering. The `engine_registry` fixture empties the
    registry and replays whatever it found on the way out, so registering here is
    both deterministic and leak-free.
    """
    from worker.engines.kinetic import Kinetic_Typography_Engine

    engine_registry.register(Kinetic_Typography_Engine())
    return engine_registry


def test_info_advertises_kinetic_typography_domains(client, kinetic_registry):
    """Validates: Requirements 17.2, 17.3

    `/api/info` advertises the engine row (flag, default-off, availability) plus
    its Kinetic_Style / Reveal_Mode domains under
    `capabilities["kinetic_typography"]`, and every v0.8.0 caption option value is
    still advertised alongside it (additive, never replaced).
    """
    from worker.effects.caption_presets import VALID_ANIMATIONS
    from worker.engines.kinetic import KINETIC_STYLES, REVEAL_MODES

    body = client.get("/api/info").json()

    rows = [row for row in body["engines"] if row["id"] == "kinetic_typography"]
    assert len(rows) == 1, body["engines"]
    row = rows[0]
    assert row["flag"] == "kinetic_typography_enabled"
    assert row["enabled_by_default"] is False
    assert row["stage"] == "compose"
    assert row["requires_network"] is False
    # Availability is host-dependent (it needs ffmpeg's subtitles filter), so
    # assert the pair is self-consistent rather than pinning one outcome.
    assert isinstance(row["available"], bool)
    assert isinstance(row["missing"], list)
    assert row["available"] is (row["missing"] == [])

    # The option domains ride in the generic capabilities block, keyed by the
    # Engine_Id — this is the key the UI reads.
    domains = body["capabilities"]["kinetic_typography"]
    assert domains["styles"] == list(KINETIC_STYLES)
    assert domains["reveal_modes"] == list(REVEAL_MODES)
    assert domains["reveal_modes"] == ["cumulative", "word_by_word"]
    assert len(domains["styles"]) == 7

    # Additive: every v0.8.0 caption preset and animation value is still there.
    effects = body["effects"]
    for name in ("karaoke", "boxed", "minimal", "pop", "typewriter", "hormozi"):
        assert name in effects["caption_presets"]
    assert set(effects["caption_animations"]) == set(VALID_ANIMATIONS)
    assert effects["caption_templates"] == ["karaoke", "boxed", "minimal"]
    # C13 expanded this from three positions to nine. The guard's substance is that the
    # pre-existing values are still offered and still mean the same thing, not that the list is
    # frozen at three - a client that only knows the original names is unaffected, which is what
    # this now asserts. Updated deliberately.
    assert set(["bottom", "center", "top"]) <= set(effects["caption_positions"])
    assert effects["caption_positions"][0] == "bottom"
    assert PREEXISTING_INFO_KEYS <= set(body)


def test_upload_accepts_every_kinetic_field(client):
    """Validates: Requirements 17.4

    Every kinetic Form field is accepted by `/api/upload` and reaches the stored
    job's options.
    """
    resp = client.post(
        "/api/upload",
        files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")},
        data={
            "kinetic_typography_enabled": "true",
            "kinetic_style": "bounce",
            "kinetic_reveal": "word_by_word",
            "kinetic_font": "Impact",
            "kinetic_max_lines": "3",
            "kinetic_max_line_width": "30",
            "kinetic_safe_area_x_pct": "8",
            "kinetic_safe_area_y_pct": "12",
            "kinetic_motion_ms": "250",
            "kinetic_confidence_floor": "0.4",
        },
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["jobs"][0]["id"]

    job = get_manager().store.get(job_id)
    assert job is not None
    assert job.options.kinetic_typography_enabled is True
    assert job.options.kinetic_style == "bounce"
    assert job.options.kinetic_reveal == "word_by_word"
    assert job.options.kinetic_font == "Impact"
    # Form values arrive as text and are coerced by the engine's resolve_options,
    # so assert the submitted value survived rather than pinning a numeric type.
    assert str(job.options.kinetic_max_lines) == "3"
    assert str(job.options.kinetic_max_line_width) == "30"
    assert str(job.options.kinetic_safe_area_x_pct) == "8"
    assert str(job.options.kinetic_safe_area_y_pct) == "12"
    assert str(job.options.kinetic_motion_ms) == "250"
    assert str(job.options.kinetic_confidence_floor) == "0.4"

    # Omitting every field keeps the documented defaults (Req 17.1).
    plain = client.post(
        "/api/upload", files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")}
    )
    assert plain.status_code == 200, plain.text
    default_job = get_manager().store.get(plain.json()["jobs"][0]["id"])
    assert default_job is not None
    assert default_job.options.kinetic_typography_enabled is False
    assert default_job.options.kinetic_style == "karaoke_fill"
    assert default_job.options.kinetic_reveal == "cumulative"
    assert default_job.options.kinetic_font == ""
    assert default_job.options.kinetic_max_lines == 2
    assert default_job.options.kinetic_max_line_width == 22
    assert default_job.options.kinetic_safe_area_x_pct == 6.0
    assert default_job.options.kinetic_safe_area_y_pct == 10.0
    assert default_job.options.kinetic_motion_ms == 120
    assert default_job.options.kinetic_confidence_floor == 0.0


def test_upload_unrecognised_kinetic_style_still_creates_job(client):
    """Validates: Requirements 17.7

    An unrecognised `kinetic_style` is never rejected: the job is accepted, the
    raw value is stored, and the engine's own resolution applies the documented
    default while recording the substitution.
    """
    resp = client.post(
        "/api/upload",
        files={"files": ("clip.mp4", b"FAKEVIDEODATA", "video/mp4")},
        data={
            "kinetic_typography_enabled": "true",
            "kinetic_style": "wobble",
            "kinetic_reveal": "sideways",
            "kinetic_max_lines": "abc",
        },
    )
    assert resp.status_code == 200, resp.text
    job = get_manager().store.get(resp.json()["jobs"][0]["id"])
    assert job is not None
    assert job.options.kinetic_style == "wobble"

    from worker.engines.kinetic import DEFAULT_REVEAL, DEFAULT_STYLE, Kinetic_Options

    resolved = Kinetic_Options.from_processing_options(job.options)
    assert resolved.style == DEFAULT_STYLE
    assert resolved.reveal == DEFAULT_REVEAL
    assert resolved.max_lines == 2
    assert "style_substituted" in resolved.notes
