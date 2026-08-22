"""``render.yaml`` describes a deployment that can actually start.

The blueprint is the documented one-click deploy, it carries ``autoDeploy: true``, and **nothing
tested it**. That combination shipped a manifest which could not boot at all:
``ENVIRONMENT: production`` was set, ``API_AUTH_TOKEN`` was absent entirely, and ``CORS_ORIGINS``
was ``sync: false`` — so ``_check_deployment_security()`` hit *both* of its refuse-in-production
conditions and raised ``InsecureDeploymentError`` from inside the ASGI lifespan. uvicorn never
bound, ``healthCheckPath`` never answered, and every push produced a failed deploy.

The gap is a specific one worth naming, because the logic was not what was missing.
``tests/test_api_security.py`` covers ``_check_deployment_security()`` thoroughly — with
*monkeypatched* settings. Six tests asserted what the function does with a given configuration and
none asked whether the configuration this repository actually ships is one of the good ones. A
green suite plus a red deploy is exactly that shape, and CI could not see it either: the "Import &
boot smoke test" step only does ``from api.main import app``, and FastAPI does not run the lifespan
on import, so the startup gate executes nowhere in CI.

So this reads the manifest as an operator's Render dashboard would and runs the real gate against
the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api.main import InsecureDeploymentError, _check_deployment_security
from config import BASE_DIR, Settings, settings

MANIFEST = Path(__file__).resolve().parent.parent / "render.yaml"


def _web_service() -> dict:
    """The single web service defined by the blueprint."""
    parsed = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    services = [s for s in parsed.get("services", []) if s.get("type") == "web"]
    assert len(services) == 1, f"expected exactly one web service, found {len(services)}"
    return services[0]


def _declared_env() -> dict[str, dict]:
    """The service's env vars, keyed by name, as declared."""
    return {entry["key"]: entry for entry in _web_service().get("envVars", [])}


def _value_at_deploy_time(entry: dict) -> str | None:
    """What the process will see for one declared env var, or ``None`` for "maybe unset".

    Three Render forms, and the difference between them is the whole point of this file:

    * ``value:`` — committed here, so it is exactly what the container gets. An empty string is a
      real value and is returned as one, not folded into ``None``.
    * ``generateValue: true`` — Render mints a random secret at sync time. Guaranteed present, and
      its content is unknowable here, so it stands in as a placeholder.
    * ``sync: false`` — Render prompts, and the operator may skip it. Modelled as unset, which is
      the pessimistic reading and the only safe one for a boot check.
    """
    if entry.get("generateValue") is True:
        return "generated-at-sync-time"
    if "value" in entry:
        return str(entry["value"])
    return None


def test_the_blueprint_boots(monkeypatch):
    """The shipped manifest passes the startup gate it is subject to.

    This is the test whose absence let the un-bootable manifest ship. It deliberately drives the
    real ``_check_deployment_security`` rather than re-implementing its rules, so tightening the
    gate cannot silently stop covering the blueprint.
    """
    env = _declared_env()
    monkeypatch.setattr(settings, "environment", _value_at_deploy_time(env["ENVIRONMENT"]))
    token_entry = env.get("API_AUTH_TOKEN")
    assert token_entry is not None, (
        "render.yaml declares ENVIRONMENT=production, and production refuses to boot without "
        "API_AUTH_TOKEN. Omitting the key does not deploy without auth - it does not deploy."
    )
    monkeypatch.setattr(settings, "api_auth_token", _value_at_deploy_time(token_entry))

    cors_entry = env.get("CORS_ORIGINS")
    assert cors_entry is not None, "render.yaml must declare CORS_ORIGINS"
    cors = _value_at_deploy_time(cors_entry)
    assert cors is not None, (
        "CORS_ORIGINS is declared sync:false, so an operator who skips the prompt gets the "
        "application default of '*', which production refuses to boot with. Give it a value."
    )
    monkeypatch.setattr(settings, "cors_origins", cors)

    _check_deployment_security()


def test_this_file_would_have_caught_the_original_manifest(monkeypatch):
    """The exact configuration that shipped, asserted to be rejected.

    Without this, ``test_the_blueprint_boots`` is a test whose failure mode is unproven — it
    passes on the fixed manifest, and nothing demonstrates it would have failed on the broken one.
    The reconstruction is the real thing: ``ENVIRONMENT: production`` declared, ``API_AUTH_TOKEN``
    absent, ``CORS_ORIGINS`` declared ``sync: false`` and left unanswered, which leaves the
    application default of ``*``.
    """
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", None)
    monkeypatch.setattr(settings, "cors_origins", Settings.model_fields["cors_origins"].default)
    with pytest.raises(InsecureDeploymentError) as caught:
        _check_deployment_security()
    message = str(caught.value)
    assert "API_AUTH_TOKEN" in message and "CORS_ORIGINS" in message


def test_the_blueprint_is_production_and_therefore_gated():
    """A guard on the test above, which would pass vacuously under a local ENVIRONMENT.

    If someone "fixes" a failing deploy by setting ``ENVIRONMENT: development``, every refusal
    downgrades to a warning, ``test_the_blueprint_boots`` goes green, and the deployment is
    publicly readable with no shared secret. That is the cheapest wrong fix available, so it gets
    its own assertion.
    """
    environment = _value_at_deploy_time(_declared_env()["ENVIRONMENT"])
    probe = Settings(environment=environment)
    assert not probe.is_local_environment, (
        f"render.yaml sets ENVIRONMENT={environment!r}, which config.py treats as a developer "
        "machine - so the deployment's security checks only warn. It is a deployment."
    )


def test_the_declared_cors_value_disables_the_wildcard():
    """The empty ``CORS_ORIGINS`` really does parse to "no cross-origin access".

    ``Settings.cors_origins_list`` filters empty segments, so ``""`` becomes ``[]`` rather than
    ``[""]``, and ``cors_allow_wildcard`` is then false. That is a two-step inference about a
    value committed in a YAML file, and it is what makes the blueprint bootable - worth pinning
    rather than re-deriving.
    """
    cors = _value_at_deploy_time(_declared_env()["CORS_ORIGINS"])
    probe = Settings(cors_origins=cors)
    assert probe.cors_origins_list == []
    assert not probe.cors_allow_wildcard
    # The same value must also leave credentialed requests coherent: no wildcard means the
    # CORS spec no longer forbids Access-Control-Allow-Credentials.
    assert probe.cors_allow_credentials


def test_the_generated_token_is_not_committed():
    """A secret in this file would be in every fork of the repository."""
    entry = _declared_env()["API_AUTH_TOKEN"]
    assert "value" not in entry, (
        "API_AUTH_TOKEN must not carry a literal value in render.yaml - use "
        "`generateValue: true` so Render mints it and it stays out of version control."
    )
    assert entry.get("generateValue") is True


@pytest.mark.parametrize(
    ("key", "field"),
    [("WHISPER_MODEL", "whisper_model"), ("STORAGE_BACKEND", "storage_backend")],
)
def test_the_blueprint_does_not_silently_downgrade_a_default(key, field):
    """Pinned because it happened: ``WHISPER_MODEL`` was ``base`` here and ``small`` in the code.

    The manifest quietly shipped worse captions on every clip, with no symptom other than the
    words being wrong, and the comment beside it now asks that the two be kept equal. An ask in a
    comment is not a gate, so this is the gate. If a deliberate divergence is ever wanted, this
    test is the right place to record why.
    """
    declared = _value_at_deploy_time(_declared_env()[key])
    default = Settings.model_fields[field].default
    expected = getattr(default, "value", default)
    assert declared == str(expected), (
        f"render.yaml sets {key}={declared!r} but config.py defaults {field} to {expected!r}. "
        "Either match it or document the divergence here."
    )


def test_every_declared_key_is_a_real_setting():
    """A key with no matching field is configuration that does nothing.

    ``Settings`` uses ``extra="ignore"``, so a stale or misspelled key in this manifest is
    accepted in silence — the operator sets it, the deploy succeeds, and the value is discarded.
    Eight such variables were retired from ``.env.example`` for exactly this reason; the blueprint
    deserves the same check.
    """
    fields = set(Settings.model_fields)
    unknown = [key for key in _declared_env() if key.lower() not in fields]
    assert not unknown, (
        f"render.yaml declares {unknown}, which no Settings field reads. Settings uses "
        "extra='ignore', so these are silently discarded at boot."
    )


def test_the_disk_is_mounted_where_storage_is_written():
    """A persistent disk mounted somewhere the app does not write is not persistence.

    ``storage_root`` defaults to ``./storage`` and the image's workdir is ``/app``, so the disk has
    to land on ``/app/storage``. Get this wrong and everything works — until the first redeploy
    silently discards every clip and the jobs database, which is the least recoverable failure in
    this whole manifest and produces no error at all.
    """
    disk = _web_service().get("disk") or {}
    mount = str(disk.get("mountPath", ""))
    # The declared *default* expressed relative to BASE_DIR, not `Settings().storage_root`:
    # `tests/conftest.py` redirects STORAGE_ROOT into a tmp directory for the whole session, so an
    # instantiated Settings here reports the test sandbox. The default is `BASE_DIR / "storage"`,
    # and the Dockerfile copies the repository to WORKDIR /app — so /app is BASE_DIR in the image.
    default_root = Path(str(Settings.model_fields["storage_root"].default))
    relative = default_root.relative_to(BASE_DIR)
    assert mount == f"/app/{relative}", (
        f"disk.mountPath is {mount!r} but storage_root defaults to {default_root} "
        f"(BASE_DIR/{relative}), and BASE_DIR is /app in the image — so durable data would be "
        "written outside the disk and lost on redeploy."
    )
