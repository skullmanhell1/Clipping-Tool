"""Production registration seam for the AV engines.

Importing an engine module is what registers it: each engine module ends with a
guarded ``registry.register(<Engine>())`` call that runs at import time. Nothing
in ``worker/`` imported those modules, so on a real install the default registry
was empty — ``/api/info`` advertised no engine, ``Engine_Host`` found none, and a
Feature_Flag could not mean anything however it was set.

This module is that seam, and its **sole** job is to import the engine modules
for their import-time side effect. It is imported once at module scope by the two
production entry points that need a populated registry:

* :mod:`api.main` — so ``/api/info`` advertises every engine (each still
  ``enabled_by_default: false``).
* :mod:`worker.pipeline` — so ``Engine_Host`` can find an engine when its
  Feature_Flag is on.

Registration deliberately does **not** live in :mod:`worker.engines.__init__`:
that package is an empty marker by contract, so importing ``worker.engines`` must
pull in nothing and its stdlib-only submodules stay importable without dragging
in ffmpeg, OpenCV or torch.

Adding an engine is therefore one line here and nothing else — a new engine spec
appends its import below and touches neither ``api/main.py`` nor
``worker/pipeline.py``. Keep this module trivially additive: no logic, no
conditionals, no re-exports, only side-effect imports. Every engine module is
import-safe (each heavy dependency sits behind a lazy call), so importing this
module never requires ffmpeg, libass or any font to be installed.
"""

from __future__ import annotations

from worker.engines import kinetic  # noqa: F401  (side-effect import: registers the engine)
