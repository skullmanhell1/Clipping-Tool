#!/usr/bin/env python
"""Prove the application actually starts, rather than merely imports.

The CI step this replaces was called "Import & boot smoke test" and performed an import:

    python -c "from api.main import app; print('FastAPI app OK', app.title)"

**FastAPI does not run the lifespan on import.** So `_run_startup` — which creates the storage
directories, proves the storage root is writable, installs the job-scoped log filter, runs
`_check_deployment_security()` and starts the retention sweeper — executed *nowhere in CI*. The
step's name described a boot; its content was an import, and the gap between the two is not
academic: it is why a `render.yaml` that could not boot at all shipped and stayed shipped. Every
gate was green, because none of them started the app.

Entering the lifespan is the whole point of this script. It is also the cheapest possible
integration test — one client, three requests — and it fails loudly rather than degrading.

Run it locally the same way CI does:

    python scripts/boot_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fail(message: str) -> None:
    print(f"BOOT SMOKE FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_config_loads() -> None:
    """Settings resolve at all. Kept from the original step; it is a real precondition."""
    import config

    print(f"  config loads            {config.settings.app_name}")


def check_the_app_boots() -> None:
    """The lifespan runs, and the app serves.

    ``TestClient`` as a context manager is what triggers startup and shutdown. Without the
    ``with``, this would be the same import test with extra steps — which is precisely the
    mistake being corrected, so it is worth stating rather than assuming a reader knows.
    """
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        health = client.get("/healthz")
        if health.status_code != 200:
            _fail(f"/healthz returned {health.status_code}, expected 200")
        print(f"  lifespan + /healthz     {health.json()}")

        info = client.get("/api/info")
        if info.status_code != 200:
            _fail(f"/api/info returned {info.status_code}, expected 200")
        payload = info.json()
        print(f"  /api/info               version={payload.get('version')}")


def check_the_production_gate_is_live() -> None:
    """A misconfigured production deployment refuses to start.

    Asserted here, at the lifespan, and not only as a unit test of
    ``_check_deployment_security()``. The distinction is the entire lesson of this file: the
    function was well covered by tests that called it directly, and what nobody checked was
    whether *starting the app* enforces it. A gate that raises in a unit test and is never reached
    during startup protects nothing.

    Run in a subprocess because ``config.settings`` is a module-level singleton built from the
    environment at import time; mutating the environment in-process would not rebuild it, and
    monkeypatching it would test the mock rather than the boot.
    """
    import subprocess
    import textwrap

    program = textwrap.dedent(
        """
        from fastapi.testclient import TestClient
        from api.main import app
        try:
            with TestClient(app) as client:
                client.get("/healthz")
        except Exception as exc:
            print(type(exc).__name__)
        else:
            print("BOOTED")
        """
    )
    env = dict(os.environ)
    env["ENVIRONMENT"] = "production"
    env["API_AUTH_TOKEN"] = ""
    env["CORS_ORIGINS"] = "*"
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if "InsecureDeploymentError" not in combined:
        _fail(
            "a production environment with no API_AUTH_TOKEN and wildcard CORS was allowed to "
            f"boot. The startup security gate is not being reached.\n{combined.strip()}"
        )
    print("  production gate         refuses to boot when misconfigured")


def main() -> int:
    print("boot smoke:")
    check_config_loads()
    check_the_app_boots()
    check_the_production_gate_is_live()
    print("boot smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
