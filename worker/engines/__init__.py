"""AV engines package (engine foundation).

Deliberately an *empty* package marker: importing :mod:`worker.engines` must pull
in nothing, so tooling and tests can import individual submodules (for example
:mod:`worker.engines.timebase`, which is stdlib-only) without dragging in ffmpeg,
OpenCV, torch, or any other optional heavy dependency.

Import the submodules directly:

* :mod:`worker.engines.timebase` — ``Time_Base``, ``Timeline_Segment``,
  Segment_List normalisation. Pure stdlib.
"""
