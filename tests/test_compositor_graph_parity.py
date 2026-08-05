"""The exact ffmpeg command the compositor builds, frozen per configuration.

`compositor.render_clip` is ~440 lines assembling one ffmpeg invocation across roughly 25
phases, and until now the *only* thing that expressed the layering was append-order into two
Python lists plus hand-written labels (`vlook`/`vbroll`/`vbase`/`vout`/`vbrand`,
`aclean`/`aout`/`aeng`/`aloud`/`apeak`). A mis-ordered append is not an error: it is a silent
visual regression that still encodes successfully.

So before restructuring any of it, this pins what it currently produces. These are
**characterisation tests** — they assert current behaviour rather than desired behaviour, and
their value is entirely in being written *first*. A refactor that changes one of these strings
has changed the render, whatever the test suite otherwise says.

Two things are checked per configuration:

* the `-filter_complex` graph, split into its `;`-separated segments, which is where layer
  order and label wiring live;
* the argv shape — every `-i` in order, and the codec/map flags — which is where the
  **input-index accounting** lives. Those indices are load-bearing: b-roll and emoji overlays
  address inputs by number, and the brand logo is drawn with ffmpeg's `movie=` source filter
  specifically to avoid perturbing them.

Paths are normalised because `tmp_path` changes per run; nothing else is. Durations are not
normalised — the graph embeds fade and zoom timings derived from the probed duration, and a
change in those is exactly the kind of thing worth catching.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conftest import FakeWord, options_all_off, requires_ffmpeg
from worker.effects import compositor
from worker.effects.broll import AssetRef, BrollCue

pytestmark = requires_ffmpeg

#: Committed alongside this module. Regenerated deliberately, by running
#: ``python scripts/freeze_compositor_graph.py`` — never automatically, and never from inside the
#: test run. A golden that rewrites itself when it fails is not a guard.
#:
#: **Inspect the diff.** Every line of it describes a change to what ffmpeg is asked to do.
GOLDEN = Path(__file__).parent / "golden" / "compositor_commands.json"


def _words():
    return [
        FakeWord(0.2, 0.6, "This"), FakeWord(0.7, 1.1, "is"),
        FakeWord(1.2, 1.6, "fire"), FakeWord(1.7, 2.2, "money"),
    ]


def _normalise(text: str, tmp_path: Path) -> str:
    """Replace machine- and run-specific paths, and nothing else."""
    repo = str(Path(compositor.__file__).resolve().parents[2])
    out = text.replace(str(tmp_path.resolve()), "<TMP>").replace(str(tmp_path), "<TMP>")
    out = out.replace(repo, "<REPO>")
    # The ffmpeg binary may be an absolute path from PATH resolution or a bare name.
    out = re.sub(r"^\S*ffmpeg$", "<FFMPEG>", out)
    return out


def _capture(monkeypatch, tmp_path, base, options, **kwargs) -> dict:
    """Run `render_clip` with ffmpeg stubbed out, and return its normalised command."""
    recorded: list[list[str]] = []

    def _fake_run(cmd, *a, **kw):
        recorded.append([str(part) for part in cmd])
        # Produce the destination so callers that stat it still work.
        dest = Path(cmd[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\0" * 64)
        return None

    monkeypatch.setattr(compositor, "_run", _fake_run)
    result = compositor.render_clip(
        base, tmp_path / "out.mp4", options, _words(), tmp_path, **kwargs
    )

    if result is None:
        return {"rendered": False}
    assert len(recorded) == 1, f"expected exactly one ffmpeg pass, got {len(recorded)}"
    cmd = [_normalise(part, tmp_path) for part in recorded[0]]

    graph: list[str] = []
    if "-filter_complex" in cmd:
        graph = cmd[cmd.index("-filter_complex") + 1].split(";")

    return {
        "rendered": True,
        # Every input in order. This is the index accounting, made visible.
        "inputs": [cmd[i + 1] for i, part in enumerate(cmd) if part == "-i"],
        "graph": graph,
        # Mapping and codec choices: which streams were re-encoded versus copied.
        "flags": [
            part for part in cmd
            if part.startswith("-") and part not in ("-i", "-filter_complex")
        ],
        "maps": [cmd[i + 1] for i, part in enumerate(cmd) if part == "-map"],
        "effects": sorted(result.effects_applied),
    }


# --------------------------------------------------------------------------- #
# The configuration matrix                                                      #
# --------------------------------------------------------------------------- #
#: Named so a golden diff says which configuration changed.
#:
#: Chosen to exercise each *structural* branch rather than every option: whether the look chain
#: exists, whether b-roll splits the chain in two, whether emoji adds a layer on top, and each
#: audio stage. The b-roll case is the important one — it is the only path where the look and
#: caption chains are emitted as two separate segments.
CONFIGURATIONS: dict[str, dict] = {
    "nothing_enabled": dict(options=dict(captions=False)),
    "captions_only": dict(options=dict(captions=True)),
    "look_only": dict(options=dict(captions=False, color="vivid", zoom=True)),
    "look_and_captions": dict(options=dict(captions=True, color="warm", zoom=True, fades=True)),
    "progress_bar_only": dict(options=dict(captions=False, progress_bar=True)),
    "hook_title_only": dict(options=dict(captions=False, hook_title=True), hook_text="WAIT"),
    "captions_and_hook": dict(options=dict(captions=True, hook_title=True), hook_text="WAIT"),
    "music_only": dict(options=dict(captions=False, music="chill")),
    "music_and_fades": dict(options=dict(captions=False, music="chill", fades=True)),
    "fades_audio_only": dict(options=dict(captions=False, fades=True)),
    "everything_visual": dict(options=dict(
        captions=True, hook_title=True, color="vivid", zoom=True, transitions=True,
        fades=True, progress_bar=True,
    ), hook_text="WAIT FOR IT"),
    "loudness_with_music": dict(options=dict(
        captions=False, music="upbeat", loudness_normalise=True,
    )),
    "caption_preset_pop": dict(options=dict(captions=True, caption_preset="pop")),
    # --- the input-index accounting ----------------------------------------------
    # These four exist because the first version of this matrix had none of them, and an
    # `emoji_offset` mutated by +1 passed every case. Three offsets interact — music shifts
    # b-roll, b-roll shifts emoji — so each combination has to appear or the arithmetic is
    # unchecked.
    "emoji_only": dict(options=dict(captions=False, emoji="standard"), needs="emoji"),
    "emoji_with_music": dict(
        options=dict(captions=False, emoji="standard", music="chill"), needs="emoji",
    ),
    "broll_with_captions_and_emoji": dict(
        options=dict(captions=True, emoji="standard"), needs="both",
    ),
    "broll_music_captions_emoji": dict(
        options=dict(captions=True, emoji="standard", music="chill", color="vivid", zoom=True),
        needs="both",
    ),
}


def resolvers(needs: str | None, tmp_path: Path) -> dict:
    """Injected overlay sources, so a configuration can add inputs without a network or a CDN.

    Deliberately fixed 1x1 PNGs rather than real artwork: what is being frozen is the *index*
    each overlay input lands on and the label wiring around it, not the pixels.
    """
    if not needs:
        return {}
    png = tmp_path / "overlay.png"
    if not png.exists():
        # A minimal valid PNG. ffmpeg never runs here (`_run` is stubbed), so it only has to
        # exist for the path checks inside the overlay builders.
        png.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        ))
    out: dict = {"emoji_resolver": lambda cue: png}
    if needs == "both":
        asset = AssetRef(path=str(png), kind="image", provider="local", license="local")
        out["broll_resolver"] = lambda: [BrollCue(0.4, 1.4, "fire", asset=asset)]
    return out


@pytest.fixture(scope="module")
def golden() -> dict:
    if not GOLDEN.exists():
        return {}
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(CONFIGURATIONS))
def test_the_command_is_unchanged(name, golden, make_video, tmp_path, monkeypatch):
    """One configuration, compared against its frozen command.

    A failure here is not necessarily a bug — but it *is* a change to what ffmpeg is asked to
    do, so it has to be looked at and explained rather than re-frozen reflexively.
    """
    spec = dict(CONFIGURATIONS[name])
    option_kwargs = spec.pop("options")
    spec.update(resolvers(spec.pop("needs", None), tmp_path))
    base = make_video("base.mp4", duration=3.0, w=1080, h=1920)
    options = options_all_off(**option_kwargs)

    captured = _capture(monkeypatch, tmp_path, base, options, **spec)

    if name not in golden:
        # Failure rather than skip: a skip would satisfy this suite while checking nothing, and
        # CI rejects skips precisely because they read as passes.
        pytest.fail(
            f"no frozen command for {name!r}. Generate it with "
            "`python scripts/freeze_compositor_graph.py` and inspect the diff."
        )

    expected = golden[name]
    assert captured["rendered"] == expected["rendered"], name
    if not captured["rendered"]:
        return

    # Compared field by field so a failure names *what* moved rather than dumping two commands.
    assert captured["inputs"] == expected["inputs"], f"{name}: input order/count changed"
    assert captured["graph"] == expected["graph"], f"{name}: filter graph changed"
    assert captured["maps"] == expected["maps"], f"{name}: stream mapping changed"
    assert captured["flags"] == expected["flags"], f"{name}: encoder flags changed"
    assert captured["effects"] == expected["effects"], f"{name}: effects_applied changed"


def test_the_golden_file_covers_every_configuration(golden):
    """So a configuration cannot be added to the matrix and silently never checked.

    Without this, the `pytest.skip` above would quietly turn a new case into no coverage — the
    same vacuous-pass hole the no-skip CI gate exists to close.
    """
    missing = sorted(set(CONFIGURATIONS) - set(golden))
    assert not missing, (
        f"{missing} have no frozen command. Generate with "
        "`python scripts/freeze_compositor_graph.py`"
    )
    stale = sorted(set(golden) - set(CONFIGURATIONS))
    assert not stale, (
        f"{stale} are frozen but no longer in the matrix; remove them so the file describes "
        "what is actually checked"
    )


def test_a_rendered_configuration_produces_exactly_one_pass(make_video, tmp_path, monkeypatch):
    """The single-pass contract, asserted directly rather than inferred from the goldens.

    Every effect is composed into one `-filter_complex`; an engine never invokes ffmpeg itself.
    Two passes would mean a generation of quality lost for free.
    """
    base = make_video("base.mp4", duration=2.0, w=720, h=1280)
    options = options_all_off(
        captions=True, color="vivid", zoom=True, progress_bar=True, music="chill",
    )
    captured = _capture(monkeypatch, tmp_path, base, options)
    assert captured["rendered"] is True
    # _capture already asserts exactly one recorded command; this states the intent.
    assert captured["graph"], "a rendered clip with effects produced no filter graph"
