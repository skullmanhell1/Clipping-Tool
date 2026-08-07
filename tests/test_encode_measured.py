"""Measured encode settings (O16, O17, O20).

The headline result of this batch is that **two of the three defaults did not move**, and these
tests exist to keep them from moving by accident later.

That is not timidity. R4.3 says change the preset default *only if* the measurement shows a
fidelity improvement, and R5.6 says that if the measurement cannot distinguish the scaler
candidates, keep the default and record the finding. The measurements came back contradictory and
null respectively, so keeping the defaults **is** the deliverable.

What did change is the thing the requirements ask for independently of any measurement: the
resampling algorithm is now stated explicitly, uniformly, and configurably (R5.1-R5.3). With three
passes and nine scaling sites, two stages resampling differently produces compounding softness that
cannot be attributed to any one stage — and that is true whichever algorithm wins.
"""

from __future__ import annotations

import pytest

from config import settings
from worker import output_profiles
from worker.ffmpeg_utils import (
    DEFAULT_SCALER_FLAGS,
    SCALER_FLAGS,
    aac_args,
    h264_args,
    scaler_args,
)

# --- O16: the preset default did not move, and that is the finding -------------------------


def test_the_preset_default_is_still_veryfast():
    """R4.3. The measurement did not support a change, so the default stays.

    Measured on a 4K->1080x1920 downscale at fixed CRF 20, against the same filter graph at
    slow/crf12:

        source A (testsrc2)      faster +1.137  fast +0.677  medium +0.822  VMAF
        source B (noise+bars)    faster -0.586  fast -0.185  medium -0.209  VMAF

    Opposite directions on two sources. What slower presets reliably produced was a **smaller
    file** at equal CRF (-1.8 MB on source B), which is what x264 presets actually trade and is
    not a fidelity gain.

    Pinned so that a future change has to come with its own measurement and its own commit
    (R4.4), rather than arriving as a plausible-sounding tidy-up.
    """
    assert settings.x264_preset == "veryfast"


def test_the_crf_default_is_untouched_by_this_work():
    """R4.6. CRF and preset interact, so moving both makes neither attributable."""
    assert settings.x264_crf == 20


def test_the_preset_still_flows_through_the_single_builder():
    """R4.8. An encoder setting that reached eight of nine sites is how this class of bug starts."""
    assert "-preset" in h264_args()
    assert settings.x264_preset in h264_args()


# --- O17: explicit, uniform, configurable -------------------------------------------------


def test_every_encode_states_its_resampling_algorithm_explicitly():
    """R5.1. Before this, every `scale=` ran swscale's unstated default."""
    args = h264_args()
    assert "-sws_flags" in args
    assert args[args.index("-sws_flags") + 1] == DEFAULT_SCALER_FLAGS


def test_the_flags_come_first_so_they_apply_to_the_whole_invocation():
    """R5.3, and the reason this lives in the shared builder rather than at nine call sites.

    One global `-sws_flags` covers every `scale=` in the graph, which makes "identical flags on
    every scale in a job" true by construction. Threading a per-filter `flags=` argument to nine
    sites would make it true by review, and the failure mode of reaching eight is silent.
    """
    assert h264_args()[:2] == ["-sws_flags", DEFAULT_SCALER_FLAGS]


def test_the_default_is_swscales_own_default_so_no_pixels_move():
    """The measured outcome, not caution.

    `lanczos` -- the algorithm usually recommended for downscaling -- came in at VMAF -0.07,
    SSIM +0.0009, PSNR +0.03 against bicubic on the exact 4K->1080x1920 case R5.5 nominates.
    That is noise. R5.6 says keep the default and record the finding.

    It also means no golden or parity fixture needed re-freezing, which is worth having.
    """
    assert DEFAULT_SCALER_FLAGS == "bicubic"
    assert settings.scaler_flags == "bicubic"


@pytest.mark.parametrize("algorithm", SCALER_FLAGS)
def test_every_offered_algorithm_is_accepted(algorithm, monkeypatch):
    """R5.2. The setting has to actually work, including for the ones nobody should pick."""
    monkeypatch.setattr(type(settings), "scaler_flags", algorithm, raising=False)
    monkeypatch.setattr(settings, "scaler_flags", algorithm, raising=False)
    assert scaler_args() == ["-sws_flags", algorithm]


def test_an_unrecognised_algorithm_falls_back_rather_than_failing_the_job(monkeypatch):
    """swscale errors out on an unknown name, which would turn a typo into a failed render.

    Same reasoning as the tone-mapping operator resolver: a mistyped setting must degrade to the
    documented default, not destroy the job it was set on.
    """
    monkeypatch.setattr(settings, "scaler_flags", "definitely-not-a-scaler", raising=False)
    assert scaler_args() == ["-sws_flags", DEFAULT_SCALER_FLAGS]


def test_an_empty_setting_falls_back_too(monkeypatch):
    monkeypatch.setattr(settings, "scaler_flags", "", raising=False)
    assert scaler_args() == ["-sws_flags", DEFAULT_SCALER_FLAGS]


def test_neighbor_is_offered_and_documented_as_a_bad_choice():
    """The most interesting measurement in the batch, kept where it will be read.

    `neighbor` scored **+0.75 VMAF** while losing **1.02 dB PSNR** and 0.005 SSIM. That is VMAF
    being fooled by nearest-neighbour's false sharpening, and it is the clearest evidence in this
    repository that a single metric is not a verdict — which matters because the whole point of
    M9 was to let numbers settle arguments.
    """
    assert "neighbor" in SCALER_FLAGS
    # The reasoning lives in a module comment rather than a docstring, because a tuple cannot
    # carry one -- so this asserts on the source instead of pretending otherwise.
    import inspect

    from worker import ffmpeg_utils

    source = inspect.getsource(ffmpeg_utils)
    assert "fooled by nearest-neighbour" in source or "false sharpening" in source


def test_the_uniformity_requirement_is_independent_of_which_algorithm_wins():
    """Stated as a test because it is the part most likely to be argued away.

    "The measurement was null, so why set the flag at all?" — because R5.3 is about two stages
    agreeing with each other, not about which algorithm is best. Compounding softness across
    three passes is a defect even when every pass individually uses a good algorithm.
    """
    first = h264_args()
    second = h264_args()
    assert first[:2] == second[:2], "every invocation in a job must resample identically"


# --- O20: audio bitrate is configurable and unchanged -------------------------------------


def test_the_audio_bitrate_is_now_configurable(monkeypatch):
    """R7.1, R7.2. It was hard-coded at `128k` in a single string."""
    monkeypatch.setattr(settings, "audio_bitrate_kbps", 192, raising=False)
    assert "192k" in aac_args()


def test_the_audio_default_is_unchanged_because_nothing_measured_it():
    """R7.5, honestly.

    M9's three metrics — SSIM, PSNR, VMAF — are all *image* metrics. There is no instrument in
    this repository that can compare 128k against 192k, so moving the default would be exactly
    the unmeasured substitution O16's measurement argued against. The setting is the honest
    deliverable; the default is not a claim.
    """
    assert settings.audio_bitrate_kbps == 128
    assert "128k" in aac_args()


def test_au8s_sample_rate_and_channel_normalisation_are_untouched():
    """R7.4. Those exist because mono plays from one side and surround gets silently downmixed."""
    args = aac_args()
    assert "-ar" in args and str(int(settings.output_sample_rate)) in args
    assert "-ac" in args and str(int(settings.output_channels)) in args


def test_the_bitrate_resolver_mirrors_the_video_one():
    """Two settings that look alike must behave alike.

    `resolve_audio_bitrate_kbps` deliberately copies `resolve_max_bitrate_kbps`' shape. Stating
    the rule twice differently is how two lookalike settings come to diverge, which is the kind of
    surprise nobody debugs quickly.
    """
    assert output_profiles.resolve_audio_bitrate_kbps() == settings.audio_bitrate_kbps


def test_an_intermediate_never_carries_more_audio_bitrate_than_the_final(monkeypatch):
    """R7.6. Holds by construction: every pass reads the same resolver."""
    monkeypatch.setattr(settings, "audio_bitrate_kbps", 160, raising=False)
    assert aac_args().count("160k") == 1
    assert output_profiles.resolve_audio_bitrate_kbps() == 160


def test_a_zero_or_absent_bitrate_falls_back_to_the_default(monkeypatch):
    """A misconfigured `0` would produce a silent or rejected track."""
    monkeypatch.setattr(settings, "audio_bitrate_kbps", 0, raising=False)
    assert output_profiles.resolve_audio_bitrate_kbps() == 128


# --- the measurement is committed, not just described --------------------------------------


def test_the_preset_and_scaler_measurements_are_committed():
    """R4.7, R5.6, R13.11. "No measurable difference, kept the default" is a result.

    Committed so the next person does not re-run the same experiment — which is the specific waste
    the spec asks to prevent, and the reason a null result has to be written down rather than
    simply acted upon by doing nothing.
    """
    import json
    from pathlib import Path

    path = Path("eval/baselines/encode_presets_v0.11.0.json")
    assert path.is_file(), "the measured table must be committed, including its null results"
    data = json.loads(path.read_text())
    assert "presets" in data and "scalers" in data
    assert data["preset_decision"]["changed"] is False
    assert data["scaler_decision"]["changed"] is False
    # The contradiction is the finding; it must survive in the record rather than being smoothed.
    assert "contradict" in json.dumps(data).lower()
