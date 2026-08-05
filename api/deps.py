"""The dependency accessors every router shares, in one place.

These three functions are the seams where the API reaches the rest of the system, and they are
re-exported here for one specific reason: **tests patch them**, and they need one place to patch.

Before ``api/main.py`` was split, tests could write ``monkeypatch.setattr(api.main,
"get_history", ...)`` and have it apply to every route, because every route lived in that one
module. After the split that would have become a trap -- patching ``api.main`` would silently
have no effect on a route that had moved to a router, and the test would still pass while
exercising the real history store. That is a vacuous pass, which is the failure mode this repo's
test gates exist to prevent.

So routers import this *module* and call ``deps.get_manager()`` rather than importing the
functions directly. The attribute lookup happens at call time, so one patch of
``api.deps`` reaches every caller.
"""

from __future__ import annotations

from publishers.history import get_history
from publishers.manager import get_publish_manager
from worker.jobs import get_manager

__all__ = ["get_history", "get_manager", "get_publish_manager"]
