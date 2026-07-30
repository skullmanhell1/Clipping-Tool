"""Delivered audio is levelled, ducked and normalised in shape (AU1, AU2, AU8, O4).

Verified absent across the whole repo before this: no ``loudnorm``, no ``dynaudnorm``, no
``sidechaincompress``, no LUFS target, no ``-ar``/``-ac``, no bitrate ceiling. Music was
mixed at a flat ``volume=0.12``.

Each of those has a consequence that only shows up after the file leaves us:

* **AU1** — a clip quieter than the platform's target is turned *up* on playback, which
  lifts its noise floor along with the speech. One that is louder is turned down, losing the
  headroom it was mastered with. Either way the creator stops controlling the result.
* **AU2** — a flat bed has no good level. Loud enough to hear between sentences is loud
  enough to fight the speech during them; quiet enough not to fight it is inaudible, which
  is no music at the cost of an extra encode.
* **AU8** — output sample rate and channel count were whatever the source happened to be.
  A mono clip plays out of one side on some players; a 5.1 layout gets downmixed by whatever
  decoder sees it first, if at all.
* **O4** — ``-crf`` is a quality target with no bitrate ceiling, so a busy clip can balloon
  past a platform's file-size limit and be rejected on upload.

The loudness tests measure the *rendered file* with a second ``loudnorm`` analysis pass
rather than checking that the filter string was assembled. A filter can be present and
ineffective — overridden later in the chain, applied to the wrong label, or fed a
measurement from the wrong file — and every one of those still looks correct in a
command-line assertion.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from config import settings as app_settings
from worker.effects import audio
from worker.ffmpeg_utils import aac_args, h264_args

FFMPEG = shutil.which(app_settings.ffmpeg_binary) or shutil.which("ffmpeg")
FFPROBE = shutil.which(app_settings.ffprobe_binary) or shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="no ffmpeg/ffprobe on PATH; audio mastering checks need both",
)


def _make_tone(path, *, freq=300, seconds=4.0, volume=1.0, rate=44100, channels=1):
    """A tone at a known level, deliberately *not* at the output's rate/layout."""
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=f={freq}:d={seconds}:sample_rate={rate}",
            "-af", f"volume={volume}", "-ac", str(channels), "-y", str(path),
        ],
        check=True, capture_output=True, timeout=120,
    )
    return path


def _probe_audio(path) -> dict[str, str]:
    proc = subprocess.run(
        [
            FFPROBE, "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=sample_rate,channels,codec_name",
            "-of", "default=nw=1", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


# --------------------------------------------------------------------------- #
# AU1 — loudness                                                               #
# --------------------------------------------------------------------------- #
def test_platform_targets_differ_and_fall_back_to_the_configured_default():
    """The targets are per platform because the platforms are.

    YouTube normalises to roughly -14 LUFS while TikTok and Instagram sit nearer -11, so a
    single number would be wrong for one of them by ~3 LU — clearly audible.
    """
    assert audio.platform_loudness_target("youtube") == -14.0
    assert audio.platform_loudness_target("tiktok") == -11.0
    assert audio.platform_loudness_target("instagram") == -11.0
    # Unknown, empty and oddly-cased inputs resolve rather than raising.
    assert audio.platform_loudness_target("whop") == app_settings.loudness_target_lufs
    assert audio.platform_loudness_target("") == app_settings.loudness_target_lufs
    assert audio.platform_loudness_target("  YouTube ") == -14.0


@requires_ffmpeg
@pytest.mark.real_binary
def test_measure_loudness_reads_a_real_level(tmp_path):
    quiet = _make_tone(tmp_path / "quiet.wav", volume=0.05)
    stats = audio.measure_loudness(quiet)
    assert stats is not None
    # A tone at 5% amplitude is far below any platform target; the exact figure is ffmpeg's
    # business, but it must be in the right region rather than a parsed zero.
    assert -60.0 < stats.input_i < -30.0, stats
    assert stats.input_tp < 0.0


@requires_ffmpeg
@pytest.mark.real_binary
def test_measure_loudness_returns_none_instead_of_raising(tmp_path):
    """A clip must never fail because its loudness could not be measured.

    The caller degrades to rendering at the source's own level, which is what the
    ``loudness_degraded:unmeasurable`` marker records.
    """
    missing = tmp_path / "nope.wav"
    assert audio.measure_loudness(missing) is None

    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"this is not audio")
    assert audio.measure_loudness(corrupt) is None

    silent_video = tmp_path / "novideo.mp4"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=black:s=64x64:d=1", "-y", str(silent_video)],
        check=True, capture_output=True, timeout=120,
    )
    assert audio.measure_loudness(silent_video) is None


@requires_ffmpeg
@pytest.mark.real_binary
@pytest.mark.parametrize("platform", ["youtube", "tiktok"])
def test_a_quiet_clip_is_normalised_to_the_platform_target(platform, tmp_path):
    """AU1 end to end: measure, apply, then measure the result.

    Both passes use ffmpeg's own analysis, so this asserts the delivered loudness rather
    than the presence of a filter.
    """
    source = _make_tone(tmp_path / "src.wav", volume=0.05)
    before = audio.measure_loudness(source)
    assert before is not None

    target = audio.platform_loudness_target(platform)
    out = tmp_path / f"{platform}.wav"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-af", audio.loudnorm_filter(before, target), "-y", str(out)],
        check=True, capture_output=True, timeout=180,
    )

    after = audio.measure_loudness(out)
    assert after is not None
    assert abs(after.input_i - target) < 1.0, (
        f"{platform}: {before.input_i:.2f} LUFS -> {after.input_i:.2f}, wanted {target}"
    )
    # And the true-peak ceiling is respected, which is the other half of AU1: a clip that
    # hits 0 dBFS will clip in the lossy encoder even at the right loudness.
    assert after.input_tp <= app_settings.loudness_true_peak_db + 0.5, after


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_loud_clip_is_brought_down_not_only_up(tmp_path):
    """Normalisation is two-directional; only testing quiet input would miss half of it.

    Pink noise rather than a tone, because loudness is K-weighted: a 300 Hz sine at 0.9
    amplitude measures about -23 LUFS, so "high amplitude" and "loud" are not the same
    thing and a tone cannot easily be made to sit above a platform target at all.

    The target is chosen *below* the source rather than fixed at a platform value, because
    what this test is about is the direction of correction. Pinning it to -14 would make the
    test depend on manufacturing a fixture louder than YouTube's target, which is a fact
    about the fixture generator rather than about normalisation.
    """
    source = tmp_path / "loud.wav"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anoisesrc=c=pink:a=0.8:d=4", "-y", str(source)],
        check=True, capture_output=True, timeout=120,
    )
    before = audio.measure_loudness(source)
    assert before is not None

    target = before.input_i - 8.0          # unambiguously quieter than the source
    out = tmp_path / "quieter.wav"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-af", audio.loudnorm_filter(before, target), "-y", str(out)],
        check=True, capture_output=True, timeout=180,
    )
    after = audio.measure_loudness(out)
    assert after is not None
    assert after.input_i < before.input_i, "a clip above the target must be turned down"
    assert abs(after.input_i - target) < 1.0, (
        f"{before.input_i:.2f} LUFS -> {after.input_i:.2f}, wanted {target:.2f}"
    )


# --------------------------------------------------------------------------- #
# AU2 — ducking                                                                #
# --------------------------------------------------------------------------- #
def test_the_duck_graph_splits_the_speech_and_keys_the_compressor():
    """The speech must be both the sidechain key and part of the mix.

    A filter output cannot be consumed twice, so without ``asplit`` the graph either fails
    to build or silently drops the speech from the mix — which would be a clip of ducked
    music and nothing else.
    """
    graph = audio.music_mix_filter("0:a", "1:a", "aout", 0.12, 5.0, duck=True)
    assert "asplit=2" in graph
    assert "sidechaincompress=" in graph
    # The compressor's main input is the bed and its key is the speech, in that order:
    # reversed, it would duck the speech under the music.
    assert "[bedv][sckey]sidechaincompress=" in graph
    assert graph.endswith("[aout]")


def test_ducking_can_be_turned_off_and_restores_the_flat_mix():
    flat = audio.music_mix_filter("0:a", "1:a", "aout", 0.12, 5.0, duck=False)
    assert "sidechaincompress" not in flat
    assert "asplit" not in flat
    assert "amix=inputs=2:duration=first:normalize=0[aout]" in flat


def test_a_ratio_of_one_disables_ducking_even_when_asked_for(monkeypatch):
    """``music_duck_ratio=1.0`` is documented as "no ducking"; 1:1 compression is a no-op.

    Emitting the filter anyway would add a pass over the audio to achieve nothing.
    """
    monkeypatch.setattr(audio.settings, "music_duck_ratio", 1.0)
    graph = audio.music_mix_filter("0:a", "1:a", "aout", 0.12, 5.0, duck=True)
    assert "sidechaincompress" not in graph


def test_fades_still_apply_on_both_paths():
    """Fades and ducking are independent; adding one must not drop the other."""
    for duck in (True, False):
        graph = audio.music_mix_filter("0:a", "1:a", "aout", 0.12, 5.0, fade=True, duck=duck)
        assert graph.count("afade=t=in") == 2, graph    # bed and speech
        assert graph.count("afade=t=out") == 2, graph


@requires_ffmpeg
@pytest.mark.real_binary
def test_the_duck_graph_runs_and_lowers_the_bed_under_speech(tmp_path):
    """Built by us, executed by ffmpeg, and the effect measured.

    The comparison is the *same instant* rendered twice, once with ducking and once without,
    rather than one render compared across time. Two reasons: the speech contributes to any
    window it occupies, so a with/without pair cancels it out; and ``sidechaincompress``
    releases slowly enough that a window shortly after the speech is still partly ducked —
    an across-time assertion would be measuring the release envelope, which is a tuning
    choice rather than the behaviour under test.
    """
    speech = tmp_path / "speech.wav"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=f=300:d=3:sample_rate=48000",
         # silent for 1s, loud for 1s, silent for 1s
         "-af", "volume='if(between(t,1,2),1.0,0.0)':eval=frame",
         "-y", str(speech)],
        check=True, capture_output=True, timeout=120,
    )
    bed = _make_tone(tmp_path / "bed.wav", freq=800, seconds=3.0, rate=48000)

    def _render(duck: bool):
        out = tmp_path / f"mixed_{int(duck)}.wav"
        graph = audio.music_mix_filter("0:a", "1:a", "aout", 0.9, 3.0, duck=duck)
        subprocess.run(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(speech), "-i", str(bed),
             "-filter_complex", graph, "-map", "[aout]", "-y", str(out)],
            check=True, capture_output=True, timeout=180,
        )
        assert out.exists() and out.stat().st_size > 0
        return out

    def _bed_level(path, start: float, end: float) -> float:
        """Mean volume of the bed's frequency band in a window, in dB.

        ``highpass=f=600`` keeps the 800 Hz bed and rejects the 300 Hz speech, so this
        measures the bed even while someone is talking over it.
        """
        proc = subprocess.run(
            [FFMPEG, "-nostdin", "-hide_banner", "-i", str(path),
             "-af", f"atrim=start={start}:end={end},highpass=f=600,volumedetect",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        for line in proc.stderr.splitlines():
            if "mean_volume:" in line:
                return float(line.split("mean_volume:")[1].strip().split()[0])
        raise AssertionError(f"no mean_volume in ffmpeg output:\n{proc.stderr[-800:]}")

    ducked = _bed_level(_render(True), 1.2, 1.8)
    flat = _bed_level(_render(False), 1.2, 1.8)

    assert ducked < flat - 1.0, (
        f"bed measured {ducked:.1f} dB under speech with ducking on and {flat:.1f} dB with "
        "it off; the sidechain is not attenuating the bed"
    )


# --------------------------------------------------------------------------- #
# AU8 / O4 — the shape of the delivered file                                    #
# --------------------------------------------------------------------------- #
def test_audio_arguments_pin_rate_and_layout():
    args = aac_args()
    assert args[args.index("-ar") + 1] == str(app_settings.output_sample_rate)
    assert args[args.index("-ac") + 1] == str(app_settings.output_channels)


def test_the_bitrate_ceiling_is_opt_in_and_paired_with_a_buffer():
    """``-maxrate`` without ``-bufsize`` is ignored by libx264, so they travel together."""
    assert "-maxrate" not in h264_args()

    delivered = h264_args(vbv_cap=True)
    maxrate = int(app_settings.output_max_bitrate_kbps)
    assert delivered[delivered.index("-maxrate") + 1] == f"{maxrate}k"
    assert delivered[delivered.index("-bufsize") + 1] == f"{maxrate * 2}k"


@requires_ffmpeg
@pytest.mark.real_binary
def test_a_44100_mono_source_is_delivered_as_48000_stereo(tmp_path):
    """AU8 against a source that is deliberately neither.

    A 48 kHz stereo fixture would come out 48 kHz stereo with the flags absent, so the test
    would pass without testing anything.
    """
    source = _make_tone(tmp_path / "mono441.wav", rate=44100, channels=1)
    probed_source = _probe_audio(source)
    assert probed_source["sample_rate"] == "44100"
    assert probed_source["channels"] == "1"

    out = tmp_path / "delivered.m4a"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
         *aac_args(), "-y", str(out)],
        check=True, capture_output=True, timeout=180,
    )
    probed = _probe_audio(out)
    assert probed["sample_rate"] == str(app_settings.output_sample_rate)
    assert probed["channels"] == str(app_settings.output_channels)
    assert probed["codec_name"] == "aac"
