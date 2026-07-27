"""Integration tests for the single-pass effect compositor."""
from __future__ import annotations

from tests.conftest import FakeWord, probe_duration, probe_size, requires_ffmpeg
from worker.effects import compositor
from worker.models import ProcessingOptions


def _words():
    return [FakeWord(0.2, 0.6, "This"), FakeWord(0.7, 1.1, "is"),
            FakeWord(1.2, 1.6, "fire"), FakeWord(1.7, 2.2, "money")]


@requires_ffmpeg
def test_noop_returns_none(make_video, tmp_path):
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    opts = ProcessingOptions(captions=False)  # nothing enabled
    result = compositor.render_clip(base, tmp_path / "out.mp4", opts, _words(), tmp_path)
    assert result is None


@requires_ffmpeg
def test_all_effects_single_pass(make_video, png_asset, tmp_path):
    base = make_video("base.mp4", duration=3.0, w=1080, h=1920)
    asset = png_asset("e.png")
    opts = ProcessingOptions(
        captions=True, hook_title=True, color="vivid", zoom=True, transitions=True,
        fades=True, progress_bar=True, emoji="heavy", music="chill",
        caption_template="boxed", caption_position="bottom",
    )
    result = compositor.render_clip(
        base, tmp_path / "all.mp4", opts, _words(), tmp_path,
        hook_text="WAIT FOR IT", emoji_resolver=lambda c: asset,
    )
    assert result is not None
    applied = result.effects_applied
    for fx in ("captions", "hook_title", "color:vivid", "zoom", "transitions",
               "fades", "progress_bar", "emoji:heavy", "music:chill"):
        assert fx in applied
    assert result.path.exists()
    assert probe_size(result.path) == (1080, 1920)


@requires_ffmpeg
def test_music_only_copies_video(make_video, tmp_path):
    base = make_video("base.mp4", duration=2.0, w=640, h=360)
    opts = ProcessingOptions(captions=False, music="upbeat")
    result = compositor.render_clip(base, tmp_path / "m.mp4", opts, _words(), tmp_path)
    assert result is not None
    assert result.effects_applied == ["music:upbeat"]
    assert probe_duration(result.path) > 1.5


@requires_ffmpeg
def test_captions_only(make_video, tmp_path):
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    opts = ProcessingOptions(captions=True)
    result = compositor.render_clip(base, tmp_path / "c.mp4", opts, _words(), tmp_path)
    assert result is not None
    assert "captions" in result.effects_applied



# --------------------------------------------------------------------------- #
# Task 6 — B-roll single-pass compositor integration (6.5, 6.6, 6.7)
# --------------------------------------------------------------------------- #
import re

from worker.effects.broll import AssetRef, BrollCue


def _spy_run(monkeypatch):
    """Wrap ``compositor._run`` to record every ffmpeg command (calls through)."""
    calls: list[list[str]] = []
    real = compositor._run

    def _wrapper(cmd):
        calls.append(list(cmd))
        return real(cmd)

    monkeypatch.setattr(compositor, "_run", _wrapper)
    return calls


def _image_resolver(png_path, keyword="fire", start=0.4, end=1.4):
    """A b-roll resolver returning a single resolved (image) cue."""
    asset = AssetRef(path=str(png_path), kind="image", provider="local",
                     license="local")
    return lambda: [BrollCue(start, end, keyword, asset=asset)]


def _maps_and_codecs(cmd):
    """Extract the ordered (-map / -c:v / -c:a) argument pairs from a command."""
    out = []
    i = 0
    while i < len(cmd):
        if cmd[i] in ("-map", "-c:v", "-c:a"):
            out.append((cmd[i], cmd[i + 1]))
            i += 2
        else:
            i += 1
    return out


@requires_ffmpeg
def test_broll_single_pass_with_captions_and_emoji(make_video, png_asset,
                                                   monkeypatch, tmp_path):
    """Validates: Requirements 10.1, 10.3, 17.1 — one ffmpeg pass, distinct indices."""
    base = make_video("base.mp4", duration=3.0, w=1080, h=1920)
    emoji_png = png_asset("emoji.png", color="red")
    broll_png = png_asset("broll.png", color="blue")

    calls = _spy_run(monkeypatch)
    opts = ProcessingOptions(captions=True, emoji="heavy")
    result = compositor.render_clip(
        base, tmp_path / "single.mp4", opts, _words(), tmp_path,
        emoji_resolver=lambda c: emoji_png,
        broll_resolver=_image_resolver(broll_png, keyword="fire"),
    )

    assert result is not None
    assert result.path.exists()
    # Exactly ONE ffmpeg invocation for the render (Reqs 10.1, 17.1).
    assert len(calls) == 1
    cmd = calls[0]

    # b-roll marker composited and provenance recorded (Reqs 9.4, 12.2).
    assert "broll:fire" in result.effects_applied
    assert result.broll_records and result.broll_records[0]["keyword"] == "fire"

    # Distinct, collision-free input indices in the filter graph (Req 10.3).
    fc = cmd[cmd.index("-filter_complex") + 1]
    non_base = [int(m) for m in re.findall(r"\[(\d+):v\]", fc) if int(m) != 0]
    assert non_base, "expected overlay inputs referenced in the graph"
    assert len(non_base) == len(set(non_base))  # no index collisions
    # One base input + one input per distinct overlay asset.
    assert cmd.count("-i") == 1 + len(set(non_base))


@requires_ffmpeg
def test_zero_resolvable_cues_equals_broll_disabled(make_video, monkeypatch,
                                                    tmp_path):
    """Validates: Requirements 9.3 — no resolvable assets == b-roll disabled.

    Property 18 (as an integration example): enabling b-roll with an empty
    resolved-cue list yields the same maps/codecs as rendering with b-roll off.
    """
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)
    opts = ProcessingOptions(captions=True)

    calls = _spy_run(monkeypatch)
    # b-roll "enabled" but the resolver yields nothing resolvable.
    with_broll = compositor.render_clip(
        base, tmp_path / "with.mp4", opts, _words(), tmp_path,
        broll_resolver=lambda: [],
    )
    disabled = compositor.render_clip(
        base, tmp_path / "without.mp4", opts, _words(), tmp_path,
    )

    assert with_broll is not None and disabled is not None
    assert len(calls) == 2
    # Same stream maps + codecs => rendered identically to b-roll disabled.
    assert _maps_and_codecs(calls[0]) == _maps_and_codecs(calls[1])
    assert with_broll.effects_applied == disabled.effects_applied
    assert with_broll.broll_records == []


@requires_ffmpeg
def test_stream_copy_and_noop_contract(make_video, monkeypatch, tmp_path):
    """Validates: Requirements 17.2, 17.3 — audio stream-copy + None no-op."""
    base = make_video("base.mp4", duration=2.0, w=1080, h=1920)

    # Only video changes (captions) -> audio must be stream-copied.
    calls = _spy_run(monkeypatch)
    result = compositor.render_clip(
        base, tmp_path / "vidonly.mp4", ProcessingOptions(captions=True),
        _words(), tmp_path,
    )
    assert result is not None
    cmd = calls[-1]
    assert ("-c:a", "copy") in _maps_and_codecs(cmd)  # unmodified audio copied

    # Everything off (even with a b-roll resolver that yields nothing) -> None.
    noop = compositor.render_clip(
        base, tmp_path / "noop.mp4", ProcessingOptions(captions=False),
        _words(), tmp_path, broll_resolver=lambda: [],
    )
    assert noop is None


# --------------------------------------------------------------------------- #
# av-engines-foundation, task 14 — the engine input-index seam
# ---------------------------------------------------------------------------
# ``Engine_Context.first_input_index`` (host) + the reserved engine input block
# (compositor) close the API gap noted in ``tests/test_pipeline_effects.py``: an
# engine can now know which ffmpeg ``-i`` index its own inputs land on, because
# the host reserves one contiguous block of ``AV_Engine.max_inputs`` indices per
# contributing engine immediately after the base clip, in registry
# ``(priority, engine_id)`` order.
#
# These two tests do not need real ffmpeg: ``probe``/``_run`` are stubbed so the
# assertions are on the argv and the ``-filter_complex`` string the compositor
# builds, which is exactly what the seam is about.
# --------------------------------------------------------------------------- #
from pathlib import Path as _Path
from types import SimpleNamespace as _Namespace

from tests.fakes import FakeClock, StaticProber
from worker.engines.base import (
    AV_Engine,
    Compose_Contribution,
    Compose_Input,
    Engine_Result,
    Engine_Stage,
    Engine_Status,
)
from worker.engines.capabilities import Capability_Report
from worker.engines.host import Engine_Host
from worker.engines.registry import Engine_Registry


class Input_Claiming_Engine(AV_Engine):
    """A COMPOSE engine that declares inputs and reads its reserved block start.

    Deliberately *not* a ``FakeEngine``: the point of this double is that its
    contribution is built inside ``run`` from ``ctx.first_input_index``, which is
    how a real engine will write ``[N:v]`` labels for its own inputs.
    """

    def __init__(self, engine_id, paths, *, priority=100, z_order=0):
        self.engine_id = str(engine_id)
        self.stage = Engine_Stage.COMPOSE
        self.priority = priority
        self.max_inputs = len(paths)          # this engine's reserved block size
        self.z_order = z_order
        self._paths = tuple(_Path(p) for p in paths)
        self.contexts: list = []

    def flag_field(self) -> str:
        return f"{self.engine_id}_enabled"

    def resolve_options(self, options):
        return options

    def plan(self, ctx):
        return {}

    def run(self, ctx) -> Engine_Result:
        self.contexts.append(ctx)
        # The label an engine can only write because the host published its index.
        label = f"[{ctx.first_input_index}:v]"
        return Engine_Result(
            engine_id=self.engine_id,
            status=Engine_Status.APPLIED,
            contribution=Compose_Contribution(
                engine_id=self.engine_id,
                inputs=tuple(
                    Compose_Input(path=path, loop=True, duration=1.0)
                    for path in self._paths
                ),
                # Index-free filter text carrying the claimed label so the test can
                # see WHICH engine's filter landed where in the graph.
                video_filters=(f"drawtext=text='{self.engine_id}{label}'",),
                z_order=self.z_order,
            ),
        )


def _stub_ffmpeg(monkeypatch, *, has_audio=True, duration=3.0, w=1080, h=1920):
    """Stub ``probe``/``_run`` so the compositor's argv is inspectable offline."""
    from worker.ffmpeg_utils import MediaInfo

    info = MediaInfo(duration=duration, width=w, height=h, fps=30.0,
                     has_audio=has_audio)
    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append([str(part) for part in cmd])
        _Path(str(cmd[-1])).write_bytes(b"stub-render")
        return None

    monkeypatch.setattr(compositor, "probe", lambda path: info)
    monkeypatch.setattr(compositor, "_run", fake_run)
    return calls


def _input_paths(cmd):
    """The ``-i`` operands of an ffmpeg argv, in index order."""
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]


def test_engine_inputs_land_at_the_host_reserved_indices(tmp_path, monkeypatch):
    """Validates: Requirements 1.5, 10.3, 23.3

    Two enabled COMPOSE engines declare 1 and 2 inputs. The host hands the first
    ``first_input_index == 1`` (immediately after the base clip) and the second
    ``first_input_index == 2`` (1 + the first engine's ``max_inputs``), and each
    engine's input really lands on that ffmpeg ``-i`` index. Filters keep layering
    by ``(z_order, engine_id)`` while inputs follow registry order, so the two
    orderings are shown to be genuinely decoupled.
    """
    base = tmp_path / "geo.mp4"
    base.write_bytes(b"stub-clip")
    alpha_input = tmp_path / "alpha0.png"
    beta_inputs = [tmp_path / "beta0.png", tmp_path / "beta1.png"]
    for path in [alpha_input, *beta_inputs]:
        path.write_bytes(b"stub-png")

    # Registry order is (priority, engine_id): alpha (10) then beta (20).
    # z_order is the reverse, so filter order must NOT follow input order.
    alpha = Input_Claiming_Engine("alpha_engine", [alpha_input], priority=10, z_order=9)
    beta = Input_Claiming_Engine("beta_engine", beta_inputs, priority=20, z_order=1)
    registry = Engine_Registry()
    registry.register(alpha)
    registry.register(beta)

    options = _Namespace(alpha_engine_enabled=True, beta_engine_enabled=True,
                         permissibility_mode=False)
    host = Engine_Host(
        options, job_id="job1", temp_dir=tmp_path / "tmp", registry=registry,
        capabilities=Capability_Report(StaticProber({})), clock=FakeClock(),
    )
    outcome = host.run_stage(
        Engine_Stage.COMPOSE, clip_id="01_abc123", source=tmp_path / "src.mp4",
        clip_path=base, clip_start=0.0, clip_end=3.0, duration=3.0,
    )

    # The reserved block starts, known BEFORE run() and published to the engine.
    assert alpha.contexts[0].first_input_index == 1
    assert beta.contexts[0].first_input_index == 2
    # Contributions arrive in registry order — what the reservation is built from.
    assert [c.engine_id for c in outcome.contributions] == ["alpha_engine",
                                                            "beta_engine"]

    calls = _stub_ffmpeg(monkeypatch)
    result = compositor.render_clip(
        base, tmp_path / "out.mp4", ProcessingOptions(captions=False), [],
        tmp_path / "work", engine_contributions=outcome.contributions,
    )
    assert result is not None
    assert len(calls) == 1                     # still exactly one ffmpeg pass
    cmd = calls[0]

    inputs = _input_paths(cmd)
    assert inputs == [str(base), str(alpha_input), str(beta_inputs[0]),
                      str(beta_inputs[1])]
    # Each engine's first input sits on exactly the index it was promised.
    assert inputs.index(str(alpha_input)) == alpha.contexts[0].first_input_index
    assert inputs.index(str(beta_inputs[0])) == beta.contexts[0].first_input_index
    # ... and the second engine's block really accounts for the first's size.
    assert (beta.contexts[0].first_input_index
            == alpha.contexts[0].first_input_index + alpha.max_inputs)

    # Filters layer by (z_order, engine_id): beta (z=1) below alpha (z=9), which is
    # the OPPOSITE of the input order. The two orderings decouple by design.
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.index("beta_engine[2:v]") < graph.index("alpha_engine[1:v]")

    host.finish_clip("01_abc123")


def test_zero_contribution_engine_block_preserves_v080_input_indices(tmp_path,
                                                                     monkeypatch):
    """Validates: Requirements 23.1, 23.3 — the reserved block is inert when empty.

    The parity guarantee of the seam: with no engine contribution the block is
    empty, so music stays on input index 1 (``[1:a]`` in the mix filter), b-roll
    starts at 2 and emoji after it — byte-identically to v0.8.0 — and passing
    ``None``, ``[]`` or a contribution-free sequence produces the very same argv.
    """
    base = tmp_path / "base.mp4"
    base.write_bytes(b"stub-clip")
    music = tmp_path / "music.m4a"
    music.write_bytes(b"stub-music")
    emoji_png = tmp_path / "emoji.png"
    emoji_png.write_bytes(b"stub-png")

    monkeypatch.setattr(compositor.audio, "resolve_music",
                        lambda mood, duration, temp_dir: music)
    calls = _stub_ffmpeg(monkeypatch)

    opts = ProcessingOptions(captions=True, music="chill", emoji="heavy",
                             caption_template="boxed")

    def render(dest, contributions):
        return compositor.render_clip(
            base, tmp_path / dest, opts, _words(), tmp_path / "work",
            emoji_resolver=lambda char: emoji_png,
            engine_contributions=contributions,
        )

    legacy = render("legacy.mp4", None)          # every v0.8.0 caller
    empty = render("empty.mp4", [])              # host with no contribution
    none_left = render("nones.mp4", [None])      # hostile/empty sequence

    assert legacy is not None and empty is not None and none_left is not None
    assert len(calls) == 3

    def canonical(cmd):
        return [part.replace("legacy.mp4", "<out>").replace("empty.mp4", "<out>")
                .replace("nones.mp4", "<out>") for part in cmd]

    assert canonical(calls[1]) == canonical(calls[0])
    assert canonical(calls[2]) == canonical(calls[0])

    # The pinned v0.8.0 index accounting: base 0, music 1, emoji from 2.
    inputs = _input_paths(calls[0])
    assert inputs[0] == str(base)
    assert inputs[1] == str(music)
    assert inputs[2:] == [str(emoji_png)] * (len(inputs) - 2)
    graph = calls[0][calls[0].index("-filter_complex") + 1]
    assert "[1:a]volume=" in graph                     # music label unchanged
    assert "[2:v]" in graph                            # first emoji input at 2
