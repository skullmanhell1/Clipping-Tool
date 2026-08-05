# Backup and restore

Covers the durable state this project keeps on local disk. Everything below was checked
against the code on branch `phase7/observability`; the commands were executed in a sandbox
(see [Verification](#verification-what-was-actually-run) for exactly what was run and what it
returned).

## There are exactly two SQLite databases

`grep -rn "sqlite3" --include=*.py .` finds application code in only two places:
`worker/job_persistence.py` and `publishers/history.py`. **The caches are not SQLite** — they
are directories of JSON files. If you have been told this project has "three SQLite DBs
(jobs.db, history.db, caches)", that is wrong, and a backup plan built on it would run
`sqlite3` against paths that are directories.

### `jobs.db` — job records

* Setting `jobs_db` (`config.py:1011`), env var `JOBS_DB`, default `<BASE_DIR>/storage/jobs.db`.
* One table, created in `Job_Persistence._init()`:

  ```
  jobs(id TEXT PRIMARY KEY, batch_id TEXT, created_at REAL NOT NULL,
       updated_at REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL)
  ```

  plus `idx_jobs_created` on `created_at` and `idx_jobs_batch` on `batch_id`.
* `data` is the whole job serialised by `Job.to_dict()` as JSON. `batch_id` and `created_at`
  are lifted into columns because they are the only fields ever *queried* rather than
  returned — see the module docstring.
* `_init()` runs `PRAGMA journal_mode=WAL`. WAL is a persistent property of the file, so it
  survives across processes.
* **Lost if gone:** the job list and its download links. The clip *files* under
  `clips_dir` survive independently, so losing this database produces clips that exist on
  disk with no job referencing them. Bounded state anyway: `JobStore._restore()` calls
  `prune(keep=settings.max_persisted_jobs)` (default 500, `config.py:1014`) on every start,
  so this database is a rolling window, not an archive.

### `history.db` — publish history, campaigns, OAuth access tokens

* Setting `history_db` (`config.py:1008`), env var `HISTORY_DB`, default
  `<BASE_DIR>/storage/history.db`.
* Four tables, created in `HistoryStore._init()`: `clips`, `publish_attempts`, `campaigns`,
  `oauth_tokens`. Also `PRAGMA journal_mode=WAL`, and one index `idx_attempt_due` on
  `publish_attempts(state, scheduled_at)`.
* **This is the irreplaceable one.** `publish_attempts` is the only record of what was posted
  where, when, with which external ID and URL. It cannot be recomputed from anything else on
  disk. Nothing deletes from it — `storage_backends/retention.py::cleanup_expired()` only
  sweeps files under the `clips` and `temp` areas (`_CLEANABLE = ("clips", "temp")`), so
  history rows outlive the files they name and `clips.path` can point at a deleted file.
* `oauth_tokens` (PB4) holds **credentials in plaintext**:

  ```
  oauth_tokens(platform TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '',
               access_token TEXT NOT NULL, expires_at REAL, refreshed_at REAL NOT NULL,
               PRIMARY KEY (platform, account_id))
  ```

  There is no encryption anywhere in `publishers/` or `config.py` (checked). So a backup of
  `history.db` is a backup of live access tokens and must be stored with the same care as
  `.env`. These are short-lived *access* tokens derived from the long-lived refresh
  credential in settings, and `HistoryStore.clear_token()` exists precisely so the next
  publish mints a fresh one — so if you would rather not carry them, deleting the
  `oauth_tokens` rows from a restored copy is safe and costs one token refresh.

  Note: there is no `worker/oauth_tokens.py`. The table lives in `publishers/history.py`, and
  `publishers/youtube.py` is currently its only consumer.

#### The schema migration in `history.py` is not versioned

Read it accurately, because "migration" oversells it. `_init()` runs the `CREATE TABLE IF NOT
EXISTS` script, and then:

```python
existing = {row["name"] for row in db.execute("PRAGMA table_info(publish_attempts)").fetchall()}
if "retry_count" not in existing:
    db.execute("ALTER TABLE publish_attempts ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
```

* It detects the need by **column presence**, not by a version number. `PRAGMA user_version`
  is never set and stays `0` (verified before and after migrating a pre-PB5 database).
* There is exactly **one** such step: adding `retry_count` for databases created before PB5.
  `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` in SQLite, so an unconditional `ALTER`
  would raise on every start after the first — hence the check.
* It runs on **every** `HistoryStore` construction and is idempotent.
* Consequence for restore: restoring an *older* `history.db` is fine, because the first open
  migrates it forward. Restoring a *newer* one into older code is not covered by anything.

### The caches are directories of JSON files, not databases

| What | Path setting | On-disk shape |
| --- | --- | --- |
| Transcripts (T8) | `transcript_cache_dir` / `TRANSCRIPT_CACHE_DIR`, default `storage/transcripts` | `<key>.json`, one file per entry (`transcript_cache.cache_path`) |
| Intermediates (I3) | `intermediate_cache_dir` / `INTERMEDIATE_CACHE_DIR`, default `<temp_dir>/intermediates` | `<key>.json` files **and** `frames-<hash>-<fp>/` directories of keyframe images |
| External b-roll | `broll_cache_dir`, default `assets/broll_cache` | downloaded asset files |

All of it is **reconstructible**. Both caches are content-addressed, carry a schema constant
(`transcript_cache.SCHEMA_VERSION = 2`, `intermediate_cache.SCHEMA = 1`), and treat any
unreadable or stale-schema entry as a miss rather than an error. Losing them costs re-running
ASR and whole-file decodes — expensive, not fatal. `intermediate_cache.prune()` already
deletes the oldest beyond `intermediate_cache_max_entries` (default 200), so entries are
transient by design.

Because they are plain files, `rsync`/`tar` is the correct tool for them, and they need no
special handling. Backing them up is optional and purely a cost optimisation.

### Other state worth taking, which is easy to forget

* `storage/runtime_config.json` (`runtime_config_path`, `config.py:350`) — operator settings
  edited through the UI, including the effective `retention_days`. Small, hand-made, not
  reconstructible.
* `storage/profiles.json` (`profiles_path`, `config.py:351`) — saved user profiles. Same.
* `.env` — every credential and path override. Not in the repo and not regenerable.

All three, plus both `.db` files, are in `.gitignore` (`storage/*.db`, `storage/*.db-wal`,
`storage/*.db-shm`, `storage/runtime_config.json`, `storage/profiles.json`), so a git clone
restores none of it.

### Rendered clips and uploads

`clips_dir` (`storage/clips`) and `uploads_dir` (`storage/uploads`) are the large ones.

* **Uploads are irreplaceable if the source was a local file.** For a URL job the source can
  be re-downloaded; for an uploaded file, `storage/uploads` is the only copy.
* **Clips are regenerable in principle but not cheaply, and not always.** Re-rendering needs
  the source still present (`Job.source_path` records the local file the pipeline read), and
  it re-runs the paid/slow stages. Selection is not guaranteed byte-identical across
  versions. Treat clips as expensive-to-lose rather than safe-to-lose, and note that
  `cleanup_expired()` deletes them after `retention_days` (default 30) anyway, so anything
  you want permanently should be exported rather than left in `storage/clips`.

Practical split: always back up the two databases and the three small config files — that is
kilobytes to megabytes. Back up `uploads` and `clips` on whatever slower schedule their size
justifies, or not at all if you accept re-rendering.

## Why `cp` is the wrong tool for a live WAL database

Both databases run in WAL mode. In WAL mode a committed transaction is durable once it is in
the `-wal` sidecar; it does not have to be in the `.db` file yet. So copying only `<db>` can
give you a snapshot that is missing recent commits, and copying `.db`, `-wal` and `-shm`
separately with `cp` is worse — they are read at different instants, so the set can be
mutually inconsistent (torn).

This is not theoretical. Measured in the sandbox, with one writer holding a connection open
and committing:

* 500 committed rows lived in a 53 KiB `jobs.db-wal` while `jobs.db` itself was **4096 bytes**.
* `cp jobs.db` during a live writer produced a copy containing **0 rows** (the table existed
  but was empty). In a second run with 59 committed rows, `cp` captured **1**.
* `.backup` against that same live writer produced **391 rows** with
  `PRAGMA integrity_check` → `ok`.

Two correct options:

1. **Stop the app, then copy.** A clean connection close checkpoints the WAL and removes the
   sidecars, so after a graceful shutdown `cp jobs.db` is sound. This is a perfectly valid
   answer and the simplest one if you can take the downtime. If the process was killed
   (`SIGKILL`, OOM) rather than shut down, the sidecars remain and you are back to case 2.
2. **Use SQLite's own backup, which is safe against a running writer:**

   ```bash
   sqlite3 storage/jobs.db ".backup '/backups/jobs.db'"
   ```

   or, equivalently for our purposes:

   ```bash
   sqlite3 storage/jobs.db "VACUUM INTO '/backups/jobs.db'"
   ```

   Both produce a single self-contained file with no sidecars, and both gave
   `integrity_check = ok` under a concurrent writer.

   One verified difference: **`.backup` preserves `journal_mode=wal` in the output; `VACUUM
   INTO` produces a file with `journal_mode=delete`.** This does not break anything here,
   because `_init()` re-runs `PRAGMA journal_mode=WAL` on the first open, but it means a
   `VACUUM INTO` copy is not byte-comparable with a `.backup` copy. `.backup` is preferred
   below for that reason; `VACUUM INTO` additionally compacts, which is occasionally what you
   want.

If the `sqlite3` CLI is unavailable, the Python module is equivalent via
`Connection.backup()` — see [the Python fallback](#python-fallback-no-sqlite3-cli).

## Backup procedure

Run from the repository root, with `STORAGE` pointing at your `storage_root`
(`storage/` by default; `/app/storage` inside the container, which `docker-compose.yml`
bind-mounts from `./storage`). Safe to run while the app is serving.

```bash
#!/bin/sh
set -eu
STORAGE="${STORAGE:-./storage}"
DEST="${BACKUP_ROOT:-/backups}/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DEST"

# 1. The two SQLite databases, via SQLite's own backup (safe against a live writer).
#    The -f guard matters: `.backup` from a nonexistent source exits 0 and writes a
#    valid but EMPTY database, which is a silent way to back up nothing.
for db in jobs.db history.db; do
  if [ -f "$STORAGE/$db" ]; then
    sqlite3 "$STORAGE/$db" ".backup '$DEST/$db'"
    result=$(sqlite3 "$DEST/$db" 'PRAGMA integrity_check;')
    [ "$result" = "ok" ] || { echo "FAILED: $db integrity_check=$result" >&2; exit 1; }
    echo "ok: $db"
  else
    echo "absent, skipped: $db" >&2
  fi
done

# 2. Small hand-made state. Plain files; cp is correct here.
for f in runtime_config.json profiles.json; do
  if [ -f "$STORAGE/$f" ]; then cp "$STORAGE/$f" "$DEST/$f"; fi
done

# 3. Credentials. Contains secrets; so does history.db (plaintext oauth_tokens).
if [ -f .env ]; then cp .env "$DEST/env.backup"; fi

tar -C "$DEST" -czf "$DEST.tar.gz" .
echo "wrote $DEST.tar.gz"
```

Restrict permissions on the result (`chmod 600`), and encrypt it if it leaves the host: it
contains `.env` and live access tokens.

Large, optional, and separate — these are plain files, so ordinary tools apply, and they do
not need to be quiesced:

```bash
# Irreplaceable for file-upload jobs; re-downloadable for URL jobs.
rsync -a --delete "$STORAGE/uploads/"  /backups/uploads/
# Regenerable at the cost of a re-render; deleted anyway after retention_days.
rsync -a --delete "$STORAGE/clips/"    /backups/clips/
# Pure cost optimisation. Skip unless re-running ASR is painful.
rsync -a --delete "$STORAGE/transcripts/" /backups/transcripts/
```

### Python fallback (no `sqlite3` CLI)

Verified to work; `mode=ro` on the source guarantees the backup cannot modify the live
database.

```python
import sqlite3
from pathlib import Path

for name in ("jobs.db", "history.db"):
    src = Path("storage") / name
    if not src.exists():
        continue
    dest = Path("/backups") / name
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    with target:
        source.backup(target)          # safe against a concurrent writer
    assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    source.close()
    target.close()
```

## Restore procedure

**Ordering constraint: the app must be stopped before you touch these files.** Restoring under
a running process means live connections keep writing through their own WAL and page cache,
and the result is the old data, the new data, or a corrupt mixture. Stop it first.

**The `-wal`/`-shm` files are the trap.** If the previous process was killed rather than shut
down, `storage/` still holds `jobs.db-wal` and `jobs.db-shm`. Copying a restored `.db` into
place while leaving those sidecars behind causes SQLite to replay the stale WAL over your
restored file. Measured: a restore of a 1-row backup over a killed database that had left a
49 KiB `-wal` yielded **500 old rows and none of the restored data — and
`PRAGMA integrity_check` returned `ok`.** The restore is silently and completely discarded,
with no error to notice. Always remove or move aside all three files together.

Backups produced by `.backup`/`VACUUM INTO`/`Connection.backup()` have **no** sidecars, so
there is nothing to place alongside them; you only ever delete the old ones.

```bash
#!/bin/sh
set -eu
STORAGE="${STORAGE:-./storage}"
SRC="$1"                        # directory holding the restored jobs.db / history.db

# 0. Stop the app FIRST (docker compose down, systemctl stop, ...). Then:

# 1. Verify the backups before touching live data. Never restore an unverified file
#    over the only copy you have.
for db in jobs.db history.db; do
  result=$(sqlite3 "$SRC/$db" 'PRAGMA integrity_check;')
  [ "$result" = "ok" ] || { echo "REFUSING: $db integrity_check=$result" >&2; exit 1; }
  echo "verified $db: $result"
done

# 2. Move the current state aside — .db AND both sidecars, together.
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$STORAGE/pre-restore-$TS"
for db in jobs.db history.db; do
  for suffix in "" "-wal" "-shm"; do
    if [ -e "$STORAGE/$db$suffix" ]; then mv "$STORAGE/$db$suffix" "$STORAGE/pre-restore-$TS/"; fi
  done
done

# 3. Put the backups in place. No sidecars to copy.
for db in jobs.db history.db; do cp "$SRC/$db" "$STORAGE/$db"; done

# 4. Small config files, if the backup has them.
for f in runtime_config.json profiles.json; do
  if [ -f "$SRC/$f" ]; then cp "$SRC/$f" "$STORAGE/$f"; fi
done

# 5. Restore .env by hand — review it rather than clobbering the current one.
echo "restored into $STORAGE; previous state in $STORAGE/pre-restore-$TS"
```

Then start the app and check the log. Moving the old files aside rather than deleting them
means a failed restore is still recoverable.

## Integrity verification

```bash
sqlite3 storage/jobs.db    'PRAGMA integrity_check;'   # expect: ok
sqlite3 storage/history.db 'PRAGMA integrity_check;'   # expect: ok
```

`PRAGMA quick_check` is a cheaper subset if the file is large. `PRAGMA foreign_key_check`
returns nothing here, because neither schema declares foreign keys.

Understand the limit: `integrity_check` validates the b-tree structure, not the meaning of
the bytes. It returned `ok` on the botched stale-`-wal` restore above, which had thrown away
every restored row. Structural integrity is necessary, not sufficient — check row counts too:

```bash
sqlite3 storage/jobs.db    'SELECT COUNT(*) FROM jobs;'
sqlite3 storage/history.db 'SELECT COUNT(*) FROM clips; SELECT COUNT(*) FROM publish_attempts;'
```

Two further checks worth running, since neither is covered by `integrity_check`:

```bash
# The JSON blob is opaque to SQLite. This confirms it still parses.
sqlite3 storage/jobs.db 'SELECT COUNT(*) FROM jobs WHERE json_valid(data)=0;'   # expect 0
# Confirm the PB5 column is present (or accept that first start will add it).
sqlite3 storage/history.db 'PRAGMA table_info(publish_attempts);' | grep retry_count
```

### A restored `jobs.db` is not inert on first load

This is important if you are diffing before and after, or restoring to inspect: **the first
start-up rewrites the restored file.** Verified by driving `Job_Persistence` against a
restored database with four rows (`processing`, `queued`, `completed`, `failed`):

1. `load_all()` reads every row and rebuilds it through `Job.from_dict`.
2. Any row whose status is in `INTERRUPTED_STATUSES` — which is exactly
   `{"queued", "processing"}` — is rewritten to `failed`, with `stage = "Interrupted by
   restart"` and an explanatory `error`. If the job recorded a plan and finished some of it,
   the message is the resumable variant naming `<done> of <planned>` clips instead.
3. **That rewrite is persisted**, via `self.save(job)` — it is not just an in-memory view.
   Observed on disk: statuses went from
   `[(j1,processing), (j2,queued), (j3,completed), (j4,failed)]` to
   `[(j1,failed), (j2,failed), (j3,completed), (j4,failed)]`, and the log emitted
   `marked 2 interrupted job(s) as failed after restart`.
4. `JobStore._restore()` then calls `prune(keep=settings.max_persisted_jobs)`, which
   **deletes rows**: `DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY
   created_at DESC LIMIT ?)`. Verified `prune(keep=2)` on a 4-row table removed 2.
   `keep <= 0` is a guarded no-op, so `MAX_PERSISTED_JOBS=0` cannot wipe the store.

So restoring a `jobs.db` with more than `max_persisted_jobs` rows **permanently discards the
excess on first start**. If you need to inspect the full contents, open a copy read-only
(`sqlite3 'file:copy.db?mode=ro'`) instead of starting the app against it. Raise
`MAX_PERSISTED_JOBS` before starting if you want to keep them all.

`history.db` has no equivalent behaviour — nothing prunes it — but its first open may run the
`retry_count` `ALTER TABLE` described above, which is also a write.

## Not covered, and known limitations

* **No automation.** Nothing in this repository schedules, rotates, verifies, or offsites a
  backup. The scripts above are what you get; wiring them to cron/systemd is on you.
* **No point-in-time recovery.** There is no WAL archiving or log shipping. Recovery is to
  the instant of the last snapshot, and everything after it is gone. RPO is your backup
  interval.
* **No consistency *between* the two databases.** They are backed up by two independent
  `.backup` calls, so a job can be present in one and absent from the other. In practice the
  app tolerates this (`history.py` holds no foreign key to `jobs`, and job restore is
  best-effort), but a cross-database point-in-time snapshot requires stopping the app.
* **The JSON blob in `jobs.db` is never migrated.** `data` is opaque to SQLite, and
  `Job.from_dict` handles shape drift by defensive coercion rather than by migration: unknown
  keys are dropped (`{k: data[k] for k in cls.__dataclass_fields__ if k in data}` in
  `ProcessingOptions.from_dict` and `ClipResult.from_dict`), missing keys fall back to
  benign defaults, numerics are coerced in `try/except`, and an unrecognised `status`
  degrades to `FAILED`. This is what makes an old backup loadable at all — but it means
  fields removed by a later version are silently discarded on load, and the loss is not
  reported anywhere. There is no schema version on `jobs.db` to detect it with.
* **`history.db` has no schema version either.** Forward migration works by column
  sniffing (one step, `retry_count`). Restoring a database written by *newer* code into
  *older* code is unhandled and untested.
* **Backups contain secrets in the clear.** `.env`, and `oauth_tokens.access_token` inside
  `history.db`. Encrypt at rest; do not commit; do not ship to a shared bucket unencrypted.
* **Not covered here:** the S3 storage backend (`storage_backend=s3`), for which object
  lifecycle and versioning replace this procedure entirely; and `storage/engines/` retained
  stem WAVs (`stem_retain_stems`), which are large derived artefacts treated like clips.
* **Untested claim, flagged as such:** restoring onto a *different* SQLite major version than
  the one that wrote the backup. Both CLI and Python module here are 3.40.0, and the on-disk
  format has been stable for far longer, so this is very likely fine — but it was not
  exercised.

## Verification: what was actually run

Both `sqlite3` CLI **3.40.0** and Python **3.11.15** (`sqlite3` module linked against
3.40.0) are present in this sandbox, so the CLI commands above are the verified ones and the
Python fallback was verified too. All tests used throwaway databases under `/tmp`, built with
the real DDL copied from `job_persistence.py` and `history.py`. No repository file was
modified.

| # | What was run | Result |
| --- | --- | --- |
| 1 | `PRAGMA journal_mode=WAL` on a fresh file, reopened in a new process | `wal` — confirms WAL is persistent, not per-connection |
| 2 | Live writer with 59 committed rows, then `shutil.copy` of the `.db` only | copy had **1** row; `-wal` was 16512 bytes |
| 3 | Same live connection, `Connection.backup()` | **59** rows, `integrity_check` = `ok` |
| 4 | Background process committing for 6 s; `sqlite3 ".backup"` mid-flight | **391** rows, `integrity_check` = `ok`; writer reached 1179 rows |
| 5 | `cp` against that same live writer | **0** rows |
| 6 | `.backup` and `VACUUM INTO` on a WAL database | both `ok`, both 60/60 rows, no sidecars emitted; `journal_mode` `wal` vs **`delete`** |
| 7 | `kill -9` a writer holding 500 committed rows | `.db` = **4096 bytes**, `-wal` = 53592 bytes, `-shm` present; reopening replayed all 500 |
| 8 | Restore a 1-row backup over that killed db, **leaving** `-wal`/`-shm` | **500 old rows**, restored row absent, `integrity_check` = **`ok`** |
| 9 | Same restore after removing `.db`, `-wal`, `-shm` | **1** row (`NEW1`); `integrity_check`, `quick_check` = `ok`; `foreign_key_check` empty |
| 10 | Real `Job_Persistence.load_all()` on a restored 4-row db | 2 interrupted rows rewritten to `failed` **on disk**; log: `marked 2 interrupted job(s) as failed after restart` |
| 11 | Real `prune(keep=2)` then `prune(keep=0)` | removed 2, then 0 (guard holds); `journal_mode` still `wal` |
| 12 | Real `HistoryStore` opened on a pre-PB5 `history.db` lacking `retry_count` | column added, existing row got `0`, `user_version` stayed **`0`**, re-opening twice more did not raise |
| 13 | The backup script above, **verbatim**, under `sh -eu` | `ok: jobs.db`, `ok: history.db`; tar contained `jobs.db`, `history.db`, `runtime_config.json`, `profiles.json`; skipped the absent `.env` without aborting |
| 14 | The restore script above, **verbatim**, against a `kill -9`'d database with live sidecars | restored value read back as `111` not the stale `999`; `jobs.db`, `-wal`, `-shm` and the old `history.db` all quarantined into `pre-restore-<ts>/` |
| 15 | `sqlite3 missing.db ".backup out.db"` | exit **0**, `out.db` a valid **empty** database — the footgun the `-f` guard prevents |
| 16 | Python fallback with `file:...?mode=ro` source | 1 row backed up, `integrity_check` = `ok` |
| 17 | `json_valid(data)=0` count, `retry_count` presence grep, `quick_check`, read-only URI open | `0`, `6\|retry_count\|INTEGER\|1\|0\|0`, `ok`, opened and queried successfully |
