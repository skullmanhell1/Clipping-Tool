"""The label format for the selection benchmark, and its loader (S1).

A label file records, for one source video, the moments a human would actually post. The
format is deliberately small - start, end, and a note - because a labelling task that is
tedious does not get done, and twenty sources labelled roughly is worth far more than three
labelled meticulously.

One file per source, in a directory:

    eval/labels/podcast-ep12.json

    {
      "source": "/media/podcasts/ep12.mp4",
      "notes": "two hosts, lots of cross-talk",
      "moments": [
        {"start": 412.0, "end": 455.0, "note": "the story about the failed launch"},
        {"start": 1123.5, "end": 1160.0, "note": "punchline about pricing"}
      ]
    }

``source`` may be absolute or relative to the label file, so a dataset stays portable when the
footage sits beside it.

Deliberately *not* in the format:

* **A rank or score per moment.** Asking a human to order their own picks invites
  second-guessing, and the metrics do not need it: precision@k asks whether a returned clip is
  one you wanted, not whether it was your third favourite.
* **Anything the pipeline can derive.** Duration is probed, transcripts are cached. A label
  file the tool could partly generate is a label file that will drift from the footage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class DatasetError(ValueError):
    """A label file is missing, malformed, or describes moments that cannot be scored."""


@dataclass(frozen=True)
class LabelledMoment:
    """One moment a human marked as worth posting."""

    start: float
    end: float
    note: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class LabelledSource:
    """One source video and its labelled moments."""

    source: Path
    moments: list[LabelledMoment]
    notes: str = ""
    #: Where the label came from, for error messages that name a file a human can open.
    label_path: Path | None = None

    @property
    def name(self) -> str:
        return self.source.name or str(self.source)

    @property
    def exists(self) -> bool:
        return self.source.exists()


@dataclass
class Dataset:
    """A set of labelled sources."""

    sources: list[LabelledSource] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.sources)

    @property
    def moment_count(self) -> int:
        return sum(len(source.moments) for source in self.sources)

    def missing_media(self) -> list[LabelledSource]:
        """Labelled sources whose video file is not present.

        Reported rather than raised: a dataset is often shared without its footage, and the
        harness can still validate the labels and score a cached transcript.
        """
        return [source for source in self.sources if not source.exists]


def _parse_moment(raw: object, where: str) -> LabelledMoment:
    if not isinstance(raw, dict):
        raise DatasetError(f"{where}: each moment must be an object, got {type(raw).__name__}")
    try:
        start = float(raw["start"])
        end = float(raw["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DatasetError(f"{where}: moment needs numeric 'start' and 'end' ({exc})") from exc
    if end <= start:
        raise DatasetError(f"{where}: moment ends at or before it starts ({start} -> {end})")
    if start < 0:
        raise DatasetError(f"{where}: moment starts before zero ({start})")
    return LabelledMoment(start=start, end=end, note=str(raw.get("note", "")))


def load_label_file(path: str | Path) -> LabelledSource:
    """Load and validate one label file.

    Validation is strict and fails loudly. A dataset is the instrument every §3 change is
    measured with, so a typo'd timestamp silently scoring against the wrong part of a video
    would corrupt every result taken from it - and unlike a normal input, nothing downstream
    would look wrong.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError(f"label file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path}: not valid JSON ({exc})") from exc

    if not isinstance(raw, dict):
        raise DatasetError(f"{path}: expected an object at the top level")

    source_value = raw.get("source")
    if not source_value or not isinstance(source_value, str):
        raise DatasetError(f"{path}: 'source' must be a non-empty string")
    source = Path(source_value)
    if not source.is_absolute():
        # Relative to the label file, so a dataset directory can travel with its footage.
        source = (path.parent / source).resolve()

    moments_raw = raw.get("moments")
    if not isinstance(moments_raw, list) or not moments_raw:
        raise DatasetError(f"{path}: 'moments' must be a non-empty list")

    moments = [
        _parse_moment(item, f"{path} moment {index}") for index, item in enumerate(moments_raw)
    ]
    moments.sort(key=lambda moment: (moment.start, moment.end))

    # Overlapping labels make precision ambiguous: one returned clip could legitimately match
    # two "different" wanted moments, so a selector would be rewarded twice for one decision.
    for earlier, later in zip(moments, moments[1:]):
        if later.start < earlier.end:
            raise DatasetError(
                f"{path}: labelled moments overlap ({earlier.start}-{earlier.end} and "
                f"{later.start}-{later.end}). Merge them, or move a boundary: an overlap "
                "lets one returned clip match two wanted moments at once."
            )

    return LabelledSource(
        source=source,
        moments=moments,
        notes=str(raw.get("notes", "")),
        label_path=path,
    )


def load_dataset(directory: str | Path) -> Dataset:
    """Load every ``*.json`` label file in ``directory``."""
    directory = Path(directory)
    if not directory.is_dir():
        raise DatasetError(f"not a directory: {directory}")
    paths = sorted(p for p in directory.glob("*.json") if p.is_file())
    if not paths:
        raise DatasetError(f"no label files (*.json) in {directory}")
    return Dataset(sources=[load_label_file(path) for path in paths])


#: Written by ``scripts/eval_selection.py template`` as a starting point for labelling.
TEMPLATE: dict = {
    "source": "relative/or/absolute/path/to/video.mp4",
    "notes": "anything about this source worth remembering while reading results",
    "moments": [
        {"start": 0.0, "end": 30.0, "note": "what makes this moment worth posting"},
    ],
}
