"""A15 sound-effect stings reach the rendered audio graph.

`worker/effects/sfx.py` shipped complete and tested with no importer outside its own test module, so
`sfx_volume` was read by nothing and `SFX_MODE` had no path that would honour any value other than
the documented default. `tests/test_script_and_placement.py` covers the module's own arithmetic; this
file covers whether anything calls it.

Two things here are about honesty rather than function, and both are the point of the file:

* **`assets/sfx/` ships empty**, and only `pop` and `click` can be synthesised. `TRIGGER_SFX` maps the
  *transition* trigger to `whoosh`, which cannot be — so `SFX_MODE=transitions` with no user file
  produces no sound at all. That must be *reported*, not passed over silently.
* **Input indices are an absolute contract.** `build_mix` numbers its inputs from an offset, so the
  sfx argv has to be appended after the emoji block. Getting that wrong renumbers every emoji input
  and the graph either fails to initialise or overlays the wrong images.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.conftest import FFMPEG, options_all_off, requires_ffmpeg
from worker.effects import compositor as comp
from worker.effects import sfx
from worker.transcribe import Word


def _words(text: str = "this is amazing money fire love win") -> list[Word]:
    return [
        Word(start=i * 0.5, end=(i * 0.5) + 0.4, text=token) for i, token in enumerate(text.split())
    ]


def _capture(monkeypatch, tmp_path, make_video, *, mode, duration=4.0, **option_overrides):
    """Render through `render_clip` with a stubbed encoder; return (argv, graph entries, markers)."""
    src = make_video("sfx.mp4", duration=duration, w=640, h=360)
    seen: dict = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = list(cmd)
        if "-filter_complex" in cmd:
            seen["graph"] = cmd[cmd.index("-filter_complex") + 1].split(";")
        return None

    monkeypatch.setattr(comp, "_run", fake_run)
    monkeypatch.setattr(comp.settings, "sfx_mode", mode)

    result = comp.render_clip(
        src,
        tmp_path / "out.mp4",
        options_all_off(aspect="9:16", captions=True, **option_overrides),
        _words(),
        tmp_path / "tmp",
    )
    return seen.get("cmd", []), seen.get("graph", []), (result.effects_applied if result else [])


# --------------------------------------------------------------------------- #
# The sting reaches the graph                                                 #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_an_emoji_sting_reaches_the_filter_complex(monkeypatch, tmp_path, make_video):
    """`pop` is synthesisable, so the emoji trigger is the one that can actually make a sound."""
    _cmd, graph, markers = _capture(
        monkeypatch, tmp_path, make_video, mode="emoji", emoji="heavy", emoji_mode="keyword"
    )

    mixed = [entry for entry in graph if "[asfx]" in entry]
    assert mixed, f"no sfx mix in the graph: {graph}"
    assert "amix" in mixed[0]
    assert "normalize=0" in mixed[0], (
        "amix without normalize=0 makes the speech 1/n quieter for the whole clip"
    )
    assert any(m.startswith("sfx:") for m in markers), markers


@requires_ffmpeg
def test_the_sting_is_delayed_to_its_moment_on_every_channel(monkeypatch, tmp_path, make_video):
    """`adelay=...:all=1`. Without `all=1` a stereo sting arrives on one channel then the other."""
    _cmd, graph, _markers = _capture(
        monkeypatch, tmp_path, make_video, mode="emoji", emoji="heavy", emoji_mode="keyword"
    )

    delays = [entry for entry in graph if "adelay=" in entry]
    assert delays, graph
    assert all(":all=1" in entry for entry in delays)


@requires_ffmpeg
def test_off_by_default_adds_no_input_and_no_filter(monkeypatch, tmp_path, make_video):
    """The documented default. It has to be a true no-op or every audio golden moves."""
    cmd, graph, markers = _capture(
        monkeypatch, tmp_path, make_video, mode="off", emoji="heavy", emoji_mode="keyword"
    )

    assert not [entry for entry in graph if "[asfx]" in entry]
    assert not [m for m in markers if m.startswith("sfx")]
    assert not [arg for arg in cmd if str(arg).endswith("sfx_pop.wav")]


# --------------------------------------------------------------------------- #
# The absent-sound case, which is most of them                                #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_a_transition_sting_reports_that_the_sound_is_missing(monkeypatch, tmp_path, make_video):
    """`SFX_MODE=transitions` cannot make a sound out of the box, and says so.

    `TRIGGER_SFX["transition"]` is `whoosh`; `SFX_NAMES["whoosh"]` is `False`, meaning not
    synthesisable; and `assets/sfx/` ships empty. So an operator who sets this hears nothing. The
    marker is the difference between "this feature is off" and "this feature is broken".
    """
    monkeypatch.setattr(comp.settings, "sfx_dir", str(tmp_path / "empty"))
    _cmd, graph, markers = _capture(
        monkeypatch, tmp_path, make_video, mode="transitions", transitions=True
    )

    assert not [entry for entry in graph if "[asfx]" in entry], (
        "a sting was mixed for a sound that cannot exist"
    )
    assert "sfx_missing:whoosh" in markers, markers


@requires_ffmpeg
def test_a_user_supplied_sting_wins_and_then_the_transition_works(
    monkeypatch, tmp_path, make_video
):
    """Dropping a `whoosh.wav` into `SFX_DIR` is the documented way to get the missing sound.

    This is the other half of the test above: the refusal must be about the *absent file*, not about
    the transition trigger being unwired.
    """
    sfx_dir = tmp_path / "stings"
    sfx_dir.mkdir()
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=d=0.2:c=pink",
            str(sfx_dir / "whoosh.wav"),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(comp.settings, "sfx_dir", str(sfx_dir))

    _cmd, graph, markers = _capture(
        monkeypatch, tmp_path, make_video, mode="transitions", transitions=True
    )

    assert [entry for entry in graph if "[asfx]" in entry], graph
    assert "sfx_missing:whoosh" not in markers
    assert any(m.startswith("sfx:") for m in markers), markers


@requires_ffmpeg
def test_no_sting_for_an_emoji_that_never_reached_the_screen(monkeypatch, tmp_path, make_video):
    """An accent on nothing is worse than no accent.

    An emoji cue can be planned and still not composite — its asset may fail to resolve. The sting is
    keyed off the *overlay graph*, not the cue list, for exactly that reason: a pop with no emoji
    behind it is an unexplained noise, and the viewer has no way to interpret it.

    The same discipline `broll_duck_windows` applies when it dips the bed only under b-roll that
    actually made it on screen.
    """
    # Cues plan normally, then the overlay build yields nothing at all.
    monkeypatch.setattr(comp.emoji, "build_overlay", lambda *a, **k: ([], ""))

    _cmd, graph, markers = _capture(
        monkeypatch, tmp_path, make_video, mode="emoji", emoji="heavy", emoji_mode="keyword"
    )

    assert not [entry for entry in graph if "[asfx]" in entry], (
        "a sting was mixed for an emoji that never reached the screen"
    )
    assert not [m for m in markers if m.startswith("sfx:")], markers


@requires_ffmpeg
def test_a_missing_sound_is_reported_once_not_once_per_hit(monkeypatch, tmp_path, make_video):
    """Twelve emoji with no `pop` available is one missing sound, not twelve.

    A marker list that repeats the same fact per occurrence is noise, and `effects_applied` is read
    by humans.
    """
    monkeypatch.setattr(comp.settings, "sfx_dir", str(tmp_path / "empty"))
    monkeypatch.setattr(comp.settings, "sfx_mode", "emoji")
    # Force every resolution to fail, so the repetition is what is under test rather than synthesis.
    monkeypatch.setattr(sfx, "resolve_sting", lambda name, _tmp: (None, f"sfx_missing:{name}"))

    hits, markers = comp._plan_sfx(
        emoji_starts=tuple(i * 0.5 for i in range(12)),
        transition_times=(),
        duration=10.0,
        temp_dir=tmp_path,
    )

    assert hits == []
    assert markers == ["sfx_missing:pop"]


# --------------------------------------------------------------------------- #
# The input-index contract                                                    #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_sfx_inputs_come_last_and_their_indices_match_the_graph(
    monkeypatch, tmp_path, make_video
):
    """The contract that a wrong answer breaks silently.

    `build_mix` addresses its inputs by absolute index, so the sfx argv must be appended *after* the
    emoji block. Append it earlier and every emoji input is renumbered — the graph then either fails
    to initialise or composites the wrong image, and neither failure points at sfx.

    Asserted by reading the index out of the graph and checking that argv position really is the
    sting, rather than by pinning a number that would restate the arithmetic.
    """
    cmd, graph, _markers = _capture(
        monkeypatch,
        tmp_path,
        make_video,
        mode="emoji",
        emoji="heavy",
        emoji_mode="keyword",
        caption_emoji=True,
    )

    delays = [entry for entry in graph if "adelay=" in entry]
    assert delays, graph

    index = int(delays[0].split("[")[1].split(":")[0])
    positions = [i for i, arg in enumerate(cmd) if arg == "-i"]
    assert index < len(positions), (
        f"the graph references input {index} but only {len(positions)} were supplied"
    )
    referenced = cmd[positions[index] + 1]
    assert str(referenced).endswith(".wav"), (
        f"input {index} is {referenced!r}, not a sting -- the sfx inputs are in the wrong position "
        "and the emoji inputs have been renumbered"
    )


@requires_ffmpeg
def test_the_volume_setting_is_actually_applied(monkeypatch, tmp_path, make_video):
    """`sfx_volume` was read by nothing at all before this.

    Two different values must produce two different graphs, which is the only assertion that cannot
    pass if the setting is ignored.
    """
    monkeypatch.setattr(comp.settings, "sfx_volume", 0.9)
    _c1, graph_loud, _m1 = _capture(
        monkeypatch, tmp_path, make_video, mode="emoji", emoji="heavy", emoji_mode="keyword"
    )
    monkeypatch.setattr(comp.settings, "sfx_volume", 0.1)
    _c2, graph_quiet, _m2 = _capture(
        monkeypatch, tmp_path, make_video, mode="emoji", emoji="heavy", emoji_mode="keyword"
    )

    loud = [e for e in graph_loud if "volume=" in e and "sfx" in e]
    quiet = [e for e in graph_quiet if "volume=" in e and "sfx" in e]
    assert loud and quiet
    assert loud != quiet, "sfx_volume made no difference to the graph"
    assert "volume=0.900" in loud[0]
    assert "volume=0.100" in quiet[0]


# --------------------------------------------------------------------------- #
# Placement in the chain                                                      #
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_sting_is_mixed_after_the_music_bed_and_before_loudnorm(
    monkeypatch, tmp_path, make_video
):
    """Both halves are requirements.

    After the bed, because a sting mixed into the *speech* branch would be ducked by AU2 every time it
    landed under the music — the opposite of an accent. Before `loudnorm`, so the sting is inside the
    signal being corrected rather than added after the correction.
    """
    _cmd, graph, _markers = _capture(
        monkeypatch,
        tmp_path,
        make_video,
        mode="emoji",
        emoji="heavy",
        emoji_mode="keyword",
        music="chill",
        loudness_normalise=True,
    )

    positions = {"sfx": None, "bed": None, "loud": None}
    for i, entry in enumerate(graph):
        if "[asfx]" in entry and positions["sfx"] is None:
            positions["sfx"] = i
        if "sidechaincompress" in entry or ("amix" in entry and "[aout]" in entry):
            positions["bed"] = i if positions["bed"] is None else positions["bed"]
        if "loudnorm" in entry and positions["loud"] is None:
            positions["loud"] = i

    assert positions["sfx"] is not None, graph
    if positions["bed"] is not None:
        assert positions["bed"] < positions["sfx"], "the sting was mixed before the music bed"
    if positions["loud"] is not None:
        assert positions["sfx"] < positions["loud"], "the sting was added after loudness correction"


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_graph_actually_initialises_in_ffmpeg(monkeypatch, tmp_path, make_video):
    """The test that would catch a malformed filter string.

    Every other assertion here reads a graph that was never handed to ffmpeg. A plausible-looking
    label or a wrong input index only fails at initialisation, which is exactly how the `volume=<expr>dB`
    defect in AU12 survived review — so one test renders for real.
    """
    src = make_video("sfx_real.mp4", duration=3.0, w=320, h=240)
    monkeypatch.setattr(comp.settings, "sfx_mode", "emoji")

    result = comp.render_clip(
        src,
        tmp_path / "real.mp4",
        options_all_off(
            aspect="9:16", captions=True, emoji="heavy", emoji_mode="keyword", caption_emoji=True
        ),
        _words(),
        tmp_path / "tmp",
    )

    assert result is not None
    assert (tmp_path / "real.mp4").exists(), "the composed clip was not produced"
    assert (tmp_path / "real.mp4").stat().st_size > 0
