"""Built-in profiles (U2) and the labelled synthesised music bed (A15).

Both close the same kind of gap: something the product implied but never actually said.

* U2 — the settings panel exposes thirteen independent toggles and no opinion about which
  combinations make sense together. A user editing a podcast has to know that a two-host
  shot needs speaker-aware reframing, and that a slow zoom on top of it reads as restless.
  A profile is that knowledge, written down.
* A15 — ``resolve_music`` returned a path and nothing recorded which of its two sources it
  came from, so a clip carrying a synthesised two-tone drone was reported as
  ``music:upbeat``: indistinguishable from one with a licensed bed under it. Since
  ``assets/music`` ships empty, in practice it was always the drone.
"""

from __future__ import annotations

import dataclasses

import pytest

from worker.effects import audio
from worker.models import BUILTIN_PROFILES, ProcessingOptions


# --------------------------------------------------------------------------- #
# U2: the bundles are coherent and real                                        #
# --------------------------------------------------------------------------- #
def test_the_four_profiles_the_plan_asks_for_exist():
    assert sorted(BUILTIN_PROFILES) == ["educational", "gaming", "podcast", "talking_head"]
    for name, profile in BUILTIN_PROFILES.items():
        assert profile.name == name, "the key and the profile's own name must agree"
        assert profile.label and profile.description and profile.rationale
        assert profile.settings, f"{name} sets nothing, so picking it would do nothing"


def test_every_profile_setting_is_a_real_option_field():
    """A typo'd key would be silently dropped by ``from_dict`` and do nothing at all.

    That is the failure mode worth guarding: the profile would still appear in the picker,
    still report its bundle over the API, and quietly not apply half of it.
    """
    fields = set(ProcessingOptions.__dataclass_fields__)
    for name, profile in BUILTIN_PROFILES.items():
        unknown = sorted(set(profile.settings) - fields)
        assert not unknown, f"{name} sets non-existent option(s): {unknown}"


def test_profile_settings_survive_from_dict_intact():
    """Every value a profile sets must pass the model's own coercion unchanged.

    ``from_dict`` validates enum-like fields against known value sets and silently falls back
    to the documented default on anything unrecognised. A profile naming, say, a caption
    preset that does not exist would therefore be accepted here and ignored in the render.
    """
    for name, profile in BUILTIN_PROFILES.items():
        options = ProcessingOptions.from_dict({"profile": name})
        for field, expected in profile.settings.items():
            assert getattr(options, field) == expected, (
                f"profile {name!r} sets {field}={expected!r} but the resolved options have "
                f"{getattr(options, field)!r} - the value did not survive validation"
            )


@pytest.mark.parametrize("name", sorted(BUILTIN_PROFILES))
def test_selecting_a_profile_is_recorded_on_the_options(name):
    """A finished job should be able to say which bundle produced it."""
    assert ProcessingOptions.from_dict({"profile": name}).profile == name


def test_an_explicit_request_value_beats_the_profile():
    """The profile is a starting point, not an override.

    This is why the bundle is expanded in ``from_dict``: only there do we still know which
    fields the caller actually sent. Once the options exist, a field holding its default is
    indistinguishable from a field nobody mentioned.
    """
    gaming = BUILTIN_PROFILES["gaming"]
    assert gaming.settings["emoji"] == "heavy"  # the premise of the test

    options = ProcessingOptions.from_dict({"profile": "gaming", "emoji": "subtle"})
    assert options.emoji == "subtle", "the explicit request lost to the profile"
    # And the rest of the bundle still applied.
    assert options.caption_preset == gaming.settings["caption_preset"]
    assert options.profile == "gaming"


def test_an_explicit_false_also_beats_the_profile():
    """A caller turning something *off* is as explicit as turning it on.

    Easy to get wrong: a truthiness check would treat ``False`` as "not supplied" and let the
    profile win, so the one setting the user cared about would be the one ignored.
    """
    assert BUILTIN_PROFILES["gaming"].settings["zoom"] is True
    options = ProcessingOptions.from_dict({"profile": "gaming", "zoom": False})
    assert options.zoom is False


def test_an_unknown_profile_is_ignored_rather_than_recorded():
    """A job must not claim a bundle that does not exist."""
    for value in ("nope", "", None, "PODCAST-ish", 7):
        options = ProcessingOptions.from_dict({"profile": value})
        assert options.profile == "" or options.profile in BUILTIN_PROFILES
        if value not in BUILTIN_PROFILES:
            assert options.profile == ""


def test_profile_names_are_matched_case_insensitively():
    assert ProcessingOptions.from_dict({"profile": "Podcast"}).profile == "podcast"
    assert ProcessingOptions.from_dict({"profile": " GAMING "}).profile == "gaming"


def test_no_profile_leaves_the_global_defaults_alone():
    """Picking nothing must behave exactly as before U2 existed."""
    assert ProcessingOptions.from_dict({}) == ProcessingOptions()
    assert ProcessingOptions().profile == ""


def test_profiles_disagree_with_each_other():
    """Four bundles that resolved to the same thing would be four labels, not four profiles.

    Guards the actual product claim - that picking a profile changes the output - rather than
    just that the plumbing runs.
    """
    resolved = {
        name: dataclasses.replace(ProcessingOptions.from_dict({"profile": name}), profile="")
        for name in BUILTIN_PROFILES
    }
    for first in resolved:
        for second in resolved:
            if first < second:
                assert resolved[first] != resolved[second], f"{first} and {second} are identical"


def test_the_two_destructive_or_owning_features_appear_only_in_profiles():
    """The features U1 deliberately left off the global default, used deliberately here.

    ``filler_removal`` removes content, and ``kinetic_typography_enabled`` hands the caption
    layer to an AV engine. Neither belongs in a default that applies to footage nobody has
    described; both are reasonable once a user says what they are editing. If a future change
    makes them global defaults, this test should be deleted along with that decision - it
    exists to record that the split was intentional.
    """
    assert ProcessingOptions().filler_removal is False
    assert ProcessingOptions().kinetic_typography_enabled is False

    filler = {n for n, p in BUILTIN_PROFILES.items() if p.settings.get("filler_removal")}
    kinetic = {
        n for n, p in BUILTIN_PROFILES.items() if p.settings.get("kinetic_typography_enabled")
    }
    assert filler == {"podcast", "educational"}, "unscripted speech is where this earns its cost"
    assert kinetic == {"gaming"}, "the one audience that asks for animated captions"


def test_single_speaker_profiles_do_not_pay_for_diarisation():
    """Talking-head footage has one speaker, so finding that out costs time for nothing."""
    talking_head = BUILTIN_PROFILES["talking_head"].settings
    assert talking_head["diarization"] is False
    assert talking_head["speaker_reframe"] is False
    # And the multi-speaker profile does the opposite, which is what distinguishes them.
    assert BUILTIN_PROFILES["podcast"].settings["speaker_reframe"] is True


# --------------------------------------------------------------------------- #
# A15: the synthesised bed says what it is                                      #
# --------------------------------------------------------------------------- #
def test_a_user_supplied_track_is_reported_as_a_real_track(tmp_path, monkeypatch):
    monkeypatch.setattr(audio.settings, "music_dir", tmp_path)
    (tmp_path / "upbeat.mp3").write_bytes(b"not really audio, but it exists")

    bed = audio.resolve_music_bed("upbeat", 2.0, tmp_path / "work")
    assert bed is not None
    assert bed.source == audio.SOURCE_USER_TRACK
    assert bed.synthesised is False
    assert bed.path.name == "upbeat.mp3"


def test_the_fallback_bed_is_reported_as_synthesised(tmp_path, monkeypatch):
    """The gap A15 closes: a path alone cannot tell you this is a tone generator."""
    monkeypatch.setattr(audio.settings, "music_dir", tmp_path / "empty")
    monkeypatch.setattr(audio, "synthesize_bed", lambda mood, duration, dest: dest)

    bed = audio.resolve_music_bed("upbeat", 2.0, tmp_path / "work")
    assert bed is not None
    assert bed.source == audio.SOURCE_SYNTHESISED
    assert bed.synthesised is True


def test_synthesis_can_be_refused(tmp_path, monkeypatch):
    """Silence is a legitimate preference over a drone.

    With no user track and synthesis disabled, there is no bed at all - the clip renders
    without music rather than with something the caller would not have chosen.
    """
    monkeypatch.setattr(audio.settings, "music_dir", tmp_path / "empty")
    monkeypatch.setattr(audio.settings, "music_allow_synthesis", False)

    assert audio.resolve_music_bed("upbeat", 2.0, tmp_path / "work") is None


def test_music_dir_ships_empty_so_the_fallback_is_the_normal_case():
    """Records why A15 matters rather than being a nicety.

    ``assets/music`` contains only a ``.gitkeep``, so unless a user has added a track,
    every music-enabled clip gets the synthesised bed. Should real beds ever ship (A14),
    this test failing is the reminder to revisit the ``music`` default in ``U1``.
    """
    import pathlib

    music_dir = pathlib.Path(__file__).resolve().parents[1] / "assets" / "music"
    tracks = [path for path in music_dir.iterdir() if path.suffix.lower() in audio._AUDIO_EXTS]
    assert not tracks, (
        f"real beds have shipped ({[t.name for t in tracks]}); revisit whether music should "
        "still default off in ProcessingOptions"
    )


def test_the_path_only_helper_still_works_for_callers_that_only_need_bytes(tmp_path, monkeypatch):
    """``resolve_music`` is retained, and must agree with the bed it wraps."""
    monkeypatch.setattr(audio.settings, "music_dir", tmp_path / "empty")
    monkeypatch.setattr(audio, "synthesize_bed", lambda mood, duration, dest: dest)

    bed = audio.resolve_music_bed("chill", 1.0, tmp_path / "w")
    path = audio.resolve_music("chill", 1.0, tmp_path / "w")
    assert bed is not None and path == bed.path
    assert audio.resolve_music("", 1.0, tmp_path / "w") is None


# --------------------------------------------------------------------------- #
# U2 over the API                                                              #
# --------------------------------------------------------------------------- #
def test_builtin_profiles_are_discoverable_over_the_api():
    """A client cannot offer a picker for something it cannot enumerate."""
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        listed = client.get("/api/profiles/builtin")
        assert listed.status_code == 200
        payload = listed.json()["profiles"]
        assert [entry["name"] for entry in payload] == list(BUILTIN_PROFILES)

        for entry in payload:
            profile = BUILTIN_PROFILES[entry["name"]]
            # The full bundle, so a client can show what picking it will change.
            assert entry["settings"] == dict(profile.settings)
            assert entry["label"] == profile.label
            assert entry["rationale"] == profile.rationale

        # /api/info carries the names, so a single call is enough to render the control.
        info = client.get("/api/info")
        assert info.status_code == 200
        assert info.json()["builtin_profiles"] == list(BUILTIN_PROFILES)


def test_the_builtin_endpoint_is_distinct_from_user_saved_profiles():
    """Two different things share the word "profile"; the API must not conflate them.

    ``/api/profiles`` lists profiles a *user* saved (mutable, stored as opaque JSON blobs).
    ``/api/profiles/builtin`` lists the shipped read-only bundles. A client that mixed them
    would offer users a delete button for a built-in.
    """
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        user_saved = client.get("/api/profiles")
        assert user_saved.status_code == 200
        # A fresh store has no user profiles, while the built-ins are always present.
        assert "default_id" in user_saved.json()
        assert client.get("/api/profiles/builtin").json()["profiles"]
