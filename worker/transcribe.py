"""Transcription via faster-whisper.

Produces segment- and word-level timestamps used downstream for segmentation
and caption rendering. Runs on CPU by default (model size configurable via
``settings.whisper_model``) and uses a GPU automatically when available and
requested (``settings.whisper_device``).

The whisper model is loaded lazily and cached process-wide, since loading is
expensive relative to inference.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class Word:
    """A single transcribed word with timing."""

    start: float
    end: float
    text: str
    probability: float = 1.0


@dataclass
class TranscriptSegment:
    """A timestamped transcript segment containing zero or more words."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    # T3: Whisper's own two indicators that a segment may be invented, previously discarded.
    # ``no_speech_prob`` is the model's estimate that the audio contains no speech at all -
    # exactly the condition under which it hallucinates - and ``avg_logprob`` is its mean token
    # confidence. Defaulted and appended so existing positional construction keeps working;
    # 0.0 for both reads as "no reason to doubt this", which is the right default for a
    # segment built by hand in a test.
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


@dataclass
class Transcript:
    """A full transcript: language + ordered segments."""

    language: str
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Return the full transcript text (segments joined by spaces)."""
        return " ".join(s.text.strip() for s in self.segments).strip()

    @property
    def words(self) -> list[Word]:
        """Return a flat list of every word across all segments."""
        return [w for s in self.segments for w in s.words]


# --- lazy model cache -------------------------------------------------------
_model_lock = threading.Lock()
_model_cache: dict[tuple[str, str, str], object] = {}


def _resolve_device() -> tuple[str, str]:
    """Resolve the (device, compute_type) pair from settings.

    ``auto`` attempts CUDA and falls back to CPU. Returns a tuple suitable for
    passing to ``WhisperModel``.
    """
    device = settings.whisper_device
    compute_type = settings.whisper_compute_type

    if device == "auto":
        try:  # pragma: no cover - depends on host hardware
            import torch  # type: ignore

            if torch.cuda.is_available():
                return "cuda", "float16"
        except Exception:
            pass
        return "cpu", "int8"
    return device, compute_type


def _get_model():
    """Load (and cache) the configured faster-whisper model."""
    from faster_whisper import WhisperModel

    device, compute_type = _resolve_device()
    key = (settings.whisper_model, device, compute_type)
    with _model_lock:
        model = _model_cache.get(key)
        if model is None:
            model = WhisperModel(
                settings.whisper_model,
                device=device,
                compute_type=compute_type,
            )
            _model_cache[key] = model
    return model


def vad_parameters() -> dict:
    """The Silero VAD parameters faster-whisper accepts, from settings (T5).

    Split out so the values can be asserted without a model, and so the cache key and the
    decode read the *same* dictionary rather than two hand-maintained copies - a divergence
    there would produce cache hits across genuinely different VAD settings.
    """
    return {
        "threshold": float(settings.whisper_vad_threshold),
        "min_silence_duration_ms": int(settings.whisper_vad_min_silence_ms),
        "min_speech_duration_ms": int(settings.whisper_vad_min_speech_ms),
        "speech_pad_ms": int(settings.whisper_vad_speech_pad_ms),
    }


def resolve_initial_prompt(vocabulary: str = "") -> Optional[str]:
    """Combine the standing prompt with this job's vocabulary (T4).

    Returns ``None`` rather than ``""`` when there is nothing to say, because faster-whisper
    treats an empty string as a real (empty) prompt rather than as absence.

    The per-job vocabulary comes second so the terms most specific to this video sit closest
    to the audio - Whisper's conditioning weakens with distance from the decode.
    """
    parts = [
        (settings.whisper_initial_prompt or "").strip(),
        (vocabulary or "").strip(),
    ]
    combined = " ".join(part for part in parts if part).strip()
    return combined or None


def transcribe_uncached(
    audio_or_video: str | Path,
    language: Optional[str] = None,
    translate: bool = False,
    beam_size: int = 5,
    *,
    vocabulary: str = "",
) -> Transcript:
    """Transcribe ``audio_or_video`` and return a :class:`Transcript`, always running ASR.

    Args:
        audio_or_video: Path to a media file. faster-whisper decodes audio
            directly (via its bundled ffmpeg bindings), so a video file works.
        language: ISO code (e.g. ``"en"``) to force, or ``None`` to auto-detect.
        translate: When ``True``, translate speech to English instead of
            transcribing in the source language.
        beam_size: Decoder beam size (higher = slightly better/slower).

    Returns:
        A :class:`Transcript` with segment- and word-level timing.

    :func:`transcribe` is the caching entry point (T8) and is what callers should use; this is
    the escape hatch for forcing a fresh transcription, and the seam the cache's own tests
    replace so they need no model.
    """
    model = _get_model()
    task = "translate" if translate else "transcribe"

    segments_iter, info = model.transcribe(
        str(audio_or_video),
        language=language,
        task=task,
        beam_size=beam_size,
        word_timestamps=True,
        # T5: was a bare `vad_filter=True` with every parameter at the library default, so
        # none of it could be adjusted for difficult audio.
        vad_filter=bool(settings.whisper_vad_filter),
        vad_parameters=vad_parameters(),
        # T4: names, jargon and brands the model has no reason to expect.
        initial_prompt=resolve_initial_prompt(vocabulary),
    )

    segments: list[TranscriptSegment] = []
    for seg in segments_iter:
        words = [
            Word(
                start=float(w.start),
                end=float(w.end),
                text=w.word,
                probability=float(getattr(w, "probability", 1.0) or 1.0),
            )
            for w in (seg.words or [])
            if w.start is not None and w.end is not None
        ]
        segments.append(
            TranscriptSegment(
                start=float(seg.start),
                end=float(seg.end),
                text=seg.text.strip(),
                words=words,
                no_speech_prob=float(getattr(seg, "no_speech_prob", 0.0) or 0.0),
                avg_logprob=float(getattr(seg, "avg_logprob", 0.0) or 0.0),
            )
        )

    return Transcript(language=info.language, segments=segments)


def cache_key_for(
    audio_or_video: str | Path,
    *,
    language: Optional[str] = None,
    translate: bool = False,
    beam_size: int = 5,
    vocabulary: str = "",
) -> Optional[str]:
    """The T8 cache key for these ASR parameters, or ``None`` if the source is unreadable.

    Named and exported rather than inlined into :func:`transcribe` because a second caller
    now needs the same key: U4's transcript endpoint reads the cached transcript for a clip
    it is not re-transcribing (:mod:`worker.clip_transcript`). Deriving the key in two places
    is the exact defect the mutation harness has caught twice - one fact stated twice, so
    changing either copy has no observable effect and the two quietly disagree about which
    entry is "the" transcript for a source.
    """
    from worker import transcript_cache

    try:
        return transcript_cache.cache_key(
            transcript_cache.hash_source(audio_or_video),
            model=settings.whisper_model,
            language=language,
            translate=translate,
            beam_size=beam_size,
            # T4/T5: the vocabulary prompt and VAD settings shape the output, so they key it.
            asr_config=transcript_cache.asr_fingerprint(vocabulary),
        )
    except OSError:
        # Unreadable or vanished source: let the ASR call produce the real error, rather than
        # reporting it as a cache problem.
        return None


def transcribe(
    audio_or_video: str | Path,
    language: Optional[str] = None,
    translate: bool = False,
    beam_size: int = 5,
    *,
    vocabulary: str = "",
    on_hit=None,
    on_miss=None,
) -> Transcript:
    """Transcribe ``audio_or_video``, reusing a cached result when one matches (T8).

    Caching is the *default* rather than an opt-in variant, deliberately. ``pipeline.transcribe``
    is an established test seam - one parity test wires a reconstructed v0.7.0 pipeline's seam to
    this very function - so introducing a separately-named cached entry point would both break
    that contract and leave every future caller to remember which name to use. The expensive,
    repeated thing should be cheap by default; :func:`transcribe_uncached` is there for when it
    must not be.

    The cache key covers the source's *content* and every parameter that shapes the output -
    model, language, task, beam size - so an upgraded model or a re-exported file misses
    rather than silently serving a stale transcript. See :mod:`worker.transcript_cache`.

    Degrades to plain transcription whenever anything about the cache is uncooperative: the
    directory is unwritable, the file cannot be hashed, an entry is corrupt. A cache is an
    optimisation, and a job must never fail because one was unavailable.

    ``on_hit``/``on_miss`` are optional callbacks taking the cache key, for progress reporting
    and for tests that need to prove which path ran.
    """
    from worker import transcript_cache

    if not settings.transcript_cache_enabled:
        return _filtered(
            transcribe_uncached(audio_or_video, language=language, translate=translate,
                                beam_size=beam_size, vocabulary=vocabulary)
        )

    key = cache_key_for(
        audio_or_video, language=language, translate=translate,
        beam_size=beam_size, vocabulary=vocabulary,
    )

    if key is not None:
        cached = transcript_cache.load(key)
        if cached is not None:
            if on_hit is not None:
                on_hit(key)
            return _filtered(cached)

    if key is not None and on_miss is not None:
        on_miss(key)

    transcript = transcribe_uncached(audio_or_video, language=language, translate=translate,
                                     beam_size=beam_size, vocabulary=vocabulary)
    if key is not None:
        transcript_cache.store(key, transcript)
    return _filtered(transcript)


def _filtered(transcript: Transcript) -> Transcript:
    """Apply T3's hallucination/repetition filter to ``transcript``.

    Applied *after* the cache, never before it: the cache holds raw ASR output, so tuning a
    threshold or fixing a filter rule takes effect on the next run instead of invalidating
    hours of transcription. Filtering is microseconds; transcribing is minutes. Storing the
    filtered form would also make the cache lossy - a segment dropped by a rule we later
    decide was wrong could not be recovered without re-running the model.
    """
    from worker import transcript_filter

    result = transcript_filter.filter_transcript(transcript)
    if result.removed_count:
        logger.info(
            "T3: dropped %d transcript segment(s): %s",
            result.removed_count, "; ".join(result.reasons[:5]),
        )
    return result.transcript
