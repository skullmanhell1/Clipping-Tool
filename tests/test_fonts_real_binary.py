"""The font a clip is rendered in is the font that was asked for (C1, A1-A3, M7).

Every other caption test in this suite asserts the font *requested* — the ``Fontname``
written into the ASS style line, or the family a preset declares. That is the assertion
that let the font chain stay broken through five completed specs:

* every built-in preset declared ``Arial``;
* ``captions._FALLBACK_FONT``, used when a declared font is unavailable, was **also**
  ``Arial``;
* Arial is not installed on any Linux host.

So the substitution branch replaced a missing font with the same missing font, reported
``font_substituted:Arial`` (naming the font that did *not* work), and libass quietly
metric-aliased to whatever the host had — Liberation Sans Regular where
``fonts-liberation`` is installed, Noto Sans elsewhere — with synthesised bold rather than
a real heavy weight. The golden files in ``test_kinetic_compositor.py`` had
``font_substituted:Arial`` frozen in as expected output, so the suite asserted the bug.

This is the same shape as the ``ffmpeg -filters`` defect described in
``test_capabilities_real_binary.py``: a resolver whose output nothing checked. The fix is
the same too — cross-check against an **independent** mechanism. Here that mechanism is
libass itself, which at ``-loglevel verbose`` prints the resolution it performed:

    fontselect: (Anton, 700, 0) -> Anton-Regular, 0, Anton-Regular
    fontselect: (Anton, 700, 0) -> /usr/share/fonts/google-noto/NotoSans-Bold.ttf, 0, ...

The first line is a correct resolution; the second is the bug. Nothing in our own code
produces that line, so these tests cannot share a defect with the code they check.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from config import settings as app_settings
from worker import captions
from worker.captions import Cue
from worker.effects import caption_presets
from worker.engines import kinetic
from worker.transcribe import Word

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None, reason="no ffmpeg binary on PATH; real-binary font checks need one"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FONTS_DIR = REPO_ROOT / "assets" / "fonts"

#: Deliberately a sibling of the font directory, not inside it: everything in ``fontsdir``
#: is offered to libass as a font, and a stray file makes it log
#: ``Error opening memory font 'fonts.json'`` on every single render.
MANIFEST_PATH = REPO_ROOT / "assets" / "fonts.json"

#: ``fontselect: (<requested>, <weight>, <italic>) -> <resolved>, <index>, <postscript>``
#: The resolved field is either a bare face name (fontsdir provider) or an absolute path
#: (fontconfig provider), which is why assertions below match on the trailing PostScript
#: name rather than on the field's shape.
_FONTSELECT = re.compile(
    r"fontselect:\s*\((?P<requested>.+?),\s*\d+,\s*\d+\)\s*->\s*(?P<resolved>.+)$"
)

_ASS_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Alignment, MarginV, Encoding
Style: Default,{font},120,&H00FFFFFF,-1,2,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,HELLO WORLD
"""


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _fontselect_lines(subtitles_filter: str, tmp_path: Path) -> list[tuple[str, str]]:
    """Render one frame and return libass' ``(requested, resolved)`` decisions.

    A one-frame render over a synthetic ``color`` source: no media fixture is needed, and
    libass still performs — and reports — full font selection.
    """
    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "verbose",
            "-f", "lavfi", "-i", "color=black:s=540x960:d=0.1",
            "-vf", subtitles_filter,
            "-frames:v", "1", "-y", str(tmp_path / "probe.png"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"ffmpeg failed:\n{proc.stderr[-2000:]}"
    out: list[tuple[str, str]] = []
    for line in proc.stderr.splitlines():
        match = _FONTSELECT.search(line)
        if match:
            out.append((match.group("requested").strip(), match.group("resolved").strip()))
    assert out, f"libass logged no fontselect line:\n{proc.stderr[-2000:]}"
    return out


def _resolve_via_libass(font: str, tmp_path: Path) -> str:
    """What libass resolves a style naming ``font`` to, with the bundled dir available."""
    ass = tmp_path / "probe.ass"
    ass.write_text(_ASS_TEMPLATE.format(font=font), encoding="utf-8")
    decisions = _fontselect_lines(captions.subtitles_filter(ass), tmp_path)
    matching = [resolved for requested, resolved in decisions if requested == font]
    assert matching, f"libass never reported a decision for {font!r}: {decisions}"
    return matching[-1]


# --------------------------------------------------------------------------- #
# The manifest describes files that exist                                       #
# --------------------------------------------------------------------------- #
def test_every_manifest_font_file_and_licence_exists():
    """A1: the faces and their licences are vendored, not merely referenced.

    ``assets/emoji`` is the cautionary tale: its ``.gitignore`` claims the assets are
    "downloaded at build time", and nothing does that, so the directory is empty and the
    emoji overlay has never had anything to composite.
    """
    repo_root = FONTS_DIR.parents[1]
    for entry in _manifest()["fonts"]:
        assert (FONTS_DIR / entry["file"]).is_file(), f"missing font file: {entry['file']}"
        licence = repo_root / entry["license_file"]
        assert licence.is_file(), f"missing licence for {entry['name']}: {licence}"
        assert licence.stat().st_size > 0, f"empty licence file: {licence}"


def test_font_directory_contains_nothing_but_fonts():
    """``assets/fonts`` is handed to libass as ``fontsdir``, so it must contain only fonts.

    libass offers every entry of that directory to FreeType and complains about anything
    it cannot parse — ``Read failed`` for a subdirectory, ``Error opening memory font``
    for a stray file. ``test_kinetic_ass.py``'s burn tests reject both as libass problems,
    correctly: a warning on every render is how a real fault stays camouflaged.

    Hence the manifest lives at ``assets/fonts.json`` and the licences in
    ``assets/font-licenses/``, both siblings rather than children.
    """
    unexpected = [
        child.name
        for child in FONTS_DIR.iterdir()
        if child.is_dir() or child.suffix.lower() not in {".ttf", ".otf", ".ttc"}
    ]
    assert not unexpected, (
        f"{unexpected} in assets/fonts makes libass log a problem on every render; "
        "keep non-font files outside the fontsdir"
    )


def test_manifest_and_code_agree_on_the_fallback_ladder():
    """The ladder is data in ``fonts.json`` and a tuple in code; they must not drift."""
    assert tuple(_manifest()["fallback_ladder"]) == captions.FALLBACK_FONTS


def test_kinetic_last_rung_matches_the_captions_ladder():
    """Three modules spell the terminal rung separately; none may drift.

    ``kinetic`` repeats the value rather than importing it (it deliberately avoids
    importing ``captions`` at module scope), and ``tests/strategies.py`` needs it to
    compute the ladder a property test expects. A literal in three places is only safe
    with an assertion like this one.
    """
    from tests import strategies

    assert kinetic.FALLBACK_FONT == captions.FALLBACK_FONTS[-1]
    assert captions._FALLBACK_FONT == captions.FALLBACK_FONTS[-1]
    assert strategies._FALLBACK_FONT == captions.FALLBACK_FONTS[-1]


def test_no_fallback_rung_is_arial():
    """The original defect, named so it cannot come back quietly.

    Arial is not installed on any Linux host. A fallback that names it is not a fallback.
    """
    assert "arial" not in {name.lower() for name in captions.FALLBACK_FONTS}


def test_every_builtin_preset_requests_a_bundled_or_ladder_font():
    """A3's purpose: a preset may only name a face we know we ship."""
    known = {entry["family"] for entry in _manifest()["fonts"]}
    known |= set(captions.FALLBACK_FONTS)
    for name, preset in caption_presets.BUILTIN_PRESETS.items():
        assert preset.font in known, f"preset {name!r} requests unbundled font {preset.font!r}"


# --------------------------------------------------------------------------- #
# M7: libass resolves the requested font to the requested font                   #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize(
    "entry",
    [e for e in _manifest()["fonts"] if not e["variable"]],
    ids=lambda e: e["name"],
)
def test_libass_resolves_each_bundled_static_face_to_itself(entry, tmp_path):
    """Every non-variable bundled face resolves to its own file, not to a substitute.

    Variable faces are excluded on purpose and covered by the test below: libass'
    ``fontsdir`` provider does not select their named instances, which is a real
    limitation rather than a bug in our code.
    """
    resolved = _resolve_via_libass(entry["family"], tmp_path)
    expected = Path(entry["file"]).stem  # e.g. "Poppins-ExtraBold"
    assert expected in resolved, (
        f"{entry['family']!r} resolved to {resolved!r}, expected the bundled "
        f"{expected!r}. libass substituted a different face."
    )


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("preset_name", sorted(caption_presets.BUILTIN_PRESETS))
def test_every_builtin_preset_renders_in_the_font_it_asks_for(preset_name, tmp_path):
    """The end-to-end assertion C1 needed: through our own ASS builder and filter.

    Uses :func:`captions.build_ass` and :func:`captions.subtitles_filter` rather than a
    hand-written ASS, so a regression anywhere in that path — the style line, the preset
    default, the fallback ladder, the ``fontsdir`` wiring — surfaces here.
    """
    preset = caption_presets.BUILTIN_PRESETS[preset_name]
    ass = tmp_path / f"{preset_name}.ass"
    notes: list[str] = []
    captions.build_ass(
        [Cue(0.0, 1.0, [Word(0.0, 0.5, "HELLO"), Word(0.5, 1.0, "WORLD")])],
        ass,
        preset=preset,
        clip_duration=1.0,
        notes=notes,
    )

    decisions = _fontselect_lines(captions.subtitles_filter(ass), tmp_path)
    requested_families = {requested for requested, _ in decisions}
    assert preset.font in requested_families, (
        f"preset {preset_name!r} declares {preset.font!r} but libass was asked for "
        f"{sorted(requested_families)} — the style line lost the preset's font"
    )

    manifest_file = {e["family"]: e["file"] for e in _manifest()["fonts"]}
    for requested, resolved in decisions:
        if requested != preset.font:
            continue
        expected = Path(manifest_file[preset.font]).stem
        assert expected in resolved, (
            f"preset {preset_name!r} asked for {preset.font!r} and libass rendered "
            f"{resolved!r}. Expected {expected!r}."
        )


@requires_ffmpeg
@pytest.mark.real_binary
def test_arial_still_does_not_resolve_to_arial(tmp_path):
    """Why Arial cannot be a fallback, asserted rather than asserted-in-a-comment.

    Whatever this host substitutes (Liberation Sans where ``fonts-liberation`` is
    installed, Noto Sans otherwise), it is never Arial. If this test ever fails because
    a real Arial appeared, the fallback ladder deserves a fresh look — but the ladder
    must not *depend* on that.
    """
    resolved = _resolve_via_libass("Arial", tmp_path)
    assert "arial" not in resolved.lower(), (
        f"Arial resolved to {resolved!r}; this host has a real Arial, which the "
        "font ladder does not and should not assume."
    )


# --------------------------------------------------------------------------- #
# A bundled face needs no system font install                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
def host_fonts(monkeypatch):
    """Pin what ``fc-list`` reports, so the tests below do not depend on the machine.

    The tests above ask "what does libass do here?" and are therefore allowed to depend
    on the host. These ask the opposite question — "what does our resolver decide when
    the host has *none* of the bundled faces?" — and that state cannot be observed
    reliably on a developer box that happens to have run the Dockerfile's ``fc-cache``.
    Injecting it is what makes the assertion mean the same thing everywhere.

    Note the probe must report *something*: ``font_available`` is deliberately
    optimistic when enumeration fails entirely, so an empty set exercises a different
    branch than a host that simply lacks these families.
    """

    def install(*families: str) -> None:
        captions._FONT_CACHE.clear()
        monkeypatch.setattr(
            captions,
            "_enumerate_system_fonts",
            lambda: frozenset(family.lower() for family in families),
        )

    yield install
    # The cache is module-level and would otherwise leak the injected answer into every
    # later test in the session.
    captions._FONT_CACHE.clear()


def test_bundled_static_faces_are_available_without_a_system_install(host_fonts):
    """A face we ship is available because we ship it, not because the host installed it.

    ``subtitles_filter`` hands ``assets/fonts`` to libass as ``fontsdir``, so these
    faces render on a bare checkout. ``fc-list`` cannot see that directory, so probing
    fontconfig alone reports them missing and the resolver substitutes them away — 
    replacing a font that would have worked, which is the C1 defect in a new costume.
    """
    host_fonts("Noto Sans", "DejaVu Sans", "Liberation Sans")
    for entry in _manifest()["fonts"]:
        if entry["variable"]:
            continue
        assert captions.font_available(entry["family"]), (
            f"{entry['family']!r} is bundled at assets/fonts/{entry['file']} and reachable "
            "through fontsdir, but the resolver reports it unavailable"
        )


def test_variable_faces_are_not_claimed_from_the_bundled_dir_alone(host_fonts):
    """The exclusion that keeps the fix honest.

    libass' directory provider does not select named instances of a variable font, so a
    request for one through ``fontsdir`` alone resolves to something else entirely —
    ``assets/fonts.json`` records ``Montserrat`` silently becoming ``NotoSans-Bold``, and
    that is why no variable family appears on the fallback ladder. Counting these as
    available would mean substituting *towards* a face that cannot be rendered, which is
    worse than substituting away from one that could.
    """
    host_fonts("DejaVu Sans")  # no overlap with any bundled family
    variable = [e for e in _manifest()["fonts"] if e["variable"]]
    assert variable, "manifest has no variable faces; this test would pass vacuously"
    for entry in variable:
        # noqa on the message, not the rule: S608 (SQL injection) fires because the prose
        # "select its named instance from fontsdir" reads as SELECT ... FROM to the heuristic.
        # There is no query here. Suppressed at the one line it misfires on rather than by
        # adding S608 to the tests/* ignores, which would switch the rule off for a suite that
        # does execute real SQL against sqlite.
        assert not captions.font_available(entry["family"]), (
            f"{entry['family']!r} is a variable face; libass cannot select its named "  # noqa: S608
            "instance from fontsdir, so it must not count as available on that basis"
        )


def test_no_preset_substitutes_on_a_host_without_the_bundled_faces(host_fonts):
    """The regression this pins: every preset kept the font it declares.

    Asserts the *resolved* value rather than the requested one, which is the distinction
    that let the original font chain stay broken through five specs.
    """
    host_fonts("Noto Sans", "DejaVu Sans", "Liberation Sans")
    for name, preset in sorted(caption_presets.BUILTIN_PRESETS.items()):
        resolved, substituted = captions.resolve_font(preset.font)
        assert (resolved, substituted) == (preset.font, False), (
            f"preset {name!r} declares {preset.font!r} but resolved to {resolved!r} "
            f"(substituted={substituted}) on a host without the bundled faces installed"
        )


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("preset_name", sorted(caption_presets.BUILTIN_PRESETS))
def test_preset_renders_in_its_own_face_without_a_system_install(
    preset_name, host_fonts, tmp_path
):
    """The same end-to-end check as above, with the host pinned rather than trusted.

    ``test_every_builtin_preset_renders_in_the_font_it_asks_for`` passes on a machine
    that has run the Dockerfile's ``fc-cache`` and fails on one that has not, so on its
    own it cannot distinguish "the code is right" from "this box is convenient". Pinning
    the probe to a host with none of the bundled faces makes the outcome depend only on
    our own resolution logic and on libass.
    """
    host_fonts("Noto Sans", "DejaVu Sans", "Liberation Sans")
    preset = caption_presets.BUILTIN_PRESETS[preset_name]
    ass = tmp_path / f"{preset_name}.ass"
    captions.build_ass(
        [Cue(0.0, 1.0, [Word(0.0, 0.5, "HELLO"), Word(0.5, 1.0, "WORLD")])],
        ass,
        preset=preset,
        clip_duration=1.0,
    )

    decisions = _fontselect_lines(captions.subtitles_filter(ass), tmp_path)
    assert preset.font in {requested for requested, _ in decisions}, (
        f"preset {preset_name!r} declares {preset.font!r} but libass was asked for "
        f"{sorted({r for r, _ in decisions})} — the resolver substituted a bundled face"
    )

    expected = Path({e["family"]: e["file"] for e in _manifest()["fonts"]}[preset.font]).stem
    for requested, resolved in decisions:
        if requested == preset.font:
            assert expected in resolved, (
                f"preset {preset_name!r} asked for {preset.font!r} and libass rendered "
                f"{resolved!r}; expected the bundled {expected!r}"
            )


# --------------------------------------------------------------------------- #
# The substitution marker names the font that was used                           #
# --------------------------------------------------------------------------- #
def test_substitution_marker_records_the_font_actually_used(monkeypatch):
    """C1: ``font_substituted:<name>`` names the replacement, not the casualty.

    ``worker/models.py`` has always documented this marker as "preset font missing;
    ``<name>`` used", but the code recorded the *requested* font — so a marker could not
    tell you what a clip had been rendered in, which is the one question it exists to
    answer.
    """
    preset = caption_presets.BUILTIN_PRESETS["hormozi"]
    only_last_rung = captions.FALLBACK_FONTS[-1]

    resolved, substituted = captions.resolve_font(
        preset.font, available=lambda name: name == only_last_rung
    )
    assert (resolved, substituted) == (only_last_rung, True)

    # And through the style-line path that writes the marker. The probe is injected
    # rather than trusted: whether this host has Anton installed is not the subject.
    monkeypatch.setattr(captions, "font_available", lambda name: name == only_last_rung)
    notes: list[str] = []
    style, hook = captions._preset_header_styles(preset, None, 110, notes)

    assert notes == [f"font_substituted:{only_last_rung}"], (
        "the marker must name the replacement; recording the requested font tells the "
        "operator nothing about what was rendered"
    )
    # And the style line libass will read must carry the replacement, not the request.
    assert f",{only_last_rung}," in style
    assert f",{only_last_rung}," in hook
    assert preset.font not in style


def test_resolve_font_prefers_earlier_rungs():
    """The ladder is ordered by preference, so availability alone must not reorder it."""
    everything = captions.FALLBACK_FONTS

    # Everything on the ladder is installed, but the requested face is not: the *first*
    # rung wins. (The probe must say False for the request itself, otherwise keeping the
    # request is the correct answer and the ladder is never consulted.)
    resolved, substituted = captions.resolve_font(
        "No Such Face", available=lambda n: n != "No Such Face"
    )
    assert (resolved, substituted) == (everything[0], True)

    # Only a middle rung installed: that rung wins, not the terminal one.
    middle = everything[3]
    resolved, substituted = captions.resolve_font(
        "No Such Face", available=lambda n: n == middle
    )
    assert (resolved, substituted) == (middle, True)


def test_resolve_font_keeps_an_available_request_untouched():
    resolved, substituted = captions.resolve_font("Anton", available=lambda n: True)
    assert (resolved, substituted) == ("Anton", False)



# --------------------------------------------------------------------------- #
# C3: a heavy face is not asked to be bold on top                              #
# --------------------------------------------------------------------------- #
@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("preset_name", sorted(caption_presets.BUILTIN_PRESETS))
def test_no_preset_asks_libass_to_synthesise_bold(preset_name, tmp_path):
    """Every bundled preset face already carries its weight, so Bold must be off.

    ASS has one bold flag and no way to say "weight 800". libass turns the flag into a
    request for CSS weight 700, and when the matched face cannot supply it, libass
    *synthesises* the emboldening — it thickens the outlines of a face that was already
    drawn heavy, which is the soft, slightly swollen look of fake bold.

    The requested weight is in libass' own ``fontselect:`` line, so this is checked rather
    than reasoned about:

        Bold=-1  ->  fontselect: (Anton, 700, 0) -> Anton-Regular    # 700 asked, 400 got
        Bold=0   ->  fontselect: (Anton, 400, 0) -> Anton-Regular    # no gap to fake

    Both resolve to the same file. The difference is entirely whether libass then fakes
    the weight difference, which is why asserting the resolved *file* is not enough here.
    """
    preset = caption_presets.BUILTIN_PRESETS[preset_name]
    assert captions.ass_bold_flag(preset) == 0, (
        f"preset {preset_name!r} declares font_weight={preset.font_weight}, which is below "
        "the threshold at which the face is trusted to supply its own weight"
    )

    ass = tmp_path / f"{preset_name}.ass"
    captions.build_ass(
        [Cue(0.0, 1.0, [Word(0.0, 0.5, "HELLO"), Word(0.5, 1.0, "WORLD")])],
        ass,
        preset=preset,
        clip_duration=1.0,
    )
    decisions = _fontselect_lines(captions.subtitles_filter(ass), tmp_path)

    proc = subprocess.run(
        [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "verbose",
            "-f", "lavfi", "-i", "color=black:s=540x960:d=0.1",
            "-vf", captions.subtitles_filter(ass),
            "-frames:v", "1", "-y", str(tmp_path / "weight.png"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    weights = re.findall(r"fontselect:\s*\(.+?,\s*(\d+),\s*\d+\)", proc.stderr)
    assert weights, f"no fontselect line to read a weight from:\n{proc.stderr[-1500:]}"
    assert "700" not in weights, (
        f"preset {preset_name!r} made libass request weight 700 from an already-heavy "
        f"face (weights seen: {sorted(set(weights))}); it will synthesise the bold"
    )
    assert decisions  # the render really did select fonts


def test_every_preset_declares_a_weight_matching_its_bundled_face():
    """``font_weight`` must describe the face the preset actually names.

    The field only earns its keep if it is true: a preset claiming weight 800 while naming
    a Regular face would turn the Bold flag off and render light text.
    """
    manifest = {entry["family"]: entry for entry in _manifest()["fonts"]}
    for name, preset in sorted(caption_presets.BUILTIN_PRESETS.items()):
        entry = manifest.get(preset.font)
        assert entry, f"preset {name!r} names {preset.font!r}, absent from the manifest"
        assert entry["heavy_face"], (
            f"preset {name!r} declares font_weight={preset.font_weight} but "
            f"{preset.font!r} is not a heavy face, so turning Bold off renders it light"
        )


# --------------------------------------------------------------------------- #
# C7 / C8: the preset's own presentation fields reach the ASS document          #
# --------------------------------------------------------------------------- #
def test_uppercase_preset_upper_cases_cue_text_without_mutating_the_words(tmp_path):
    """C7, plus the reason the implementation wraps rather than mutates.

    The same ``Word`` objects are read again by the keyword planner, the emoji planner and
    the kinetic engine. Upper-casing in place would leak a caption presentation choice into
    all of them.
    """
    words = [Word(0.0, 0.5, "quiet"), Word(0.5, 1.0, "words")]
    preset = caption_presets.BUILTIN_PRESETS["hormozi"]
    assert preset.uppercase

    ass = tmp_path / "upper.ass"
    captions.build_ass([Cue(0.0, 1.0, words)], ass, preset=preset, clip_duration=1.0)
    text = ass.read_text(encoding="utf-8")

    assert "QUIET" in text and "WORDS" in text
    assert [word.text for word in words] == ["quiet", "words"], "the transcript was mutated"


def test_outline_and_shadow_come_from_the_preset(tmp_path):
    """C8: both are per preset, not inferred from the animation style.

    They used to be 4/2 for ``karaoke_fill`` and 2/1 for everything else, so a preset could
    not ask for a heavier treatment — and 2 units at PlayRes 1920 is close to invisible over
    real footage.
    """
    import dataclasses

    base = caption_presets.BUILTIN_PRESETS["pop"]
    preset = dataclasses.replace(base, outline=9, shadow=7)
    ass = tmp_path / "edges.ass"
    captions.build_ass(
        [Cue(0.0, 1.0, [Word(0.0, 1.0, "edge")])],
        ass,
        preset=preset,
        clip_duration=1.0,
    )
    style = next(
        line for line in ass.read_text(encoding="utf-8").splitlines()
        if line.startswith("Style: Default")
    )
    fields = style.split("Style: ", 1)[1].split(",")
    # BorderStyle, Outline, Shadow are fields 16, 17, 18 of the V4+ format.
    assert (fields[16], fields[17]) == ("9", "7"), style

    # A negative value is clamped rather than written into the document, where libass
    # would reject the whole style line.
    weird = dataclasses.replace(base, outline=-4, shadow=-1)
    ass2 = tmp_path / "clamped.ass"
    captions.build_ass(
        [Cue(0.0, 1.0, [Word(0.0, 1.0, "edge")])],
        ass2,
        preset=weird,
        clip_duration=1.0,
    )
    style2 = next(
        line for line in ass2.read_text(encoding="utf-8").splitlines()
        if line.startswith("Style: Default")
    )
    fields2 = style2.split("Style: ", 1)[1].split(",")
    assert (fields2[16], fields2[17]) == ("0", "0"), style2
