"""The words behind one rendered clip (U4), for the transcript editor.

A transcript editor needs word-level timings for a clip that has already been rendered.
Nothing on the clip record carries them: :class:`~worker.models.ClipResult` stores
``transcript_text``, a flat string, and the word timings live only in the transcript the
render consumed.

This module recovers them from the **T8 transcript cache** rather than re-transcribing.
Two consequences worth being explicit about, because both are deliberate:

* **It never runs ASR.** A cache miss is reported as
  :class:`TranscriptUnavailable`, not repaired by transcribing. Re-transcription is a
  multi-minute, GPU-hungry operation and this is a synchronous ``GET`` behind a UI
  interaction; a request that silently blocks for four minutes is worse than one that
  says it cannot answer. The cache is enabled by default and the entry was written by
  the job that produced the clip, so a miss means something specific has happened -
  the cache was disabled, swept, or the source was replaced - and the caller is told
  which rather than being made to wait.

* **The words are the ones that were burned in**, because the key is derived by
  :func:`worker.transcribe.cache_key_for` (the same function the render used) and the
  T3 hallucination filter that runs after the cache is deterministic and preserves
  timings. So a word the user clicks is the word they saw.

Words are returned **clip-relative** via :func:`worker.captions.slice_words`, matching
the offsets a cut list must be expressed in.
"""

from __future__ import annotations

from pathlib import Path

from worker import captions as cap
from worker import transcript_cache
from worker.transcribe import Transcript, cache_key_for


class TranscriptUnavailable(RuntimeError):
    """No cached transcript for this source. The message is fit for an API response."""


def load_transcript(
    source: str | Path,
    *,
    language: str | None = None,
    translate: bool = False,
    vocabulary: str = "",
) -> Transcript:
    """Return the cached transcript for ``source``, or raise :class:`TranscriptUnavailable`.

    The keyword arguments must match the ones the render used, or the key will not match
    and a perfectly good entry will read as a miss.
    """
    path = Path(source)
    if not path.is_file():
        raise TranscriptUnavailable(
            "The source file for this job is no longer on disk, so its transcript cannot "
            "be recovered."
        )

    key = cache_key_for(path, language=language, translate=translate, vocabulary=vocabulary or "")
    if key is None:
        raise TranscriptUnavailable("The source file for this job could not be read.")

    cached = transcript_cache.load(key)
    if cached is None:
        raise TranscriptUnavailable(
            "No cached transcript for this clip. Transcript editing reads the cache written "
            "when the clip was made; it is unavailable if the cache was cleared or disabled, "
            "or if the source has changed since."
        )
    return cached


def words_for_clip(
    source: str | Path,
    start: float,
    end: float,
    *,
    language: str | None = None,
    translate: bool = False,
    vocabulary: str = "",
) -> list:
    """Clip-relative words for the window ``[start, end]`` of ``source``.

    Raises :class:`TranscriptUnavailable` when the transcript cannot be recovered.
    """
    transcript = load_transcript(
        source, language=language, translate=translate, vocabulary=vocabulary
    )
    return cap.slice_words(transcript, float(start), float(end))
