"""Tests for the asset-library items: A5, A9, A13, A17, A19, A22.

These six share a failure mode: an asset feature that does nothing still produces a rendered
clip. A keyword map that never matches, a style setting that is ignored, a music library where
every clip gets track one, a tag manifest that is never read, a "Ken Burns" that does not move -
each of them passes any test that only checks the render succeeded. So each test below is written
to fail if the feature were inert:

* A9 asserts the map is large *and* that no key is a caption stopword, which is the failure that
  makes a bigger map worse rather than better.
* A13 asserts the three styles produce three *different* URLs, and that an unknown one degrades to
  the shipped look rather than to nothing.
* A5 asserts the family name comes from the font file, not the filename - offering a name libass
  cannot resolve is the C1 defect this repository has already shipped once.
* A17 asserts two clips get *different* tracks and that one clip gets the *same* track twice.
* A19 asserts `on.mp4` no longer answers the keyword "money", which is true of the substring test
  it replaces.
* A22 asserts the zoom expression scales with the frame rate rather than accumulating, and that
  overlapping duck windows do not compound to silence.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from config import settings
from worker import captions as cap
from worker.effects import audio, broll
from worker.effects import emoji as em
from worker.effects.caption_presets import DEFAULT_STOPWORDS

requires_ffmpeg = pytest.mark.skipif(
    subprocess.run(["which", settings.ffmpeg_binary], capture_output=True).returncode != 0,
    reason="ffmpeg not on PATH",
)
FFMPEG = settings.ffmpeg_binary


# --------------------------------------------------------------------------- #
# A9 - the expanded keyword map
# --------------------------------------------------------------------------- #


def test_a9_the_map_is_large_enough_to_fire_on_ordinary_speech():
    """The plan's target is 500+ keywords, and the number is not arbitrary.

    `standard` intensity allows one emoji every five seconds, so a 60-second clip needs a dozen
    mapped words spread across it to fill even half its slots. At 85 keywords the overlay was
    decorative on the few clips that happened to say "money" or "fire".
    """
    assert len(em.KEYWORD_EMOJI) >= 500
    # Synonyms share a glyph, so the glyph count - and with it the vendored asset weight - grows
    # far more slowly than the vocabulary.
    assert len(set(em.KEYWORD_EMOJI.values())) < len(em.KEYWORD_EMOJI) / 2


def test_a9_no_keyword_is_a_caption_stopword_except_the_documented_four():
    """The failure that makes a bigger map *worse*.

    A11 scores stopwords at zero, so a mapped stopword only ever wins a slot when nothing better
    is in the clip - which is exactly the wrong moment for a weak match, because that clip has no
    other emoji to distract from it. "Like" is 👍 in one sense and a filler in most others.

    Enforced here rather than by care, because the failure mode of adding one is a
    plausible-looking overlay on the word "just".
    """
    collisions = {key for key in em.KEYWORD_EMOJI if key in DEFAULT_STOPWORDS}
    assert collisions == set(em.STOPWORD_KEYS_KEPT), (
        "a new keyword collides with the caption stopword list; either it belongs in "
        "STOPWORD_KEYS_KEPT with the same argument the other four have, or it should not be "
        "in the map"
    )


def test_a9_the_map_has_no_duplicate_keys():
    """A duplicate key in a dict literal is not an error - the later one silently wins.

    So a well-meant addition can remove an existing mapping with no diff that looks wrong. Checked
    against the source rather than the loaded dict, because the loaded dict cannot show it.
    """
    import ast
    import collections
    from pathlib import Path

    tree = ast.parse(Path(em.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "KEYWORD_EMOJI":
            keys = [k.value for k in node.value.keys]
            duplicates = [k for k, n in collections.Counter(keys).items() if n > 1]
            assert not duplicates, f"duplicate keyword(s): {duplicates}"
            return
    pytest.fail("KEYWORD_EMOJI literal not found")


def test_a9_every_glyph_the_map_can_emit_is_vendored():
    """The overlay must never need the network at render time (A7)."""
    from pathlib import Path

    assets = Path(settings.emoji_assets_dir)
    missing = [
        glyph for glyph in set(em.KEYWORD_EMOJI.values())
        if not (assets / em.emoji_filename(glyph)).is_file()
    ]
    assert not missing, f"{len(missing)} glyph(s) not vendored: {missing[:10]}"


def test_a9_inflected_speech_still_reaches_the_new_keywords():
    """A10's rules have to cover the additions, not just the original 85."""
    for spoken, expected_key in (
        ("investing", "invest"), ("negotiated", "negotiate"), ("celebrating", "celebrate"),
        ("collapsed", "collapse"), ("hospitals", "hospital"), ("strategies", "strategy"),
    ):
        assert em.lookup_emoji(spoken, em.KEYWORD_EMOJI) == em.KEYWORD_EMOJI[expected_key], spoken


def test_a9_homographs_are_absent_rather_than_guessed():
    """An emoji illustrating the wrong sense reads as a machine that misread the sentence.

    Each of these has two common senses with different pictures, so including it would raise the
    keyword count and lower the hit quality - the same trade C14 refused for caption presets.
    """
    for homograph in ("bank", "spring", "mine", "current", "wave", "rock", "bug", "beat",
                      "marker", "notice", "ring", "second"):
        assert homograph not in em.KEYWORD_EMOJI, homograph


# --------------------------------------------------------------------------- #
# A13 - selectable emoji styles
# --------------------------------------------------------------------------- #


def test_a13_the_three_styles_resolve_to_three_different_sources():
    """Three things differ per set, and each produces a 404 rather than a readable error.

    The base URL, the case of the hex, and the prefix plus separator. Encoding all three is what
    stops "switch to OpenMoji" from meaning "silently render no emoji".
    """
    urls = {name: em.EMOJI_STYLES[name].remote_url("\U0001f525") for name in em.EMOJI_STYLES}
    assert len(set(urls.values())) == 3, urls
    assert urls["noto"].endswith("emoji_u1f525.png")
    assert urls["twemoji"].endswith("1f525.png")
    # OpenMoji upper-cases its filenames; asking for the lower-case one is a 404.
    assert urls["openmoji"].endswith("1F525.png")


def test_a13_multi_codepoint_sequences_keep_each_style_s_own_spelling():
    """A single-codepoint test passes for a style whose separator is wrong."""
    teacher = "\U0001f9d1\u200d\U0001f3eb"
    assert em.EMOJI_STYLES["noto"].remote_filename(teacher) == "emoji_u1f9d1_200d_1f3eb.png"
    assert em.EMOJI_STYLES["twemoji"].remote_filename(teacher) == "1f9d1-200d-1f3eb.png"
    assert em.EMOJI_STYLES["openmoji"].remote_filename(teacher) == "1F9D1-200D-1F3EB.png"


def test_a13_an_unknown_style_resolves_to_the_shipped_look_not_to_a_failure():
    """A typo in a setting should not fail a job whose transcription has already been paid for."""
    for value in ("nonsense", "", None, "NOTO  "):
        assert em.resolve_style(value).name == em.DEFAULT_STYLE


def test_a13_the_default_style_keeps_the_committed_assets_directory():
    """7 MB of vendored artwork lives there; moving it would orphan all of it."""
    from pathlib import Path

    assert em.style_assets_dir(em.resolve_style("noto")) == Path(settings.emoji_assets_dir)
    others = {
        em.style_assets_dir(em.EMOJI_STYLES[name]) for name in ("twemoji", "openmoji")
    }
    assert len(others) == 2
    assert Path(settings.emoji_assets_dir) not in others


def test_a13_a_missing_glyph_in_a_selected_style_falls_back_to_the_vendored_one(monkeypatch):
    """Otherwise selecting a style you never vendored switches the *feature* off.

    A glyph in the wrong artwork set is a cosmetic inconsistency that is visible and correctable.
    A missing overlay looks like the emoji feature is broken.
    """
    monkeypatch.setattr(settings, "emoji_style", "openmoji", raising=False)
    resolved = em.resolve_asset("\U0001f525", downloader=lambda _u, _d: False)
    assert resolved is not None
    assert resolved.parent.name == "emoji", resolved


def test_a13_the_default_style_falls_back_to_nothing_because_it_is_the_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "emoji_style", "noto", raising=False)
    monkeypatch.setattr(settings, "emoji_assets_dir", tmp_path / "empty", raising=False)
    assert em.resolve_asset("\U0001f525", downloader=lambda _u, _d: False) is None


def test_a13_a_failed_download_does_not_leave_a_zero_byte_file_behind(monkeypatch, tmp_path):
    """A truncated write would be returned as a usable asset on the next call.

    Which turns one network blip into a permanently blank overlay for that glyph.
    """
    monkeypatch.setattr(settings, "emoji_style", "noto", raising=False)
    monkeypatch.setattr(settings, "emoji_assets_dir", tmp_path / "cache", raising=False)

    def half_write(_url, dest):
        dest.write_bytes(b"")
        return True

    assert em.resolve_asset("\U0001f525", downloader=half_write) is None
    assert not (tmp_path / "cache" / em.emoji_filename("\U0001f525")).exists()


def test_a13_a_style_asks_for_the_configured_mirror_only_for_the_default(monkeypatch, tmp_path):
    """An operator who pointed EMOJI_CDN_BASE at a mirror keeps that mirror for Noto.

    But a mirror of Noto cannot serve OpenMoji, so a non-default style must use its own base.
    """
    monkeypatch.setattr(settings, "emoji_assets_dir", tmp_path / "c", raising=False)
    monkeypatch.setattr(settings, "emoji_cdn_base", "https://mirror.example/emoji", raising=False)
    seen: list[str] = []

    def record(url, _dest):
        seen.append(url)
        return False

    em.resolve_asset("\U0001f525", downloader=record, style="noto")
    em.resolve_asset("\U0001f525", downloader=record, style="openmoji")
    assert seen[0].startswith("https://mirror.example/emoji")
    assert "hfg-gmuend" in seen[1]


# --------------------------------------------------------------------------- #
# A5 - operator-supplied fonts
# --------------------------------------------------------------------------- #


def test_a5_the_family_name_comes_from_the_font_file_not_the_filename(tmp_path, monkeypatch):
    """The C1 defect, which this repository has already shipped once.

    libass selects a face by the family name in its ``name`` table and answers an unknown family
    by silently substituting another. A picker that offered ``MyBrandFont.ttf`` as "MyBrandFont"
    would be offering a name that resolves to nothing.
    """
    import shutil

    source = cap.FONT_MANIFEST.parent / "fonts" / "Anton-Regular.ttf"
    directory = tmp_path / "fonts"
    directory.mkdir()
    shutil.copy(source, directory / "totally-different-name.ttf")
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)

    found = cap.discovered_fonts()
    assert [font["name"] for font in found] == ["Anton"]
    assert found[0]["family"] == "Anton"


def test_a5_a_font_whose_name_cannot_be_read_is_not_offered(tmp_path, monkeypatch):
    """Offering fewer faces beats offering one that renders as something else."""
    directory = tmp_path / "fonts"
    directory.mkdir()
    (directory / "broken.ttf").write_bytes(b"not a font at all")
    (directory / "notes.txt").write_text("ignored")
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)
    assert cap.discovered_fonts() == []


def test_a5_an_unreadable_font_does_not_leak_a_file_descriptor(tmp_path, monkeypatch):
    """``with TTFont(path)`` leaks one descriptor per unreadable file.

    ``TTFont`` takes ownership of the file only once construction *succeeds*, so a raise part-way
    through leaves the handle open and the ``with`` block never gets an object to close. A
    long-running server whose font directory contains one bad file leaks a descriptor on every
    request, and the symptom arrives much later as ``EMFILE`` somewhere unrelated.

    Counted from ``/proc/self/fd`` rather than trusted to ``ResourceWarning``: that warning only
    fires when the object happens to be collected, so it catches this intermittently.
    """
    from pathlib import Path

    directory = tmp_path / "fonts"
    directory.mkdir()
    for index in range(12):
        (directory / f"broken{index}.ttf").write_bytes(b"definitely not a font")
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)

    def open_font_descriptors() -> list[str]:
        """Descriptors pointing at the fixture's own font files.

        Counted by *target* rather than by total descriptor count: the total moves for reasons
        that have nothing to do with this code - pytest's own capture handles, another test's
        sockets - which makes a total-count assertion flaky under random test ordering while
        proving nothing extra.
        """
        targets: list[str] = []
        for entry in Path("/proc/self/fd").iterdir():
            try:
                target = str(entry.resolve())
            except OSError:
                continue
            if target.startswith(str(directory)):
                targets.append(target)
        return targets

    cap.discovered_fonts()          # warm any lazy imports
    for _ in range(3):
        cap.discovered_fonts()
    # 48 scans of a file that cannot be parsed; a leak of one descriptor each would be unmistakable.
    leaked = open_font_descriptors()
    assert leaked == [], f"{len(leaked)} font descriptor(s) still open: {leaked[:3]}"


def test_a5_a_variable_font_is_excluded_for_the_same_reason_the_manifest_excludes_them(
    tmp_path, monkeypatch
):
    """``fontsdir`` cannot select a named instance of a variable font, so it substitutes."""
    import shutil

    directory = tmp_path / "fonts"
    directory.mkdir()
    shutil.copy(
        cap.FONT_MANIFEST.parent / "fonts" / "Montserrat[wght].ttf", directory / "var.ttf"
    )
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)
    assert cap.discovered_fonts() == []


def test_a5_a_non_regular_style_is_named_so_libass_can_select_it(tmp_path, monkeypatch):
    """libass matches the *full* name for a non-regular style.

    A bold-only file offered as "Poppins" lands on a synthesised bold of whatever Poppins face
    fontconfig finds first, which is the substitution this avoids.
    """
    import shutil

    directory = tmp_path / "fonts"
    directory.mkdir()
    shutil.copy(cap.FONT_MANIFEST.parent / "fonts" / "Poppins-Black.ttf", directory / "p.ttf")
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)
    found = cap.discovered_fonts()
    assert [font["name"] for font in found] == ["Poppins Black"]
    assert found[0]["family"] == "Poppins"
    assert found[0]["style"] == "Black"


def test_a5_weight_is_reported_on_the_same_scale_the_manifest_uses():
    """Two scales for one concept, and mixing them is silent rather than an error.

    A font file says 700 for bold; fontconfig - which is what ``assets/fonts.json`` records and
    what libass prints - says 200. Emitting the file's number in the same ``weight`` field would
    make a user-supplied regular face (400) look nearly twice as heavy as a vendored black one.
    """
    assert cap._fc_weight(400) == 80        # regular
    assert cap._fc_weight(700) == 200       # bold
    assert cap._fc_weight(800) == 205       # extra bold
    assert cap._fc_weight(900) == 210       # black
    assert cap._fc_weight(0) == 0

    manifest = json.loads(cap.FONT_MANIFEST.read_text(encoding="utf-8"))
    declared = {entry["name"]: entry["weight"] for entry in manifest["fonts"]}
    for font in cap.available_fonts():
        if font["source"] == "bundled":
            assert font["weight"] == declared[font["name"]], font["name"]


def _rename_font(source, dest, family: str) -> None:
    """Copy ``source`` to ``dest`` with its recorded family changed to ``family``.

    A real fixture rather than a stub: the point of A5 is that the *file* decides the name, so a
    test that copies a vendored font under a new filename would prove nothing - the family it
    reports would collide with the manifest entry and be deduplicated away.
    """
    from fontTools.ttLib import TTFont

    with open(source, "rb") as handle:
        font = TTFont(handle)
        for record in font["name"].names:
            if record.nameID in (1, 4, 16):
                record.string = family
            elif record.nameID == 6:
                record.string = family.replace(" ", "")
        font.save(str(dest))
        font.close()


def test_a5_a_dropped_in_font_appears_in_the_picker(tmp_path, monkeypatch):
    directory = tmp_path / "fonts"
    directory.mkdir()
    _rename_font(
        cap.FONT_MANIFEST.parent / "fonts" / "Bangers-Regular.ttf",
        directory / "whatever.ttf",
        "Acme Brand Display",
    )
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)
    cap._FONT_DIR_STATE.clear()
    names = {font["name"]: font["source"] for font in cap.available_fonts()}
    assert names.get("Acme Brand Display") == "user", sorted(names)


def test_a5_a_dropped_in_font_cannot_displace_a_vendored_one_of_the_same_name(
    tmp_path, monkeypatch
):
    """The manifest carries a verified licence and a CI check that the file resolves to itself.

    A file dropped in under a vendored family's name must not quietly replace that guarantee,
    which is why the collision resolves towards the manifest rather than towards the newer file.
    """
    import shutil

    directory = tmp_path / "fonts"
    directory.mkdir()
    shutil.copy(cap.FONT_MANIFEST.parent / "fonts" / "Bangers-Regular.ttf", directory / "b.ttf")
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)
    cap._FONT_DIR_STATE.clear()
    bangers = [font for font in cap.available_fonts() if font["name"] == "Bangers"]
    assert len(bangers) == 1
    assert bangers[0]["source"] == "bundled"
    assert bangers[0]["license"] == "OFL-1.1"


def test_a5_a_bundled_face_keeps_its_verified_licence_on_a_name_collision():
    """The manifest carries a verified SPDX id and a CI check that the file resolves to itself.

    A dropped-in file with the same family name must not quietly replace that guarantee with a
    blank licence.
    """
    anton = [f for f in cap.available_fonts() if f["name"] == "Anton"]
    assert len(anton) == 1
    assert anton[0]["source"] == "bundled"
    assert anton[0]["license"] == "OFL-1.1"


def test_a5_a_user_font_claims_no_licence_it_cannot_check():
    """The operator supplied the file, so its licence is theirs to know."""
    import shutil
    from pathlib import Path
    directory = Path(settings.font_assets_dir)
    assert directory.is_dir()
    del shutil       # not used; the assertion below is about the shape of a user entry
    for font in cap.discovered_fonts():
        assert font["license"] == ""
        assert font["use"] == "user-supplied"


def test_a5_refreshing_the_cache_is_gated_on_the_directory_changing(tmp_path, monkeypatch):
    """`available_fonts` is called on every page load; fc-cache is a subprocess.

    Ungated, that is one process spawn per request for an answer that only changes when a file
    is added.
    """
    directory = tmp_path / "fonts"
    directory.mkdir()
    monkeypatch.setattr(settings, "font_assets_dir", directory, raising=False)
    cap._FONT_DIR_STATE.clear()

    calls: list[int] = []
    monkeypatch.setattr(cap, "refresh_font_cache", lambda: calls.append(1) or True)
    assert cap.refresh_font_cache_if_changed()
    assert not cap.refresh_font_cache_if_changed()
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# A17 - several music tracks per mood
# --------------------------------------------------------------------------- #


def _music_library(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "music_dir", tmp_path, raising=False)
    (tmp_path / "upbeat.mp3").write_bytes(b"a")
    (tmp_path / "upbeat_2.mp3").write_bytes(b"b")
    (tmp_path / "upbeat-3.mp3").write_bytes(b"c")
    (tmp_path / "upbeatish.mp3").write_bytes(b"nope")
    (tmp_path / "chill").mkdir()
    for name in ("one.wav", "two.wav"):
        (tmp_path / "chill" / name).write_bytes(b"d")
    return tmp_path


def test_a17_all_three_library_layouts_are_found(tmp_path, monkeypatch):
    """Holding twenty tracks should not have to look like holding one."""
    _music_library(tmp_path, monkeypatch)
    assert [p.name for p in audio.find_user_tracks("upbeat")] == [
        "upbeat.mp3", "upbeat-3.mp3", "upbeat_2.mp3",
    ]
    assert [p.name for p in audio.find_user_tracks("chill")] == ["one.wav", "two.wav"]


def test_a17_a_similar_mood_name_does_not_borrow_another_mood_s_tracks(tmp_path, monkeypatch):
    """A prefix test alone would pull `upbeatish.mp3` into `upbeat`.

    A separator is required after the mood name, so "upbeat_2" matches and "upbeatish" does not.
    """
    _music_library(tmp_path, monkeypatch)
    assert not any("upbeatish" in p.name for p in audio.find_user_tracks("upbeat"))


def test_a17_the_exact_filename_stays_first_so_a_single_track_install_is_unchanged(
    tmp_path, monkeypatch
):
    _music_library(tmp_path, monkeypatch)
    assert audio.find_user_track("upbeat").name == "upbeat.mp3"


def test_a17_different_clips_get_different_tracks(tmp_path, monkeypatch):
    """The whole point. Ten clips carrying the same eight bars is what A17 removes."""
    _music_library(tmp_path, monkeypatch)
    tracks = audio.find_user_tracks("upbeat")
    keys = [f"source.mp4:{i}:{i * 12.5:.3f}" for i in range(8)]
    chosen = {audio.choose_track(tracks, key).name for key in keys}
    assert len(chosen) > 1, "every clip in the batch got the same bed"


def test_a17_the_same_clip_gets_the_same_track_on_every_run(tmp_path, monkeypatch):
    """Reproducibility, which is not optional here.

    The M1 golden renders depend on it, and a creator re-rendering one clip of ten to fix a typo
    does not want a different bed underneath it.
    """
    _music_library(tmp_path, monkeypatch)
    tracks = audio.find_user_tracks("upbeat")
    key = "source.mp4:3:41.250"
    first = audio.choose_track(tracks, key)
    assert all(audio.choose_track(tracks, key) == first for _ in range(20))


def test_a17_selection_does_not_use_python_s_salted_hash(tmp_path, monkeypatch):
    """``hash()`` of a string is salted per process unless PYTHONHASHSEED is set.

    So a ``hash(key) % len(tracks)`` selection is stable within one run and different on the
    next - reproducible in exactly the tests that would catch it, and not in production. Checked
    by running a fresh interpreter with a different seed and comparing.
    """
    _music_library(tmp_path, monkeypatch)
    script = (
        "import sys; sys.path.insert(0, '.');"
        "from config import settings;"
        f"settings.music_dir = r'{tmp_path}';"
        "from worker.effects import audio;"
        "t = audio.find_user_tracks('upbeat');"
        "print(audio.choose_track(t, 'source.mp4:3:41.250').name)"
    )
    outs = set()
    for seed in ("0", "1", "12345"):
        # `sys.executable`, not `.venv/bin/python`. A hard-coded venv path assumes one particular
        # way of setting the project up: CI installs into the interpreter `actions/setup-python`
        # provides and has no `.venv` at all, so this raised
        # `FileNotFoundError: '.venv/bin/python'` on every run — a test about hash seeds failing
        # over a layout detail. `sys.executable` is the interpreter running the suite, which is
        # the one whose behaviour is being asserted.
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        outs.add(proc.stdout.strip())
    assert len(outs) == 1, f"selection changed with the hash seed: {outs}"


def test_a17_no_key_reproduces_the_pre_a17_behaviour(tmp_path, monkeypatch):
    """A caller that does not opt in must get byte-identical output."""
    _music_library(tmp_path, monkeypatch)
    tracks = audio.find_user_tracks("upbeat")
    assert audio.choose_track(tracks, "") == tracks[0]


def test_a17_the_bed_reports_which_track_it_is(tmp_path, monkeypatch):
    """Variety has to be visible.

    A caller looking at two clip records could not otherwise tell whether they shared a bed - the
    path is not in the record, only the marker is.
    """
    _music_library(tmp_path, monkeypatch)
    bed = audio.resolve_music_bed("upbeat", 10.0, tmp_path / "w", select_key="a.mp4:1:5.000")
    assert bed is not None
    assert 1 <= bed.track_index <= 3
    assert bed.track_count == 3


def test_a17_the_synthesised_bed_reports_no_track_count(tmp_path, monkeypatch):
    """One drone per mood by construction, so a 1/1 would imply a library that is not there."""
    monkeypatch.setattr(settings, "music_dir", tmp_path / "empty", raising=False)
    monkeypatch.setattr(settings, "music_allow_synthesis", True, raising=False)
    bed = audio.resolve_music_bed("chill", 1.0, tmp_path / "w", select_key="a.mp4:0:0.000")
    assert bed is not None and bed.synthesised
    assert bed.track_count == 0


# --------------------------------------------------------------------------- #
# A19 - tag matching for the local b-roll library
# --------------------------------------------------------------------------- #


def _broll_library(tmp_path):
    for name in ("on.mp4", "ca.mp4", "pexels-4276282.mp4", "sunrise-timelapse.mp4",
                 "money-stack.mp4"):
        (tmp_path / name).write_bytes(b"x")
    return broll.LocalProvider(tmp_path)


def test_a19_a_two_character_filename_no_longer_answers_every_keyword(tmp_path):
    """The substring test this replaces matched ``stem in token``.

    With a two-character stem that is nearly always true: ``on.mp4`` answered "money" and
    ``ca.mp4`` answered "car". Tokens were length-filtered; stems were not.
    """
    provider = _broll_library(tmp_path)
    assert broll.match_score("money", set(), "on") == 0.0
    assert broll.match_score("car", set(), "ca") == 0.0
    result = provider.search("car")
    assert result is None


def test_a19_a_tag_beats_a_filename_coincidence(tmp_path):
    """A tag is what the operator deliberately said; a filename token is a coincidence."""
    provider = _broll_library(tmp_path)
    (tmp_path / broll.TAG_MANIFEST_NAME).write_text(json.dumps({
        "pexels-4276282.mp4": ["money", "banknote"],
    }))
    # `money-stack.mp4` matches "money" on its filename; the tagged file must win.
    from pathlib import Path
    assert Path(provider.search("money").path).name == "pexels-4276282.mp4"


def test_a19_a_stock_filename_becomes_findable_once_tagged(tmp_path):
    """A provider names its files ``pexels-4276282.mp4``. No filename rule can help."""
    provider = _broll_library(tmp_path)
    assert provider.search("wallet") is None
    (tmp_path / broll.TAG_MANIFEST_NAME).write_text(json.dumps({
        "pexels-4276282.mp4": ["cash", "wallet", "banknote"],
    }))
    from pathlib import Path
    assert Path(provider.search("wallet").path).name == "pexels-4276282.mp4"


def test_a19_a_synonym_reaches_a_tag_the_keyword_does_not_name(tmp_path):
    provider = _broll_library(tmp_path)
    (tmp_path / broll.TAG_MANIFEST_NAME).write_text(json.dumps({
        "pexels-4276282.mp4": ["wealth"],
    }))
    from pathlib import Path
    # "money" and "wealth" share a glyph in the emoji map, so they are one synonym group.
    assert Path(provider.search("money").path).name == "pexels-4276282.mp4"


def test_a19_a_synonym_scores_below_an_explicit_tag():
    """Synonyms are this code inferring; a tag is the operator stating.

    Inference must never override a statement, which is what the ordering guarantees.
    """
    assert broll.match_score("money", {"wealth"}) < broll.match_score("money", {"money"})
    assert broll.match_score("money", set(), "money-stack") < broll.match_score(
        "money", {"wealth"}
    )


def test_a19_synonym_expansion_is_conservative_rather_than_wrong():
    """Two words share a group only when they share a *picture*.

    So "money"/"wealth"/"fortune" are one group and "cash" is in another, because 💰 and 💵 are
    different images. A missed synonym costs one weaker match; a wrong one puts unrelated footage
    on screen.
    """
    assert "wealth" in broll.synonyms("money")
    assert "money" not in broll.synonyms("money")      # never itself
    assert broll.synonyms("qwertyuiop") == frozenset()


def test_a19_the_best_match_wins_not_the_first_one_found(tmp_path):
    """Directory order used to decide, so renaming an unrelated file changed the b-roll."""
    provider = _broll_library(tmp_path)
    (tmp_path / broll.TAG_MANIFEST_NAME).write_text(json.dumps({
        # "money stack" hits both tokens; the other hits one.
        "sunrise-timelapse.mp4": ["money", "stack"],
        "ca.mp4": ["money"],
    }))
    from pathlib import Path
    assert Path(provider.search("money stack").path).name == "sunrise-timelapse.mp4"


def test_a19_a_tie_is_broken_by_name_so_two_machines_agree(tmp_path):
    provider = _broll_library(tmp_path)
    (tmp_path / broll.TAG_MANIFEST_NAME).write_text(json.dumps({
        "sunrise-timelapse.mp4": ["money"],
        "money-stack.mp4": ["money"],
        "ca.mp4": ["money"],
    }))
    from pathlib import Path
    names = {Path(provider.search("money").path).name for _ in range(5)}
    assert names == {"ca.mp4"}


def test_a19_a_malformed_manifest_degrades_to_filename_matching(tmp_path):
    """Which is what the library did before A19 - not a library with silently no tags."""
    provider = _broll_library(tmp_path)
    (tmp_path / broll.TAG_MANIFEST_NAME).write_text("{ this is not json")
    assert broll.load_tag_manifest(tmp_path) == {}
    from pathlib import Path
    assert Path(provider.search("money").path).name == "money-stack.mp4"


def test_a19_tags_may_be_written_as_a_string_or_a_list(tmp_path):
    """Both are things a person writes by hand.

    Rejecting one would only produce a library that silently has no tags for those entries.
    """
    (tmp_path / broll.TAG_MANIFEST_NAME).write_text(json.dumps({
        "a.mp4": ["money", "cash"],
        "b.mp4": "money, cash",
        "c.mp4": 42,
    }))
    manifest = broll.load_tag_manifest(tmp_path)
    assert manifest["a.mp4"] == manifest["b.mp4"] == frozenset({"money", "cash"})
    assert "c.mp4" not in manifest


# --------------------------------------------------------------------------- #
# A22 - Ken Burns on stills, and ducking under b-roll
# --------------------------------------------------------------------------- #


def _still_cue(start=1.0, end=3.0, path="/tmp/still.png"):
    return broll.BrollCue(
        keyword="money", start=start, end=end,
        asset=broll.AssetRef(path=path, kind="image", provider="local", source_id="",
                             license="local", attribution=""),
    )


def _graph(**kwargs):
    _args, graph, _notes = broll.build_broll_overlay(
        [_still_cue()], "v0", "vout", width=1080, height=1920, fps=30, input_offset=1, **kwargs
    )
    return graph


def test_a22_ken_burns_is_off_by_default_so_the_shipped_graph_is_unchanged():
    """It cover-crops stills into a fixed box, which is a visible change to the look."""
    assert settings.broll_ken_burns is False
    assert "zoompan" not in _graph()
    assert "scale=540:-1" in _graph()


def test_a22_the_overlay_box_is_always_even_sided():
    """libx264's 4:2:0 chroma subsampling requires even dimensions, and ``crop`` will not round.

    At 1080 wide the numbers happen to come out even (540x304), so a test at the default width
    proves nothing. At 1000 wide the height is 281 - and an odd height fails the *encode*, several
    stages after the filter string that caused it.
    """
    import re

    for width in (1000, 1080, 720, 886, 1234):
        _args, graph, _notes = broll.build_broll_overlay(
            [_still_cue()], "v0", "vout", width=width, height=1920, fps=30,
            input_offset=1, ken_burns=True, zoom=0.12,
        )
        box = re.search(r"s=(\d+)x(\d+)", graph)
        assert box, graph
        w, h = int(box.group(1)), int(box.group(2))
        assert w % 2 == 0 and h % 2 == 0, (width, w, h)


def test_a22_ken_burns_adds_motion_to_a_still():
    graph = _graph(ken_burns=True, zoom=0.12)
    assert "zoompan" in graph
    # Cover-cropped into the fixed box zoompan needs an explicit size for.
    assert "crop=540:304" in graph
    assert "s=540x304" in graph


def test_a22_a_zero_zoom_disables_the_motion_even_when_ken_burns_is_set():
    """One setting turns it off, so a caller need not know two."""
    assert "zoompan" not in _graph(ken_burns=True, zoom=0.0)


def test_a22_the_zoom_is_a_function_of_time_not_an_accumulation():
    """``z='zoom+step'`` makes the final framing depend on how many frames were produced.

    The same still would zoom twice as far on a 60fps render as on a 30fps one. ``on/frames`` is
    the same motion at any frame rate, which is also what makes a golden render reproducible
    across a change of output fps.
    """
    at_30 = _graph(ken_burns=True, zoom=0.12)
    _a, at_60, _n = broll.build_broll_overlay(
        [_still_cue()], "v0", "vout", width=1080, height=1920, fps=60,
        input_offset=1, ken_burns=True, zoom=0.12,
    )
    assert "zoom+" not in at_30 and "pzoom" not in at_30
    # 2 seconds at 30fps is 60 frames; at 60fps it is 120. Same total zoom, different divisor.
    assert "on/60" in at_30
    assert "on/120" in at_60


def test_a22_consecutive_stills_do_not_all_drift_the_same_way():
    """Four stills in one clip moving identically reads as a template.

    The zoom converges on an anchor, so a *fixed* anchor supplies the pan - and rotating it is
    what makes the four differ.
    """
    cues = [_still_cue(start=float(i), end=float(i) + 1.5) for i in range(4)]
    _a, graph, _n = broll.build_broll_overlay(
        cues, "v0", "vout", width=1080, height=1920, fps=30,
        input_offset=1, ken_burns=True, zoom=0.12,
    )
    import re
    anchors = set(re.findall(r"x='\(iw-iw/zoom\)\*([\d.]+)'", graph))
    assert len(anchors) > 1, anchors


def test_a22_a_video_asset_is_never_given_ken_burns():
    """It already moves. Adding a zoom on top would be a second, competing motion."""
    cue = broll.BrollCue(
        keyword="money", start=1.0, end=3.0,
        asset=broll.AssetRef(path="/tmp/v.mp4", kind="video", provider="local", source_id="",
                             license="local", attribution=""),
    )
    _a, graph, _n = broll.build_broll_overlay(
        [cue], "v0", "vout", width=1080, height=1920, fps=30,
        input_offset=1, ken_burns=True, zoom=0.12,
    )
    assert "zoompan" not in graph
    assert "trim=" in graph


def test_a22_the_duck_is_a_pass_through_when_disabled():
    """A disabled feature must add no processing to the graph at all."""
    assert audio.broll_duck_filter("a", "b", [], amount=0.5) == "[a]anull[b]"
    assert audio.broll_duck_filter("a", "b", [(1.0, 3.0)], amount=0.0) == "[a]anull[b]"


def test_a22_overlapping_duck_windows_do_not_compound_to_silence():
    """A chain of ``volume`` filters multiplies, so two adjacent cues would mute the bed.

    Built as one expression taking the *deepest* applicable dip instead.
    """
    graph = audio.broll_duck_filter("a", "b", [(1.0, 3.0), (2.5, 4.0)], amount=0.4)
    # One `volume` *filter*, not a chain of them: `volume=volume='...'` is a single filter whose
    # first parameter happens to repeat the name.
    assert graph.count("]volume=") == 1
    assert "max(" in graph
    # The deepest dip is 0.4, so the expression can never fall below 0.6 however many windows
    # overlap - which a multiplying chain would.
    assert "max(" in graph and graph.count("0.400") >= 4


@requires_ffmpeg
def test_a22_the_duck_expression_is_accepted_by_ffmpeg_and_actually_dips(tmp_path):
    """A malformed expression is a runtime ffmpeg error, not a Python one.

    So the string has to be checked against the binary, and the *level* has to be checked or an
    expression that always evaluates to 1 would pass a syntax test.
    """
    duck = audio.broll_duck_filter("0:a", "ducked", [(1.0, 2.0)], amount=0.6, ramp=0.05)

    def rms(window_start: float) -> float:
        # The measurement has to live *inside* the filter graph: `-af` cannot be applied to a
        # `-filter_complex` output, which ffmpeg rejects outright rather than ignoring.
        graph = (
            f"{duck};"
            f"[ducked]atrim=start={window_start}:duration=0.4,"
            f"astats=metadata=1:reset=0[out]"
        )
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostats", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=3",
             "-filter_complex", graph, "-map", "[out]", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        )
        import re
        values = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", proc.stderr)
        assert values, proc.stderr[-2000:]
        return float(values[-1]) if values[-1] != "-inf" else -120.0

    quiet = rms(1.3)      # inside the b-roll window
    loud = rms(2.4)       # after it
    assert quiet < loud - 3.0, (quiet, loud)


@requires_ffmpeg
def test_a22_the_ken_burns_graph_renders_and_the_frame_changes(tmp_path, make_video, png_asset):
    """A zoompan expression ffmpeg rejects is a failed render, and one that is constant is a
    still with extra steps. Both pass a string test."""
    base = make_video("base.mp4", duration=4.0, w=640, h=360)
    still = png_asset("shot.png", color="red")
    cue = _still_cue(start=0.5, end=3.5, path=str(still))
    inputs, graph, _notes = broll.build_broll_overlay(
        [cue], "0:v", "vout", width=640, height=360, fps=25,
        input_offset=1, ken_burns=True, zoom=0.3,
    )
    out = tmp_path / "kb.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(base), *inputs,
         "-filter_complex", graph, "-map", "[vout]", "-t", "4", str(out)],
        check=True, capture_output=True, text=True,
    )
    assert out.exists() and out.stat().st_size > 0

    def frame_md5(index: int) -> str:
        return subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(out),
             "-vf", f"select=eq(n\\,{index})", "-frames:v", "1", "-f", "md5", "-"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    assert frame_md5(20) != frame_md5(75), "the overlay did not move"



def test_a22_the_compositor_ducks_only_the_windows_that_reached_the_screen(monkeypatch, tmp_path):
    """The duck windows have to come from cues that actually composited.

    A cue whose asset failed to resolve is not on screen, and a dip in the bed under nothing is a
    hole the viewer hears with no picture to explain it. Equally, wiring the windows through as an
    empty list would leave the whole feature inert while every unit test of the filter still
    passed - so this checks the *compositor* passes them, not just that the filter can build them.
    """
    from worker.effects import compositor

    monkeypatch.setattr(settings, "broll_duck", 0.4, raising=False)
    asset = tmp_path / "shot.png"
    asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    music = tmp_path / "bed.mp3"
    music.write_bytes(b"stub")

    cue = broll.BrollCue(
        keyword="money", start=1.0, end=3.0,
        asset=broll.AssetRef(path=str(asset), kind="image", provider="local",
                             source_id="", license="local", attribution=""),
    )
    monkeypatch.setattr(
        compositor.audio, "resolve_music_bed",
        lambda mood, duration, temp_dir, **_kw: compositor.audio.MusicBed(
            path=music, mood=mood, source=compositor.audio.SOURCE_USER_TRACK
        ),
    )

    from worker.ffmpeg_utils import MediaInfo

    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append([str(part) for part in cmd])
        from pathlib import Path as _P
        _P(str(cmd[-1])).write_bytes(b"stub-render")

    monkeypatch.setattr(
        compositor, "probe",
        lambda _path: MediaInfo(duration=4.0, width=1080, height=1920, fps=30.0, has_audio=True),
    )
    monkeypatch.setattr(compositor, "_run", fake_run)

    src = tmp_path / "base.mp4"
    src.write_bytes(b"stub")

    from tests.conftest import options_all_off
    opts = options_all_off(captions=False, metadata=False, aspect="9:16", music="chill")
    result = compositor.render_clip(
        src, tmp_path / "out.mp4", opts, [], tmp_path / "tmp",
        broll_resolver=lambda: [cue],
    )
    assert result is not None
    graph = next(
        cmd[cmd.index("-filter_complex") + 1] for cmd in calls if "-filter_complex" in cmd
    )
    # The dip is present, and it covers the cue's own window rather than some default.
    assert "1-(max(" in graph or "1-(between(t,1.000,3.000)" in graph, graph
    assert "1.000" in graph and "3.000" in graph
    assert "broll_ducked:1" in result.effects_applied


def test_a22_no_duck_marker_when_the_feature_is_off(monkeypatch, tmp_path):
    """The parity case: BROLL_DUCK=0 must leave the audio graph and the markers untouched."""
    assert settings.broll_duck == 0.0
    assert audio.broll_duck_filter("a", "b", [(1.0, 3.0)], amount=settings.broll_duck) == (
        "[a]anull[b]"
    )
