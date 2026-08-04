"""APIRouter modules for the FastAPI application.

One module per tag group, plus two private support modules:

* ``_models`` — the pydantic request/response models shared across groups.
* ``_shared`` — private helpers that more than one group (or ``api.main``) needs.

Nothing in here imports ``api.main``: ``api.main`` imports these, so the reverse
edge would be a cycle. A helper needed by both a router and ``api.main`` lives in
``_shared``.
"""
