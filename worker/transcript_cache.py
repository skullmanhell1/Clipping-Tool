"""Cache transcripts by source content and ASR parameters (T8).

ASR is the most expensive stage in the pipeline by a wide margin, and it is also the most
repeated: re-running a source to try different caption presets, a different aspect ratio, or
any of the thirteen effect toggles re-transcribes audio that has not changed. The
``S1`` evaluation harness has the same problem worse - iterating on a *selection* signal meant
re-transcribing every source in the dataset each time.

Two decisions in the key, neither of which is implied by "cache by source hash":

**The ASR parameters are part of the key, not just the file.** A transcript produced by the
``base`` model is not interchangeable with one from ``small`` - that is the whole point of
``T1``, which just changed that default - and ``language``/``translate``/``beam_size`` change
the output too. Keying on the file alone would serve a stale transcript from a worse model
after an upgrade, silently and permanently, which is worse than no cache at all.

**The file is hashed by content, not by path and mtime.** Hashing a gigabyte costs a couple of
seconds; transcribing it costs minutes, so the ratio makes correctness cheap here. It also
means a re-uploaded or moved file hits the cache, and - more importantly - a file edited in
place *misses* it. Path-and-mtime keying is the usual shortcut and it is wrong in exactly the
case that matters: footage re-exported to the same name.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from config import settings

#: Bumped when the serialised shape changes, so old entries are ignored rather than
#: mis-parsed. Cheaper and safer than migrating a cache that can always be regenerated.
#: v2 added the per-segment ``no_speech_prob``/``avg_logprob`` that T3's filter reads - an
#: entry without them would look like a segment with nothing to doubt.
SCHEMA_VERSION = 2

#: Read size for hashing. Large enough that the syscall overhead is irrelevant, small enough
#: not to hold a meaningful amount of a video in memory.
_CHUNK = 1024 * 1024


def hash_source(path: str | Path) -> str:
    """Content hash of ``path``, streamed.

    Streamed rather than read whole: sources are routinely gigabytes, and a cache that needs
    the file in memory would trade an ASR run for an OOM.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def asr_fingerprint(vocabulary: str = "") -> str:
    """A short hash of the ASR configuration that T4 and T5 made adjustable.

    These belong in the cache key for the same reason the model does: a transcript decoded
    with a vocabulary prompt, or with VAD tuned to keep quiet speech, is *not* interchangeable
    with one decoded without. Leaving them out would serve the old transcript forever after a
    setting was changed - silently, and with nothing downstream able to notice, which is worse
    than having no cache at all.

    The VAD parameters are excluded when the filter is switched off, because they have no
    effect then and including them would cause pointless misses.

    Imports :mod:`worker.transcribe` lazily to avoid a cycle, and reads *its* helpers rather
    than the settings directly, so the value hashed here is by construction the value passed
    to the decoder. Two hand-maintained copies would eventually disagree, and the failure that
    produces is a cache hit across genuinely different settings.
    """
    from worker import transcribe as tr

    payload: dict[str, Any] = {
        "prompt": tr.resolve_initial_prompt(vocabulary) or "",
        "vad": bool(settings.whisper_vad_filter),
    }
    if settings.whisper_vad_filter:
        payload["vad_params"] = tr.vad_parameters()
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def cache_key(
    source_hash: str,
    *,
    model: str,
    language: str | None,
    translate: bool,
    beam_size: int,
    asr_config: str | None = None,
) -> str:
    """The cache key for a transcript: the content plus everything that shaped it.

    ``asr_config`` is the T4/T5 fingerprint. It defaults to :func:`asr_fingerprint` with no
    per-job vocabulary, so a caller that does not know about it - the S1 harness - still
    computes the same key as a production job on the same file with no vocabulary set. That
    agreement is asserted by a test; without the default the harness and the pipeline would
    quietly maintain two separate caches of the same thing.
    """
    parts = (
        f"v{SCHEMA_VERSION}",
        source_hash,
        model or "",
        language or "auto",
        "translate" if translate else "transcribe",
        str(int(beam_size)),
        asr_config if asr_config is not None else asr_fingerprint(),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def cache_path(key: str, cache_dir: str | Path | None = None) -> Path:
    directory = Path(cache_dir if cache_dir is not None else settings.transcript_cache_dir)
    return directory / f"{key}.json"


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def transcript_to_dict(transcript: Any) -> dict:
    """A JSON-safe view of a :class:`worker.transcribe.Transcript`."""
    return {
        "schema": SCHEMA_VERSION,
        "language": getattr(transcript, "language", "") or "",
        "segments": [
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": segment.text,
                "no_speech_prob": float(getattr(segment, "no_speech_prob", 0.0)),
                "avg_logprob": float(getattr(segment, "avg_logprob", 0.0)),
                "words": [
                    {
                        "start": float(word.start),
                        "end": float(word.end),
                        "text": word.text,
                        "probability": float(getattr(word, "probability", 1.0)),
                    }
                    for word in (getattr(segment, "words", None) or [])
                ],
            }
            for segment in (getattr(transcript, "segments", None) or [])
        ],
    }


def transcript_from_dict(raw: dict):
    """Rebuild a ``Transcript`` from :func:`transcript_to_dict` output.

    Raises ``(KeyError, TypeError, ValueError)`` on anything malformed; callers treat that as
    a cache miss, because a transcript that can always be regenerated is never worth
    salvaging.
    """
    from worker.transcribe import Transcript, TranscriptSegment, Word

    if int(raw.get("schema", -1)) != SCHEMA_VERSION:
        raise ValueError(f"unsupported transcript cache schema: {raw.get('schema')!r}")

    segments = [
        TranscriptSegment(
            start=float(segment["start"]),
            end=float(segment["end"]),
            text=str(segment.get("text", "")),
            no_speech_prob=float(segment.get("no_speech_prob", 0.0)),
            avg_logprob=float(segment.get("avg_logprob", 0.0)),
            words=[
                Word(
                    start=float(word["start"]),
                    end=float(word["end"]),
                    text=str(word.get("text", "")),
                    probability=float(word.get("probability", 1.0)),
                )
                for word in (segment.get("words") or [])
            ],
        )
        for segment in (raw.get("segments") or [])
    ]
    return Transcript(language=str(raw.get("language", "")), segments=segments)


# --------------------------------------------------------------------------- #
# Load / store
# --------------------------------------------------------------------------- #
def load(key: str, cache_dir: str | Path | None = None):
    """A cached transcript for ``key``, or ``None``.

    Never raises. A corrupt, truncated or unreadable entry is a miss: the transcript can be
    regenerated, so failing the job over a cache file would be trading a recoverable cost for
    an unrecoverable one.
    """
    path = cache_path(key, cache_dir)
    try:
        return transcript_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store(key: str, transcript: Any, cache_dir: str | Path | None = None) -> Path | None:
    """Cache ``transcript`` under ``key``. Best-effort; returns the path written.

    Written to a temporary file and then renamed, because a job killed mid-write would
    otherwise leave a truncated entry that every later run has to detect and discard. The
    rename is atomic on the same filesystem, so a reader only ever sees a complete file.
    """
    path = cache_path(key, cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.partial")
        temporary.write_text(json.dumps(transcript_to_dict(transcript), indent=1), encoding="utf-8")
        temporary.replace(path)
        return path
    except (OSError, TypeError, ValueError):
        return None
