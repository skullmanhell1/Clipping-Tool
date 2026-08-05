"""``docs/BACKUP_AND_RESTORE.md`` is kept in step with the code it describes (Phase 7).

The repository documented no way to back up or restore the two SQLite databases, the operator
config files or the rendered output. A self-hosted tool whose users are told to run it and keep
their publish history in it needs that written down, so the document was added.

A recovery runbook that has drifted is worse than none: it is read once, under pressure, by someone
who cannot verify it - and every claim in it is load-bearing at exactly that moment. The document's
own accuracy was established by executing all seventeen procedures (see its Verification table);
this is what stops the *code* moving out from under it afterwards.

Same argument as :mod:`tests.test_config_documentation`, which pins ``.env.example`` against
``config.Settings`` because "drift is invisible without a check like this, which is exactly why it
happened".

Only load-bearing facts are asserted - the ones whose silent change would make an operator lose
data or misread a restore. Prose is not pinned; a test that fails when someone improves a sentence
teaches people to delete tests.

The most important assertion here is :func:`test_there_are_still_exactly_two_sqlite_databases`. A
third database added later would not appear in the documented backup script, so the runbook would
keep passing every check while quietly omitting a whole store - and that omission would be
discovered when someone tried to restore it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "BACKUP_AND_RESTORE.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.is_file(), f"{DOC.relative_to(ROOT)} is missing"
    return DOC.read_text(encoding="utf-8")


def _app_modules_using_sqlite() -> set[str]:
    """Application modules that touch ``sqlite3``, excluding tests and tooling."""
    found: set[str] = set()
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith((".venv/", "tests/", "scripts/", "build/")):
            continue
        if "sqlite3" in path.read_text(encoding="utf-8"):
            found.add(relative)
    return found


# --------------------------------------------------------------------------- #
# The claim the whole runbook rests on                                          #
# --------------------------------------------------------------------------- #
def test_there_are_still_exactly_two_sqlite_databases():
    """A third store would be silently absent from the documented backup.

    The runbook enumerates the databases by name (``for db in jobs.db history.db``), which is the
    right shape for a shell script and the wrong shape for something that must not go out of date
    on its own. This is the compensating check: add a database, and this fails with the path to
    document.
    """
    assert _app_modules_using_sqlite() == {
        "worker/job_persistence.py",
        "publishers/history.py",
    }, (
        "The set of SQLite-backed stores changed. docs/BACKUP_AND_RESTORE.md enumerates them by "
        "name and its backup script loops over 'jobs.db history.db' - both need updating, or a "
        "whole store will be missing from every backup taken from this runbook."
    )


def test_the_caches_are_still_json_files_rather_than_databases():
    """The document states this explicitly because being told otherwise sends an operator to run
    ``sqlite3`` against a directory."""
    from worker import intermediate_cache, transcript_cache

    for module in (transcript_cache, intermediate_cache):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert (
            "sqlite3" not in source
        ), f"{module.__name__} is now a database; the doc says it is not"
    # The document quotes both numbers as evidence that a stale entry is treated as a miss rather
    # than an error. Bumping one is fine; leaving the document claiming the old value is not,
    # because a runbook whose small facts are wrong does not get trusted on the large ones.
    assert transcript_cache.SCHEMA_VERSION == 2, (
        "transcript_cache.SCHEMA_VERSION changed; update the cache table in "
        "docs/BACKUP_AND_RESTORE.md, which quotes the old value."
    )
    assert (
        intermediate_cache.SCHEMA == 1
    ), "intermediate_cache.SCHEMA changed; update docs/BACKUP_AND_RESTORE.md, which quotes it."


# --------------------------------------------------------------------------- #
# Why `cp` is documented as wrong                                               #
# --------------------------------------------------------------------------- #
def test_both_databases_still_use_wal():
    """The entire "why ``cp`` is the wrong tool" section depends on this.

    If either store stopped using WAL, the document's central warning - and its measured 0-rows-
    from-``cp`` finding - would be describing behaviour that no longer happens, and the extra
    ceremony would look like superstition. Which is how a runbook loses its reader.
    """
    for relative in ("worker/job_persistence.py", "publishers/history.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "journal_mode=WAL" in source, f"{relative} no longer sets WAL"


# --------------------------------------------------------------------------- #
# Restore-time behaviour the document warns about                               #
# --------------------------------------------------------------------------- #
def test_interrupted_statuses_are_still_queued_and_processing():
    """The document names this set, because restoring a database rewrites those rows *on disk*.

    An operator restoring to inspect state needs to know which rows will be altered before they
    start the app; a changed set would make the warning wrong in the specific way that matters.
    """
    from worker.job_persistence import INTERRUPTED_STATUSES

    assert set(INTERRUPTED_STATUSES) == {"queued", "processing"}


def test_restoring_more_jobs_than_the_limit_still_discards_the_excess():
    """The document warns that a restored ``jobs.db`` is pruned on first start.

    Pinned via the default rather than the prose: the number appears in the document, and the
    warning is only actionable if it matches.
    """
    from config import settings

    assert settings.max_persisted_jobs == 500
    assert "max_persisted_jobs" in DOC.read_text(encoding="utf-8")


def test_retention_still_sweeps_only_clips_and_temp():
    """The document states that history rows outlive the files they name, which follows from this.

    If retention started sweeping other areas, "the clip files survive independently" would stop
    being true and the document's guidance on what is recoverable from what would invert.
    """
    from storage_backends.retention import _CLEANABLE

    assert tuple(_CLEANABLE) == ("clips", "temp")


def test_the_history_migration_is_still_column_sniffing_with_no_version():
    """The document deliberately says "migration" oversells it.

    Restoring an older database relies on the forward step running; restoring a *newer* one is
    documented as unhandled. Both statements depend on there being no version number, so if
    ``user_version`` ever starts being set the document's limitations section is wrong.
    """
    source = (ROOT / "publishers" / "history.py").read_text(encoding="utf-8")
    assert "PRAGMA table_info(publish_attempts)" in source
    assert "retry_count" in source
    assert "user_version" not in source, (
        "history.py now uses PRAGMA user_version. docs/BACKUP_AND_RESTORE.md states that it "
        "stays 0 and that migration works by column sniffing."
    )


# --------------------------------------------------------------------------- #
# The security warning                                                          #
# --------------------------------------------------------------------------- #
def test_oauth_tokens_are_still_stored_unencrypted():
    """The document tells the reader to treat a ``history.db`` backup like ``.env``.

    If encryption were added, that warning would be overcautious - which is harmless - but the
    document also states there is no encryption anywhere in ``publishers/``, and a *stale
    reassurance* is the dangerous direction. This fails if the situation changes either way.
    """
    source = (ROOT / "publishers" / "history.py").read_text(encoding="utf-8")
    assert "oauth_tokens" in source
    assert "access_token TEXT NOT NULL" in source
    assert "clear_token" in source, "the document points at clear_token as the mitigation"


def test_there_is_still_no_worker_oauth_tokens_module():
    """The document corrects a specific false belief, so the correction must stay true."""
    assert not (ROOT / "worker" / "oauth_tokens.py").exists()


# --------------------------------------------------------------------------- #
# Every path and setting the document names must exist                          #
# --------------------------------------------------------------------------- #
#: Env-var-shaped tokens the document cites in backticks that are code constants rather than
#: settings. Listed explicitly so the check below stays precise: an operator types env vars, and
#: quietly treating a constant as one would make the test pass for the wrong reason.
_NOT_SETTINGS = {"INTERRUPTED_STATUSES"}

#: The document deliberately names one file that does *not* exist, to correct a specific false
#: belief ("there is no ``worker/oauth_tokens.py``"). Excluded here and asserted absent by
#: :func:`test_there_is_still_no_worker_oauth_tokens_module`, so the correction stays pinned - in
#: the opposite direction.
_DELIBERATELY_ABSENT = {"worker/oauth_tokens.py"}


def test_every_env_var_the_document_names_is_a_real_setting(text):
    """A runbook citing a renamed env var sends the operator to set nothing.

    ``Settings`` is configured with ``extra="ignore"``, so a stale key is accepted in silence -
    the exact trap ``tests/test_config_documentation.py`` was written for. Checked against the
    upper-case names because those are what a reader actually exports.
    """
    from config import Settings

    fields = set(Settings.model_fields)
    cited = set(re.findall(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`", text)) - _NOT_SETTINGS
    unknown = sorted(name for name in cited if name.lower() not in fields)
    assert not unknown, f"the document names env vars that no longer exist: {unknown}"
    # A floor, so the extraction cannot silently match nothing and pass vacuously.
    assert len(cited) >= 5, f"only matched {sorted(cited)}; the extraction is probably broken"


def test_the_code_constants_it_names_still_exist():
    """The names excluded above must be real, or the exclusion becomes a place to hide drift."""
    from worker import job_persistence

    assert hasattr(job_persistence, "INTERRUPTED_STATUSES")


def test_every_repository_file_the_document_names_exists(text):
    """Paths are cited as evidence throughout; a dangling one undermines the rest."""
    cited = (
        set(re.findall(r"`((?:worker|publishers|storage_backends|api|config)[\w/]*\.py)`", text))
        - _DELIBERATELY_ABSENT
    )
    missing = sorted(path for path in cited if not (ROOT / path).is_file())
    assert not missing, f"the document cites files that do not exist: {missing}"
    assert len(cited) >= 4, f"only matched {sorted(cited)}; the extraction is probably broken"


def test_the_document_is_reachable_from_the_readme():
    """A runbook nobody can find is a runbook nobody reads.

    The README already carries a file-tree map of the repository, which is where someone looking
    for operational docs will look.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "BACKUP_AND_RESTORE" in readme
