"""The app's routing table, frozen — the guard for splitting `api/main.py` into routers.

`api/main.py` was 1934 lines holding 46 routes, and the split into `api/routers/*` is meant to be a
**pure move**: the same paths, the same methods, the same order, the same auth and rate-limit
dependencies, the same response classes. Nothing about it should be observable from outside.

Which is exactly why it needs a test. The frontend has 38 hard-coded `fetch` calls; a path that
loses a trailing segment, a `PATCH` that becomes a `POST`, or a route that quietly drops its
`Depends(rate_limit)` are all things a refactor can do without failing anything else in the suite.

Order matters, and is frozen too
--------------------------------
Starlette matches routes in declaration order and takes the first hit, so moving
`/api/profiles/builtin` after a hypothetical `/api/profiles/{profile_id}` would shadow it. The two
`StaticFiles` mounts are the sharper case: the second is mounted at `""`, which matches
*everything*. If a router were registered after it, every one of its routes would return the SPA's
`index.html` instead — with a 200, so nothing would look broken until someone opened the app.

This is a characterisation test. It asserts what the table currently *is*, not what it should be.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute, _IncludedRouter
from starlette.routing import Mount, Route

from api.main import app


def iter_api_routes(routes=None) -> list[APIRoute]:
    """Every `APIRoute` the app will match, in resolution order.

    `app.include_router` does **not** flatten routes into `app.routes` on this FastAPI version
    (0.141): it appends one `_IncludedRouter` per call, which holds the real routes and delegates
    matching to them. So walking `app.routes` and filtering for `APIRoute` — which was correct
    when every route was declared with `@app.get` — now finds nothing at all.

    That is worth stating explicitly, because it is a silent failure: a test that enumerates
    "every route" and gets an empty list passes. Anything asserting a property of all routes has to
    come through here.
    """
    out: list[APIRoute] = []
    for route in (app.routes if routes is None else routes):
        if isinstance(route, APIRoute):
            out.append(route)
        elif isinstance(route, _IncludedRouter):
            out.extend(iter_api_routes(route.original_router.routes))
    return out

#: Committed alongside this module. Regenerated deliberately, by running
#: ``python scripts/freeze_route_table.py`` — never from inside the test run.
#:
#: **Inspect the diff.** Every line is a change to the app's public HTTP surface.
GOLDEN = Path(__file__).parent / "golden" / "route_table.json"


def _dependency_names(route: APIRoute) -> list[str]:
    """The names of the route's **own** declared dependencies, in order.

    This is how `Depends(rate_limit)` is checked, and it is the reason the frozen table is worth
    having: a route that silently loses its rate limit still answers requests normally.

    It does **not** show `require_api_token`. That is registered once on `FastAPI(...)` and, since
    the router split, FastAPI attaches it at the `_IncludedRouter` level rather than copying it
    onto each route's dependant — so it is enforced but invisible here.

    Introspection is the wrong tool for that property anyway. Auth is asserted behaviourally
    instead, over every route, by
    `tests/test_api_security.py::test_every_api_route_is_covered_by_the_dependency`.
    """
    return [
        dep.call.__name__
        for dep in route.dependant.dependencies
        if getattr(dep, "call", None) is not None and hasattr(dep.call, "__name__")
    ]


def _is_spa_fallback(route: APIRoute) -> bool:
    """Is this the placeholder page that stands in for an unbuilt frontend?

    `api/main.py` serves the SPA one of two ways: a `StaticFiles` mount at `""` when
    `frontend/dist` exists, or a `GET /` HTML fallback when it does not. Which one is registered
    says whether somebody has run `npm run build` - it says nothing about the API - so neither
    belongs in a frozen table.

    Leaving them in is what broke this suite. The table was frozen on a checkout with the frontend
    built; the Backend CI job installs no Node and builds nothing, so there it reported
    ``endpoints added: ['GET /']``, a mount list one item short, and
    ``expected exactly one catch-all mount, got []`` - three failures out of seven in a job whose
    Python was fine, and the whole reason this freeze looked broken rather than useful.

    Both are excluded from `capture()` and asserted directly, in both states, by
    :func:`test_the_spa_is_served_exactly_once_and_last`.
    """
    return route.path == "/" and "ui" in route.tags


def capture() -> dict:
    """The whole routing table as plain data.

    API routes are keyed by ``"METHOD path"`` rather than held in a list, deliberately. Grouping
    the routes into routers *does* change their declaration order — the current file interleaves
    tags, with `metadata` routes on either side of a `jobs` one — and freezing the order would turn
    that into a failure even though nothing observable changed.

    Order is not ignored; it is checked where it actually matters, by
    :func:`test_no_route_is_shadowed_by_an_earlier_one` and
    :func:`test_the_spa_catch_all_mount_is_last`. Those are stronger than an ordered golden: an
    ordered golden says "this is the order it was in", while they say "the order cannot change what
    any request resolves to".

    The mounts *are* ordered, because for them order is the whole story.
    """
    api: dict[str, dict] = {}
    mounts: list[dict] = []
    builtin: list[dict] = []

    for route in iter_api_routes():
        # Excluded: registered only when the frontend has *not* been built. See _is_spa_fallback.
        if _is_spa_fallback(route):
            continue
        # HEAD is added implicitly alongside GET and carries no information of its own.
        for method in sorted(route.methods - {"HEAD"}):
            api[f"{method} {route.path}"] = {
                "name": route.name,
                "tags": list(route.tags),
                "dependencies": _dependency_names(route),
                "response_class": type(route.response_class).__name__
                if not isinstance(route.response_class, type)
                else route.response_class.__name__,
                "status_code": route.status_code,
                # The declared return annotation is what FastAPI builds the response model
                # from; a lost `-> FileResponse` changes the schema and the response headers.
                "response_model": getattr(route.response_model, "__name__", None)
                if route.response_model is not None else None,
            }

    for route in app.routes:
        if isinstance(route, Mount):
            # Excluded for the same reason as the fallback route above: the mount at `""` is the
            # built SPA, so freezing it freezes whether `frontend/dist` existed at capture time.
            # Its ordering requirement - the thing that actually matters - is asserted directly.
            if route.path == "":
                continue
            mounts.append({
                "path": route.path,
                "name": route.name,
                "app": type(route.app).__name__,
            })
        elif isinstance(route, Route):
            # FastAPI's own /openapi.json, /docs, /redoc.
            builtin.append({
                "path": route.path,
                "methods": sorted((route.methods or set()) - {"HEAD"}),
                "name": route.name,
            })

    return {"routes": api, "mounts": mounts, "builtin": builtin}


@pytest.fixture(scope="module")
def golden() -> dict:
    if not GOLDEN.exists():
        return {}
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_no_endpoint_was_added_or_removed(golden):
    """The set of `METHOD path` pairs, which is the app's contract with the frontend."""
    if not golden:
        pytest.fail(
            "no frozen route table. Generate it with "
            "`python scripts/freeze_route_table.py` and inspect the diff."
        )
    captured = capture()
    added = sorted(set(captured["routes"]) - set(golden["routes"]))
    removed = sorted(set(golden["routes"]) - set(captured["routes"]))
    assert not added and not removed, (
        f"endpoints added: {added}\nendpoints removed: {removed}"
    )


def test_every_endpoint_keeps_its_tags_dependencies_and_response_shape(golden):
    """Per endpoint, so a failure names the one that changed.

    The `dependencies` list is the important one: it is where `require_api_token` and
    `rate_limit` show up, and a route that silently loses either still answers requests.
    """
    if not golden:
        pytest.fail("no frozen route table; run `python scripts/freeze_route_table.py`")
    captured = capture()
    for endpoint, expected in sorted(golden["routes"].items()):
        assert endpoint in captured["routes"], f"{endpoint} is gone"
        assert captured["routes"][endpoint] == expected, (
            f"{endpoint} changed\n"
            f"  frozen:   {json.dumps(expected, sort_keys=True)}\n"
            f"  produced: {json.dumps(captured['routes'][endpoint], sort_keys=True)}"
        )


def test_the_mounts_are_unchanged_and_still_in_order(golden):
    """Ordered, unlike the routes: for the mounts, order *is* the behaviour."""
    if not golden:
        pytest.fail("no frozen route table; run `python scripts/freeze_route_table.py`")
    assert capture()["mounts"] == golden["mounts"]


def test_no_route_is_shadowed_by_an_earlier_one():
    """Every request resolves to the route that declares it — regardless of grouping.

    This is what makes regrouping the routes into routers safe, and it is a stronger statement
    than freezing the declaration order. Starlette matches in order and takes the first hit, so a
    route is only reachable if no earlier route's *pattern* also matches its path with an
    overlapping method.

    Checked by resolving a concrete example of each route's path against the real compiled
    `path_regex` of every route before it, which is exactly how Starlette will do it at runtime.
    """
    api_routes = iter_api_routes()

    for index, route in enumerate(api_routes):
        # A concrete path for this pattern. The value has to be something no other route uses as
        # a literal segment, or the test would report a false conflict.
        sample = re.sub(r"\{[^}]+\}", "zzsamplezz", route.path)
        for method in route.methods - {"HEAD"}:
            for earlier in api_routes[:index]:
                if method not in earlier.methods:
                    continue
                assert not earlier.path_regex.fullmatch(sample), (
                    f"{method} {route.path} is unreachable: {earlier.name} "
                    f"({earlier.path}) is declared earlier and also matches {sample!r}"
                )


def test_the_spa_is_served_exactly_once_and_last():
    """Asserted independently of the golden, because getting it wrong fails silently.

    The mount at `""` matches every path. A router registered after it would have all of its
    routes answered with `index.html` and a 200, so no request would error and no test that only
    checks status codes would notice.

    Written for both states of `frontend/dist` on purpose. `api/main.py` registers the mount when
    the SPA has been built and a `GET /` fallback page when it has not, and asserting only the
    first made this a statement about the working tree: green locally, red in CI, where the backend
    job builds no frontend. Which of the two is present is not a property worth freezing - that
    *exactly one* of them is, and that the greedy one cannot shadow anything, is.
    """
    mounts = [
        (index, route) for index, route in enumerate(app.routes) if isinstance(route, Mount)
    ]
    catch_all = [(index, route) for index, route in mounts if route.path == ""]
    fallback = [route for route in iter_api_routes() if _is_spa_fallback(route)]

    assert len(catch_all) + len(fallback) == 1, (
        "`/` must be served exactly once - by the SPA mount when the frontend is built, or by the "
        f"fallback page when it is not - but found {len(catch_all)} catch-all mount(s) and "
        f"{len(fallback)} fallback route(s)"
    )

    if not catch_all:
        # Unbuilt checkout: nothing is greedy, so there is no ordering requirement to check.
        return

    index, _route = catch_all[0]
    assert index == len(app.routes) - 1, (
        "the catch-all StaticFiles mount is not last; every route after it is unreachable: "
        f"{[getattr(r, 'path', r) for r in app.routes[index + 1:]]}"
    )


def test_every_api_route_has_exactly_one_tag():
    """The tags *are* the module boundary, so an untagged route has no home.

    Pinned so a route added later cannot land in `main.py` by default and stay there.
    """
    untagged = [
        route.path for route in iter_api_routes() if len(route.tags) != 1
    ]
    assert not untagged, f"routes without exactly one tag: {untagged}"


def test_no_two_routes_share_a_path_and_method():
    """A duplicate registration is shadowed silently — the second one simply never runs."""
    seen: dict[tuple[str, str], str] = {}
    for route in iter_api_routes():
        for method in route.methods - {"HEAD"}:
            key = (route.path, method)
            assert key not in seen, (
                f"{method} {route.path} is registered twice: "
                f"{seen[key]} and {route.name}"
            )
            seen[key] = route.name
