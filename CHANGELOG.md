# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed — the three red CI crosses on PRs #1-#3, and the reason the boot gate could not catch anything

The failing checks on the first three pull requests were **not** the Actions billing block that has
since stopped CI entirely. They were two distinct, real failures, and both causes are now closed.

**PR #1 — `Frontend (build)` died on "Set up Node":**

```
Some specified paths were not resolved, unable to cache dependencies.
```

`actions/setup-node` was configured with `cache: npm` and
`cache-dependency-path: frontend/package-lock.json`, and that file did not exist — the PR shipped
the frontend with the lockfile left to be "generated on first npm install". An unresolved
cache-dependency path is a **hard failure, not a cache miss**, so the job died four steps before
anything was built, with a message naming caching rather than the missing lockfile or the fact that
`npm ci` cannot run without one. The lockfile has since been committed; a three-line precondition
step now runs *in front of* `setup-node` so a recurrence says what is actually wrong.

**PRs #2 and #3 — `Backend (lint + smoke)` died on "Import & boot smoke test".** That step never
booted anything:

```
python -c "from api.main import app; print('FastAPI app OK', app.title)"
```

**FastAPI does not run the lifespan on import.** So `_run_startup` — storage directory creation, the
writability proof, the job-scoped log filter, `_check_deployment_security()` and the retention
sweeper — executed *nowhere in CI*. The step's name described a boot and its content was an import,
and that gap is the direct reason the un-bootable `render.yaml` above shipped and stayed shipped:
every gate was green because none of them started the app.

Replaced by `scripts/boot_smoke.py`, which enters the real lifespan, serves `/healthz` and
`/api/info`, and asserts in a subprocess that a production environment with no token and wildcard
CORS is **refused at startup** — the difference between "the gate function raises when called",
which was already covered, and "the application will not start", which is what protects a
deployment. It is runnable locally with the same one command, which the inline form was not.

**To be unambiguous about what could not be fixed:** the ❌ marks on merged PRs #1-#3 are immutable
records of check runs against those commits. GitHub does not permit rewriting the conclusion of a
completed check run, so those three crosses stay in the pull request list permanently. What has been
fixed is the cause of each, verified by running the equivalent gate locally on current `main`.

### Changed — every GitHub Action bumped off the deprecated Node 20 runtime

Every failing job above also carried this annotation, and so does every *passing* one, which is how
it went unnoticed:

> Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to run on
> Node.js 24: `actions/checkout@v4`, `actions/setup-node@v4`, `actions/setup-python@v5`

"Being forced to run" is the operative phrase: GitHub is already substituting the runtime, so these
work today and break when the substitution is withdrawn — a breakage with a date on it rather than a
live one. Each action is pinned to the **first major that runs natively on Node 24**, verified
against each action's own `runs.using` rather than inferred from the version number:

| Action | Was | Now | Note |
| --- | --- | --- | --- |
| `actions/checkout` | v4 (node20) | **v5** | |
| `actions/setup-node` | v4 (node20) | **v5** | |
| `actions/setup-python` | v5 (node20) | **v6** | |
| `actions/cache` | v4 (node20) | **v5** | |
| `actions/upload-artifact` | v4 (node20) | **v6** | **v5 is still node20** — the obvious +1 does not fix it |
| `github/codeql-action` | v3 (node20) | **v4** | |

`upload-artifact` is the trap, and the reason the floor is now recorded per action in
`tests/test_ci_workflow_hygiene.py` rather than left to a version bump done by pattern-matching.
The first node24 major is chosen over the newest available to keep the behavioural delta as small
as the problem allows, which matters because **CI cannot currently be exercised to confirm it** —
the billing block means every job runs zero steps. These are verified by reading each action's
manifest and running every equivalent step locally; the first real run after billing is restored is
what confirms them.

`tests/test_ci_workflow_hygiene.py` pins all of the above: no action may target Node 20, the
lockfile guard must run *before* `setup-node` (after it, the damage is done), the backend job must
invoke the boot smoke, and the boot smoke is **executed** rather than string-matched — following
`tests/test_ci_skip_gate.py`, which opens by noting that asserting strings appear in the workflow
would not have caught its own defect, because every string was already correct.

### Fixed — the documented one-click deploy could not start, and nothing tested the manifest

`render.yaml` sets `ENVIRONMENT: production`. `_check_deployment_security()` refuses to boot in
production without `API_AUTH_TOKEN` and refuses again on wildcard CORS. The manifest **declared
neither**: the token key was absent entirely and `CORS_ORIGINS` was `sync: false`, so an operator
who skipped the prompt kept the application default of `*`. Both refusal conditions fired, the
`InsecureDeploymentError` was raised from inside the ASGI lifespan, uvicorn never bound,
`healthCheckPath: /healthz` never answered, and with `autoDeploy: true` every push produced a
failed deploy. The blueprint's own comment claimed the wildcard merely "logs a warning on every
start", which had been false since the gate was tightened.

`API_AUTH_TOKEN` is now declared with `generateValue: true` (Render mints it, it stays out of
version control) and `CORS_ORIGINS` carries an explicit empty value — correct here because one
container serves the API and the built UI from the same origin, so the right origin list is the
empty one.

**Why the suite could not see it.** `tests/test_api_security.py` covers the gate thoroughly with
*monkeypatched* settings: six tests assert what the function does with a given configuration, and
none asked whether the configuration this repository ships is one of the good ones. CI could not
see it either — the "Import & boot smoke test" step only does `from api.main import app`, and
FastAPI does not run the lifespan on import, so the startup gate executes nowhere in CI. The new
`tests/test_deployment_manifest.py` parses the manifest the way an operator's dashboard would and
drives the real gate against it, including a reconstruction of the broken manifest asserted to be
rejected — so the test's own failure mode is proven rather than assumed. It also pins the
`WHISPER_MODEL`-style drift that a comment previously only asked for, checks every declared key is
a real `Settings` field (`extra="ignore"` silently discards the rest), and checks the persistent
disk is mounted where `storage_root` actually writes.

### Fixed — cleanup destroyed the workspace of running jobs

`POST /api/storage/cleanup` defaults `temp` to true and called `cleanup_temp()` with no job id,
which `rmtree`'d every child of `storage/temp/`. A live render works in `storage/temp/<job_id>/`,
so clicking "clean up temp files" mid-render deleted the extracted audio, transcripts and
intermediate segments ffmpeg was reading from. The job then failed in the generic handler with a
message naming a missing temp file — pointing at neither the cause nor the click that caused it.

The file branch of `cleanup_expired` had the same gap. Its sibling directory branch was hardened
against exactly this race with `_EMPTY_DIR_GRACE_SECONDS` and documents it at length; the file
branch never got the equivalent guard, so the background sweeper could unlink an older cached
intermediate from under a resumed job. Age is not evidence that a file is finished with — only the
job's status is.

Both branches now consult `retention.active_job_ids()` and skip anything belonging to a QUEUED or
PROCESSING job. The count kept is returned from the endpoint and the sweep result, so an operator
can tell "nothing was old enough" from "it was all in use". The scoped `cleanup_temp(job_id)` form
deliberately still deletes: `JobManager._cleanup_temp` calls it in a `finally` for the job that has
just stopped, and refusing there would leak every job's scratch space forever.

The sweep also stopped following symlinks. `rglob` does not filter them, so a link under
`storage/clips` was followed and unlinked on mtime alone — deleting outside the storage root.
`_dir_size` already refused to follow links and explained why; the destructive path had no such
guard, which is the wrong way round.

### Fixed — a cancelled job un-cancelled itself, and could still report success

Two halves of one defect: the terminal status had two writers and no handshake.

- **During download**, `dl_progress` updated the job without calling `cancellation.checkpoint`,
  unlike the pipeline's `progress` callback whose first statement is that checkpoint. `cancel()`
  wrote CANCELLED and the next yt-dlp tick — sub-second — wrote `status=PROCESSING` back over it.
  The UI showed "Cancelled", then flipped to "Processing 6%" and kept climbing, and the download
  ran to completion: a multi-gigabyte fetch the user had explicitly stopped still spent the
  bandwidth and still landed in `uploads/`.
- **At completion**, `_execute`'s success path wrote COMPLETED without re-checking the flag, so a
  cancel arriving after the last `progress()` call produced a job the user stopped and the API
  reported as completed.

The worker now gets the last word in both places. Clips that finished before the stop are kept and
reported (`Cancelled - N clip(s) finished`) rather than dropped, because the files are on disk and
mirrored either way — omitting them would only hide them.

### Fixed — `/api/jobs` could 500 intermittently, and persistence could go backwards

`JobStore`'s lock guarded the dict lookup, not the `Job` objects it handed out. API readers then
serialised live objects with no lock while the render thread was inside `update()`: `to_dict`
iterates `self.clips` as `_store_clip` mutates `video_url`/`thumbnail_url` in place, and it hands
out the same `planned_clips`/`stage_timings` list objects that `update()` rebinds. JSON-encoding a
list another thread is replacing raises `RuntimeError: list changed size during iteration` — on the
route the shipped UI polls every 1200 ms. Added `snapshot`/`snapshot_all`/`snapshot_batch`, which
serialise inside the lock, and pointed the read routes at them; `get()` still returns the live
object for the worker, which legitimately wants it.

`_persist` had the matching write-side bug: it re-read the live job outside the lock, so a row could
be written from a state that never existed as a whole, and two overlapping updates committed in
scheduling order rather than in the order they happened — a job coming back from a restart
reporting an earlier stage than it had reached. The payload is now captured under the lock, and the
upsert is guarded by `WHERE excluded.updated_at >= jobs.updated_at` so the newest state wins
regardless of arrival order.

### Fixed — downloading a clip could OOM-kill the API and take every render with it

`download_clip` built the whole archive in a `BytesIO` before sending a byte, so peak memory was
one clip — routinely 100-400 MB — per concurrent request, in a container `render.yaml` provisions
at 2 GB that also hosts whisper and ffmpeg. It had no rate limit, and the token is allowed in the
query string for that path, so it was repeatable from a plain `<a href>`. The kill also takes
in-flight renders, which reappear as "Interrupted by restart" and look like a worker crash rather
than a download. It now streams the zip incrementally, stores the media member instead of
deflating already-compressed H.264, and carries `rate_limit`.

`download_video_only` gained the clip cross-check its sibling already had. Without it, that route
served any existing file in the job's clips directory — sidecar JSON, thumbnails, intermediates —
while the other served only files listed on the job record.

### Fixed — smaller foundation defects

- **Batch upload rollback covered only `HTTPException`.** An `OSError` (disk full) or a client
  disconnect on file 5 of 10 left files 1-4 orphaned in `uploads/`, permanently: retention
  deliberately excludes `uploads`, and the only removal endpoint needs a job record that was never
  created. Now `BaseException`.
- **The batch file count was unbounded.** `max_upload_bytes` is enforced per file, so the real
  ceiling was N x that with nothing bounding N. New `MAX_UPLOAD_FILES` (default 25).
- **`describe_store_failure` called `os.getuid()`**, which does not exist on Windows — the platform
  the function was written for. On native Windows with an unwritable `storage/`, the error
  *formatter* raised `AttributeError` and replaced the diagnostic with exactly the opaque failure
  it exists to prevent.
- **The retention sweeper swallowed every failure with `except Exception: pass`**, so a sweep that
  raised each cycle meant retention never ran and never said so: the disk filled, `last_result`
  stayed `{}`, and `/api/storage` reported healthy usage with no error field. Now logged, with the
  failure recorded in `last_result`. Failing to *start* the sweeper at boot was silent too.

### Fixed — BlazeFace never once ran in the shipped image, and the smoke test certified it

**Found by running the tool on footage with faces in it for the first time.** Every previous
exercise used a synthetic `testsrc2` pattern, which is why this survived: the only thing that
reveals it is a face.

`/api/info` reported `mediapipe: available: true`. `scripts/docker_smoke.sh` asserts on that field
and passed. And every render logged:

```
face detector: mediapipe requested but could not be imported or constructed; falling back to haar
```

**10 of 10 clips** carried `face_detector_substituted:mediapipe:haar`. The container advertised
BlazeFace and had never used it.

Two independent faults, and each hid the other:

- **The Dockerfile was missing `libegl1` and `libgles2`.** `mediapipe/tasks/c/libmediapipe.so`
  declares `NEEDED libGLESv2.so.2` and `NEEDED libEGL.so.1`, and that library is dlopen'd **only
  when a task graph is constructed** — so `import mediapipe` succeeds and
  `vision.FaceDetector.create_from_options` raises `OSError: libGLESv2.so.2: cannot open shared
  object file`. Confirmed inside the image, then fixed and confirmed again. `.github/workflows/
  ci.yml` installs all four libraries and explains why at length; the image only ever received the
  first two, so **CI exercised the real detector and the shipped container never did**.
- **The capability probe asked the wrong question.** It did `import mediapipe` plus a model-size
  check, both of which pass in an image that cannot build a graph. It now *constructs* a detector
  via the reframer's own `_mediapipe_detector`, so the probe answers the question the render path
  will ask, and closes it again rather than leaking a graph per call. A missing model is still
  reported distinctly from a broken one, and a probe failure still cannot take `/api/info` down.

This is the same shape as the `ruff check . || true` and the coverage-gate bugs this project keeps
finding: a check that could not fail, reporting something it was not measuring. The smoke test was
not merely silent here — it actively certified the defect.

Verified end to end on public-domain NASA interview footage (faces detected in 15 of 15 sampled
frames): before, 10 of 10 clips substituted Haar; after, all 10 record `face_detector:mediapipe`
with zero substitution warnings, and the XNNPACK delegate initialises. `docker_smoke.sh` still
passes — but now its assertion means something. Six tests pin the probe to construction so it
cannot drift back to import-only.

### Added — URL ingest can authenticate, so YouTube's bot gate is no longer a dead end

Pasting a YouTube link failed, and there was nothing the user could do about it:

```
Download failed: ERROR: [youtube] <id>: Sign in to confirm you're not a bot.
Use --cookies-from-browser or --cookies for the authentication. See <two wiki links>
```

yt-dlp names the fix, and **the application exposed no way to apply it** — a grep of the tree found
no `cookiefile`, no `cookiesfrombrowser`, no setting. So the error relayed two CLI flags this app
does not have and two wiki pages, describing a remedy the reader could not act on.

This is not an edge case. The gate keys on the **requesting IP**, not the video, so a server or a
container is gated near-universally and home connections increasingly. It was invisible in the suite
because the URL-ingest tests mock yt-dlp, so that path had never met the real gate.

- **`YTDLP_COOKIES_FILE`** — a Netscape `cookies.txt`. The only option that works in Docker, where
  `--cookies-from-browser` cannot work at all because there is no browser. `~` is expanded, since
  that is what a person types and yt-dlp does not expand it.
- **`YTDLP_COOKIES_FROM_BROWSER`** — the desktop convenience, `chrome` or `chrome:Profile 1`,
  converted to the `(browser, profile, keyring, container)` tuple yt-dlp expects. Both settings may
  be set together; yt-dlp merges the jars.
- **Applied to both yt-dlp entry points.** `fetch_metadata` makes a real request and hits the same
  gate, and the UI calls it *first*, so authenticating only `download_video` would have left the
  feature looking broken.
- **The gate is translated into an actionable message** naming the settings that exist instead of
  flags that do not. Matched on the stable fragments of yt-dlp's wording rather than the full
  sentence, which has been reworded before and carries a Unicode right single quote in "you're".
  An age gate (`Sign in to confirm your age`) is deliberately *not* matched — different problem,
  different fix, and mislabelling it would send the reader after cookies they do not need.
- **A misconfigured path names itself.** yt-dlp treats an absent cookie file as fatal with text that
  mentions neither the path nor that it came from configuration, so a typo read as an unrelated
  failure. Now: `YTDLP_COOKIES_FILE points at /app/storage/typo-cookies.txt, which is not a file.`

Verified in a container against a genuinely gated video: the job now fails with the hint rather than
the yt-dlp dump, and with a deliberately wrong path it reports the path. 18 tests in
`tests/test_url_ingest_cookies.py`, none touching the network — they assert the options handed to
yt-dlp and the message handed to the user, which is where both defects were.

Anonymous ingest is unchanged: with neither setting configured the options dict is empty, asserted
directly, so no existing deployment changes behaviour.

### Added — caption sync is now a number, not an argument (M10, label-free)

A desync was reported against rendered clips, and there was no way to settle it. `evaluation/
caption_timing.py` was library-only — `scripts/check_wired.py` records it as a hand-run instrument —
and **every one of its twenty tests fed it synthetic labels and synthetic events**. So the instrument
was proven and the pipeline was not: nothing in the suite measured a real render.
`tests/test_transcript_trim.py::test_captions_follow_the_cut` came closest, but it spies on
`rebase_words` and asserts a rebased word *number*, so it would still pass if the number were right
and the rendered media were shifted underneath it.

- **`speech_mask` / `coverage_overlap` / `best_fit_lag_ms` in `evaluation/caption_timing.py`.**
  Measures "a caption is on screen" against "sound is happening", both read from finished artefacts,
  **with no labels at all**. Labels would mean hand-transcribing or re-running ASR, and re-running
  ASR makes the measurement circular — ASR is where the caption times came from. That is not
  hypothetical: whisper-derived labels put the mean error at **−944 ms** on clips whose captions were
  in fact aligned, because only 5 of 47 words matched and the mean was taken over the survivors.
  IoU rather than plain overlap, so a cue list that simply covers the whole clip cannot score well.
- **`scripts/measure_caption_sync.py`** — the entry point that was missing. Reports lag, overlap at
  zero, and overlap at the best-fit lag together, because the three columns distinguish cases the
  lag alone cannot: a *consistent* lag is an arithmetic bug, lags disagreeing in sign are per-word
  ASR jitter that no constant compensation fixes, and a large lag whose overlap barely improves is
  the search finding a spurious alignment in continuous speech — noise, not a finding.
- **`tests/test_caption_sync_rendered.py`** renders a clip with real ffmpeg and measures the cues
  the ASS emitter actually received against the finished file's own audio. No ASR: the source is a
  1-second tone every 2 seconds, so the truth is constructed. The window starts at **4 s, not 0** —
  a missing or doubled clip-start subtraction is invisible at zero, and that is the defect this
  exists to catch. Verified by injecting it: deleting the `- start` in `slice_words` makes the test
  fail with `captions best fit the audio -2000 ms off zero` and `the first cue starts at 4.00s in a
  clip cut from 4.0s`. A fourth test shifts every cue by a whole burst period and asserts the metric
  notices, so the guard cannot silently become decoration.

### Investigated — the reported desync is per-word ASR jitter, and three plausible fixes were rejected on measurement

Recorded because each would have been a defensible-sounding change with no benefit, and the next
person should not have to re-derive that.

- **No constant offset exists.** Across ten rendered clips the median best-fit lag is **−0.04 s**,
  two envelope frames, well inside the 100 ms this module records as perceptible. The first cue
  fires at audio onset on nine of ten. The clip-start arithmetic is correct.
- **The four worst clips measured +1.52, −1.52, −1.16 and +1.58 s** — and gained only 3–10 points of
  overlap at those lags. Disagreeing signs plus a marginal gain is per-word jitter, not a shift.
- **Raising ASR precision does not help.** Measured on the same 120 s source: `small`/`int8` (the
  current default) **81.4%** overlap, `small`/`float32` **80.7%** at 3.4× the time, `medium`/`int8`
  **79.6%**, `medium`/`float32` **81.1%** at 9× the time. The cheapest configuration is the most
  accurate of the four, so `WHISPER_COMPUTE_TYPE` is left alone.
- **Forced alignment was prototyped and rejected, and it nearly caused a regression.** `torchaudio`'s
  `MMS_FA` CTC aligner improved the edge-anchored median from 130 ms to 110 ms — 15%, below the 20%
  threshold fixed before running it — for a **1.18 GB** model and a `torch` dependency. Then the
  interesting part: comparing whisper against it gave **−94, −104 and −105 ms** across three
  recordings (two windows of one voice, plus a synthesised second voice). Consistent sign, 12 ms
  spread, three sources; every heuristic for a real systematic bias satisfied, and the indicated fix
  was a calibrated +100 ms shift. **That would have injected 100 ms of error into a component that
  was correct.** Measured against pause-preceded words — where 300 ms of silence lets the audio
  settle the onset with no model involved — whisper reads **−10, −20 and +50 ms**. It is accurate.
  The 100 ms belonged to `MMS_FA`, whose CTC spans open at the first strongly-voiced frame and so
  start after fricatives and plosive releases that belong to the word.
- **`verifiable_word_errors` makes both traps unrepeatable.** It measures word onsets only where a
  real pause makes them verifiable and **skips the rest rather than estimating them** — which is
  precisely what the 130 ms figure was: a confident number reported for every word when only about
  one in ten carried information. Its tests cover the continuous-speech case explicitly, asserting
  that nothing is reported there, and the sign convention, which is what decided whether a
  "correction" would have gone the right way. Note the noise floor is invisible synthetically: on
  gated tones the same code reads 0 ms on known-true times even at 3.3 bursts per second, so a
  synthetic check makes the broken metric look validated.
- **T11 onset snapping is still not the answer**, and `worker/word_spans.py` now carries the second
  measurement. Its original note said a ~20 ms envelope would be needed and that R7.8 forbids the
  second audio pass. One now exists in `evaluation/` — where an instrument may spend a pass the
  render path may not, so R7.8 stands — and it shows whisper's spans already track speech at 81.4%.
  There is no gross mis-timing to recover, and snapping in near-continuous speech has onsets
  everywhere to choose from. Forced alignment would address the residual; onset snapping would not.
- **The sidecar/burn-in grouping difference is deliberate and was left alone.** It looks like a bug
  from the outside — `words_to_cues` groups 3 words to 3.0 s, `cues_from_words` 8 words to 5.0 s, so
  the `.srt` genuinely does not match what is burned in — but `worker/subtitle_export.py` documents
  the reason: three-word cues in a player's own small subtitle type flicker once a second. Two jobs,
  two groupings. `scripts/measure_caption_sync.py` therefore prefers the `.ass` and treats the
  `.srt` as a fallback, noting which it used, rather than pretending they are interchangeable.

### Fixed — `cp .env.example .env && docker compose up` could not boot the app

**Found by actually running it.** The README's quickstart is `cp .env.example .env`, then
`docker compose up --build`. Following it produced a container that died on import with
`17 validation errors for Settings`, and the same file run directly on the host worked perfectly.

The cause is that two loaders read this file and they do not agree:

| | inline `# comment` | surrounding quotes |
|---|---|---|
| python-dotenv (pydantic-settings, running directly) | stripped | stripped |
| Docker `env_file` / `--env-file` | **kept** | **kept** |

Docker takes everything after the first `=` to the end of the line. So
`OUTPUT_SHORT_SIDE=1080           # 720 | 1080 | 1440 | 2160` reached the container as the string
`1080           # 720 | 1080 | 1440 | 2160`, and `APP_NAME="AI Video Clipper"` arrived with the
quote characters still in it. 39 lines carried an inline comment and one was quoted.

**The 17 loud failures were the better half.** The other 22 were string-typed, so pydantic
*accepted* them and the comment became part of the value: `WHISPER_MODEL` was
`small              # tiny | base | small | medium | large-v3`, a model name that does not exist,
and `FACE_DETECTOR_BACKEND`, `CAPTION_MODE`, `BACKGROUND_STYLE` and `TRANSITION_STYLE` likewise
held values matching no branch. Fixing only the settings that errored would have produced a
container that booted and then quietly misbehaved.

- **Every inline comment moved onto its own line above its setting, and `APP_NAME` unquoted.** The
  documentation is unchanged in content — it is only positioned where both loaders agree.
- **`tests/test_config_documentation.py` now enforces both rules.** The existing gates there proved
  every setting was *documented*; they could not prove a documented value *parses*, which is the
  gap this shipped through. Deliberately textual checks: reproducing them by loading the file would
  mean picking one of the two loaders, and the defect lives in the disagreement between them.

### Fixed — `FOO=` in a `.env` meant `""`, not "unset", and pointed the I3 cache at the CWD

The same `cp .env.example .env` exposed a second fault. That file ships 33 keys with an empty
value, 25 of them optional with a comment saying the default applies — `INTERMEDIATE_CACHE_DIR=`
is annotated "empty = `<TEMP_DIR>/intermediates`". That sentence was false. An environment variable
that is present but empty is the string `""`, not absent, so pydantic validated it instead of
falling back to the field default.

For the 24 `str | None` keys this was invisible: `""` and `None` are both falsy and every call site
tests truthiness. `intermediate_cache_dir` is `Path | None`, so it became **`Path(".")`** — and
`worker/intermediate_cache.py` selects with `settings.intermediate_cache_dir or temp_dir /
"intermediates"`, where `Path(".")` is truthy. The I3 cache is enabled by default, so it wrote
`envelope-*.json`, `silences-*.json` and `frames-*/` straight into the process working directory.
On a host that is the repository root; in the container it is `/app`, which is root-owned while the
app runs as UID 10001, so the documented quickstart aimed a write-heavy cache at a directory the
application cannot write. It also escaped the boot-time writability probe added in 0.12.4, because
`intermediate_cache_dir` is in neither `_REQUIRED_DIR_FIELDS` nor `_OPTIONAL_DIR_FIELDS`.

- **`Settings` now normalises a blank value to `None` for every optional field**, via a
  `model_validator(mode="before")`. Normalising rather than deleting the keys from `.env.example`
  keeps them documented and discoverable, and makes the sentence they carry true. Only optional
  fields are touched — emptying a required one still fails loudly instead of silently defaulting.
- **`tests/test_settings_empty_env.py`** asserts the general rule across all 25 optional fields
  rather than the single field that happened to hurt, plus the concrete case: that `cache_dir()`
  resolves under `temp_dir` and not the CWD.
- `tests/test_storage_writability.py::test_every_directory_setting_is_classified` was already
  capable of catching this and did, the moment a `.env` existed. It had simply never run with one.
- **`storage/.cache/` is now git-ignored.** The Dockerfile points `HF_HOME` there, so the first
  `docker compose up` that transcribes anything left ~460 MB of whisper weights in `git status`.

Verified by running the whole tool in Docker, not just its tests: image built, `.env` copied from
the fixed `.env.example`, then a 120-second 1920x1080 source with real speech uploaded through
`POST /api/upload`. Whisper transcribed it, the selector chose moments, and ffmpeg rendered
**10 vertical 1080x1920 h264/aac clips** with burned-in karaoke captions, thumbnails and per-clip
metadata JSON, in 156 seconds, `status=completed`. The I3 cache landed in
`storage/temp/intermediates` and the working tree stayed clean.

### Fixed — the pinned VMAF ffmpeg was immutable but not durable, and CI cannot fetch it

The backend job's "Fetch the VMAF-capable ffmpeg" step pinned
`BtbN/FFmpeg-Builds@autobuild-2026-08-07-13-13`. That URL now returns **404**: BtbN prunes its
releases, keeping roughly the last fourteen daily autobuilds plus the last-of-month build for each
month going back about two years. The pin was a *mid-month daily*, so it was deleted upstream
fourteen days after it was chosen.

The reasoning recorded alongside the pin — immutable tag, version-stamped asset, checksummed, no
floating `latest` — is right and stands. It just secured the wrong property. Immutable means the
bytes behind the URL cannot change; durable means the URL still resolves. Only the first was
checked, and the failure mode of the second is a hard CI failure rather than a wrong reading.

This was invisible because the step is `if: cache-hit != 'true'` and the cache was warm when the
pin was made. The repository now holds **zero** Actions caches (GitHub evicts entries unused for
seven days), so the step will run — and fail — on the next build.

- **Re-pinned to `autobuild-2026-07-31-14-10`**, a month-end tag, which carries the *identical*
  asset: same ffmpeg revision `n7.1.5-12-g1fdbca85aa`, same filename, `libvmaf` verified present.
  So this is a hosting change, not a measurement change — nothing about `compare()` refusing to
  difference readings across builds is affected. Life expectancy goes from ~2 weeks to ~2 years,
  and the comment in `ci.yml` now states the retention rule so the next bump picks a month-end tag
  on purpose rather than by luck.
- **`curl` now runs with `--fail`.** Without it curl wrote GitHub's 9-byte `Not Found` body to the
  output path and exited 0, so a pruned release surfaced as
  `sha256sum: WARNING: 1 computed checksum did NOT match` — which reads as a tampered binary and
  sends you hunting a supply-chain compromise that is not there. A missing release and a changed
  release need opposite responses, so they no longer share a symptom; the 404 path now says which
  it is and names the remedy.

Verified by running the whole backend job locally against the new pin: `ruff check`,
`ruff format --check`, `mypy` (118 files), the four vendored-asset gates, `check_wired.py`, and
**2805 tests passing with no skips** with `VMAF_FFMPEG_BINARY` pointed at the newly fetched binary,
plus both frontend legs (node 20.20.2 and 22.23.2: `npm ci`, eslint, 141 vitest tests, build,
`npm audit`) and `scripts/docker_smoke.sh` end to end.

## [0.12.4] - 2026-08-21

### Fixed — the same 500, from a second cause: storage writability was never checked

**Reported from a real run** in Docker Desktop on Windows, and it is the *same endpoint and the same
sentence* as 0.12.3 — the container booted, served `/healthz` and the dashboard, then `GET /api/history`
returned a 500 ending in `sqlite3.OperationalError: attempt to write a readonly database`. The traceback
had moved one line down, from the WAL pragma to the `executescript` beneath it.

**The 0.12.3 entry above contains a claim that is wrong, and it is the reason this took two attempts.**
It argued that `storage/` was "perfectly writable", on the grounds that `ensure_local_dirs()` "had
already created `uploads/`, `clips/` and `temp/` inside it during startup, and the app could not have
booted otherwise". That inference does not hold. `ensure_local_dirs()` called
`mkdir(parents=True, exist_ok=True)`, which does **nothing at all** when the directory is already
there — and `storage/uploads`, `storage/clips`, `storage/temp` and `storage/transcripts` are committed
to this repository as `.gitkeep` files, so they are present in every clone. Startup wrote nothing, so
booting proved nothing.

Both faults were real and independent. The WAL fix in 0.12.3 is correct and stands. Underneath it was a
genuinely unwritable bind mount, which nothing looked for.

- **`ensure_local_dirs()` now proves writability with a real write** — create a file, remove it — rather
  than inferring it from existence. Not `os.access`: that reports mode bits, and every case that bites
  here (a read-only bind mount, a container UID no ACL entry covers) can present bits that look fine.
  Writing is the only way to find out whether writing works, which is why the ffmpeg capability probes
  in this project shell out to a real binary.
- **Required and optional directories are now distinguished.** The five under `storage/` are fatal at
  boot: uploads, renders and both SQLite databases live there, so an unwritable `storage/` is not a
  degraded mode. The four under `assets/` only warn — the vendored fonts, emoji and models are committed
  and read-only, writes happen only for optional extras (a non-default emoji style, the b-roll cache),
  and `assets/` mounted `:ro` is a supported way to run.
- **Both stores now translate a filesystem failure into a message that names it.** SQLite's own wording
  identifies neither the file nor the directory, and blames the database for what is usually a directory
  it cannot create a journal in. The new message resolves the actual state — missing directory,
  unwritable directory, unwritable file — and carries SQLite's original text plus the remedy. Applied to
  `publishers/history.py` and `worker/job_persistence.py` both, because that pattern has already been
  duplicated once and only one copy was reported.

The boot failure now reads:

    RuntimeError: storage_root (/app/storage) is not writable: [Errno 13] Permission denied:
    '/app/storage/.write-probe-2'. The application stores uploads, rendered clips and its SQLite
    databases there, so it cannot run. In Docker this is the storage bind mount: the image runs as
    UID 10001 and a bind mount keeps the host directory's ownership, so either grant it once with
    `sudo chown -R 10001:10001 storage`, or switch the mount to a named volume (see
    docker-compose.yml), which Docker creates with the image's ownership.

`docker-compose.yml` gains a named-volume alternative, commented and ready to swap in: Docker creates a
named volume with the *image's* ownership, so host ownership stops being something anyone has to think
about. The stale comment claiming the container would "exit immediately with `PermissionError`" is
corrected — it was verified against a checkout in which those directories did not exist, which a real
clone is not.

Verified in a rebuilt image as UID 10001, reproducing the report exactly before fixing it: an
unwritable mount with a pre-existing `history.db` produces the reported traceback byte-for-byte on the
old image, and a named boot failure on the new one. With the mount made writable, `/api/history` returns
**200**, `history.db` is created owned by 10001, and the log has zero `readonly`/`OperationalError`
entries. A read-only `assets/` mount boots and warns four times.

Ten mutations, all caught (`tests/mutations/storage_writability.json`). One escaped initially and the
mutation itself was wrong rather than the tests: it *added* an `os.access` check while leaving the real
write in place, which is stricter rather than weaker and reintroduces nothing. Replacing the write is
the defect; preceding it is not.

Gates: pytest 2799 passed, 0 skipped, 0 warnings; ruff, ruff format (242 files), mypy (118) clean;
frontend eslint/vitest/build clean; `scripts/docker_smoke.sh` green.

## [0.12.3] - 2026-08-21

### Fixed — SQLite could not start on a Docker Desktop bind mount, so the dashboard 500'd

**Reported from a real run** in Docker Desktop on Windows. The container booted, served `/healthz`,
served the dashboard — then `GET /api/history` returned a 500 ending in:

    File "/app/publishers/history.py", line 56, in _init
        db.executescript("""
    sqlite3.OperationalError: attempt to write a readonly database

**The message names the wrong cause, which is what made this worth chasing.** `storage/` was
perfectly writable — `ensure_local_dirs()` had already created `uploads/`, `clips/` and `temp/` inside
it during startup, and the app could not have booted otherwise. Nothing was read-only.

`PRAGMA journal_mode=WAL` is the one pragma in either store that depends on the **filesystem** rather
than on SQLite. WAL needs a shared-memory `-shm` sidecar and mmap, which SMB, CIFS, virtiofs and 9p do
not provide — which is to say, Docker Desktop bind mounts on Windows and macOS. SQLite reports its
inability to create those sidecars as the database being read-only, so the error points at permissions
and the real problem is the journal mode.

Two changes follow:

- **WAL is now requested separately from the schema.** Inside one `executescript`, a WAL failure takes
  the `CREATE TABLE` statements down with it — so the store was left unusable on a filesystem where
  nothing was actually wrong. That is the reported crash, and it is why the fix is structural rather
  than a wider `except`.
- **WAL is treated as an optimisation, not a requirement.** It is attempted, and its refusal is logged
  **once** with the situation named, so the next person does not diagnose a permissions problem that
  does not exist. The default rollback journal is slower under concurrent writes and completely
  correct.

**Both SQLite stores had the identical pattern, and only one was reported.**
`worker/job_persistence.py` carried the same pragma inside its own schema script. It went unreported
purely because the jobs database is created lazily on the first job while the history store is touched
by the dashboard — so history was reached first. The jobs database would have failed on the next
click, and job tracking would have gone with it. Fixed in both; the helper lives in `worker/` because
`publishers/` already imports from there and not the reverse.

Verified in a rebuilt image on a bind mount: `/api/history` — the exact endpoint from the report —
returns **200**, `/api/campaigns`, `/api/jobs` and the dashboard all serve, `history.db` is created,
and the log contains **zero** `readonly database` or `OperationalError` entries. Four mutations, all
caught; one initially escaped because the test proxy only refused WAL via `execute`, so moving the
pragma back inside `executescript` — the actual bug — slipped through.


## [0.12.2] - 2026-08-15

### Fixed — the Docker image could not transcribe, so it could not process a video at all

**Found by building and running the image, which nobody had ever done** — CI is billing-blocked, and
the release before this one shipped a Dockerfile that had never been executed.

- **`PermissionError: '/home/clipper'` on the first video.** The image creates its user with
  `--no-create-home`, deliberately, so `$HOME` names a directory that does not exist. `faster_whisper`
  resolves its model through `huggingface_hub`, which writes to `$HOME/.cache/huggingface` — so the
  **first real pipeline step died**, several layers from its cause, on a container that had booted
  perfectly cleanly and served `/healthz` for minutes beforehand. The visible half of the same defect
  had been in the logs all along: matplotlib complained about `$HOME/.config` on every single boot.

  Fixed by pointing `HF_HOME`, `XDG_CACHE_HOME` and `MPLCONFIGDIR` at `/app/storage/.cache`.
  `/app/storage` is the correct target for two independent reasons: it is `chown`ed to the container
  user, and `docker-compose.yml` bind-mounts it — so the ~460 MB model is fetched **once** and
  survives `docker compose down` rather than being re-downloaded on every start. Verified in the
  rebuilt image with no environment overrides: the model loads and 75 MB persists to the host mount.

- **A stale comment in `docker-compose.yml`, corrected by observation.** It said a non-writable
  `storage/` "surfaces as jobs failing rather than as a permission error at boot". Running the image
  showed the opposite: `ensure_local_dirs()` runs during startup, so the container exits immediately
  with `PermissionError: '/app/storage/uploads'` and never serves at all. Failing fast is the better
  behaviour — the note simply described an older one. Also records that the `chown` is unnecessary on
  Docker Desktop for Windows and macOS.

- **Two parity goldens were made host-dependent by 0.12.1 and are now pinned.** Dropping undrawable
  emoji means rendered output depends on the installed fonts, so the v0.8.0 goldens passed on a machine
  with Noto Emoji and failed on one without — a difference that is not a code change. `glyph_available`
  is now pinned alongside `font_available`, which this test file already does for exactly this reason
  and says so: *"font substitution is a property of the host."* **No golden bytes were re-frozen**; the
  documents still expect the emoji, and the fix is to control the input rather than to accept the
  output.


## [0.12.1] - 2026-08-13

### Fixed — in-caption emoji were burned into every clip as missing-glyph boxes

- **The glyph-availability check existed and was never wired.** `caption_emoji_glyph` has always
  accepted an injectable `glyph_available` callable and documented that "a glyph the active font
  cannot render is dropped while surrounding words are retained" — but **no production caller ever
  passed one**, so it defaulted to `lambda _g: True` and the guard asserted that every emoji renders.
  The only caller that supplied a checker was a test.

  The `Dockerfile` installs `fonts-liberation` and the bundled display faces and **no emoji font at
  all**, and `caption_emoji` defaults to `True`. So the shipped image burned a **▯** into every clip
  whose transcript hit a mapped keyword. Found by rendering a clip and looking at the frame — the
  caption read `gone. the secret ▯`. No test failed, because an optional dependency whose default
  disables the feature it guards is indistinguishable from not having written it. Same shape as the
  five features this release found had no importer, one layer down.

  **The check is per glyph, not per font, and that is not fussiness.** Measured on a host with
  `google-noto-emoji-fonts` installed: U+1F4B0 (money bag) is present and U+1F92B (shushing face) is
  **absent from the same font**. Installing an emoji font is therefore not a fix, and a font-level
  check would still ship boxes. Coverage is asked of `fc-list ":charset=<hex>"`, which is the same
  resolution libass performs — it does not restrict itself to the caption family, it falls back
  through fontconfig for any glyph the requested face lacks.

  Every codepoint in a sequence must be covered, because one uncovered member breaks the cluster;
  variation selectors and the ZWJ are excluded, since they carry no outline and no font advertises
  them, so requiring them would reject every emoji sequence.

  **Conservative when it cannot tell.** With no fontconfig, or a failing one, the answer is "renders"
  — dropping an emoji that would have appeared is a visible edit made on no evidence, and the failure
  being guarded against is the opposite one.

  A drop is recorded as `caption_emoji_unavailable:<n>`, counted per **distinct glyph** rather than
  per occurrence: what an operator would go and fix is a font coverage gap, not an occurrence.
  Silently omitting it would look identical to the keyword map simply not covering the word, and only
  one of those is actionable.

  Verified end to end on a rendered clip: `money 💰` is kept (the installed font has it), the
  shushing face is dropped, and the delivered subtitle file contains no codepoint outside the BMP
  that cannot be drawn. Five mutations, all caught — including reverting the default, which is the
  original bug.

  **Not bundled here, deliberately:** adding an emoji font to the image would let the *covered*
  glyphs render rather than being dropped. That is a Docker change I cannot verify in this
  environment (the image has never been built here, and libass colour-emoji support is uneven), so it
  is left as a follow-up rather than shipped untested.


## [0.12.0] - 2026-08-13

**The release that made the features it already had actually run.**

Five complete, tested features shipped in earlier versions and were called by nothing: the caption
timing passes (C23/C24/C25), stabilisation (V21), per-speaker level matching (AU12), face-aware caption
placement (V15) and sound effects (A15). Every gate was green the whole time, because a unit test of a
pure function cannot tell whether anything calls it. All five are now wired, `scripts/check_wired.py`
enforces that no new module or setting can go dark, and both of its baselines are empty.

Alongside that: HDR sources are no longer delivered grey and flat, thirteen settings that silently did
nothing are resolved, and two new features arrive off by default (V23 subject-scale normalisation and
S21 cold-open assembly), each with the measurement or trial its default is waiting on named rather
than guessed at.

**Potentially breaking.** Eight documented environment variables were **retired** because they
described behaviour this project does not have — `API_HOST`, `API_PORT`, `REDIS_URL`, `RQ_QUEUE_NAME`,
`USE_INPROCESS_FALLBACK`, `PUBLIC_BASE_URL`, `X_API_KEY`, `X_API_SECRET`. None of them ever had an
effect, and `Settings` uses `extra="ignore"`, so a stale key left in an existing `.env` stays harmless.


### Added — S21 cold-open assembly: a clip may open on its strongest line

- **`S21` lifts a clip's strongest sentence to the front.** Every clip this project delivered was one
  contiguous range, so a hook eighteen seconds in stayed buried. `worker/assembly.py` chooses the line,
  builds the keep list, and rebases everything timed against it.

  **No new way to cut video.** `worker/effects/filler.py` already renders a non-contiguous keep list in
  **one** re-encode, so assembly reuses it (R2.1, R2.2). Two of its properties turned out to be
  load-bearing, and both were established by reading it rather than assumed: it iterates the keep list
  in the order given and never re-sorts, and it builds the video and audio trims from the **same loop
  variable** — so R2.7 ("never reorder audio and video independently") holds by construction.
  `filler._merge` *does* sort, and must never be reached with an assembly; a mutation covers that.

  **Filler removal, the U4 cut list and the assembly resolve into one keep list** (R2.3). The assembly
  supplies the outer ordering and the earlier removals act as an inner filter, because doing it the
  other way round would express the subtractions against the assembled timeline rather than against
  the source offsets everything else refers to.

  **The non-monotonic rebase is the whole risk, and it has two halves.** The obvious one is order: an
  assembly is `[hook, body]` with the hook's source times *after* the body's, and a rebase that sorts
  would caption the hook at the body's positions — plausible enough to be blamed on the ASR. The
  quieter one is duplication: when the lifted line is *retained*, its source range is in the keep list
  **twice**, and `filler.rebase_words` stops at the first match. It would caption the first airing and
  leave the second silent. `assembly.rebase_onto` emits one item per occurrence, and words, emoji cues
  and speaker turns each get their own builder and their own test — one rebased consumer working does
  not imply three.

  **Guards, each with a reason.** It never lifts a sentence that is itself a dangling opener (R1.6):
  the cold open is the one position where a back-reference cannot resolve, because nothing came
  before. It never reorders a clip whose strongest line is already first (R1.5). It refuses a cold open
  that is half the clip or more, which would make the delivery a repeat rather than an edit. Where
  retaining the line would put the two airings within the repeat gap, it removes it from the body
  instead of refusing — an assembly that is fine in one configured form should not be abandoned for
  failing the other — and the length floor (R1.9) outranks that, because a clip under its preset's
  minimum is a broken deliverable while hearing a line twice is a style someone chose.

  **A finding about the existing hook scorer.** `hook_score.text_signal` is sparse: it returns 0.0 for
  any sentence without opener-ish wording, which is most of them. On a flat clip every candidate ties
  at zero and the earliest-index tiebreak returns sentence 0 — which, reported as "the strongest line
  is already first", would be a false claim about the material. `no_signal` and `already_first` are
  therefore distinct findings and tested separately.

  Off by default (R1.10), and the spec requires this default to come from a blind **preference trial**
  rather than an opinion, because it reorders the edit itself. Ten mutations are specified in
  `tests/mutations/clip-editorial-structure-assembly.json`; three initially survived and each exposed a
  real test gap — the call site being untested while the module was covered, a vacuous duplicate check
  (the fixture repeated ordinary words, so "some word appears twice" was true either way), and no test
  driving the pipeline into a refusal at all.


### Added — V23 subject-scale normalisation, and a measured reason it is not built the way the spec asked

- **`V23` keeps the speaker a similar size across cuts.** A clip cut together from a close-up and a
  wide shot delivers a speaker who changes size at every cut; reframing follows the face's *position*
  and says nothing about its *size*, so the jump survives reframing intact. `worker/subject_scale.py`
  measures face height per shot, plans a bounded magnification, and steps it at cut boundaries.

  **The spec's own mechanism is unavailable, and that was established by measurement rather than
  assumed.** R2.2 asks for the **crop size** to be adjusted per shot, and `crop`'s `w`/`h` are
  advertised as commandable (`T` in `ffmpeg -h filter=crop`), so a `sendcmd` script changing them is
  the obvious implementation. On the ffmpeg this project ships (7.0.2) it **aborts the CLI**:
  `Assertion best_input >= 0 failed at src/fftools/ffmpeg_filter.c:1923`. Verified three ways —
  `crop x`/`crop y` alone renders fine, a `crop w`/`crop h` command fails, and `scale=...:eval=frame`
  does not rescue it. Changing a crop's output dimensions reconfigures the filter link and the
  command-line tool cannot follow that mid-stream. **A test asserts the crash from both sides**, so if
  a future ffmpeg fixes it that test fails — which is exactly the notification needed to revisit this.

  **So the mechanism is a magnification step**, `zoompan` with a `z` expression constant within a shot
  and changing only at a cut — the same shape the existing `zoom_cut` style already uses, and one that
  never reconfigures the link. It sits between `crop` and `scale`: after the crop because it magnifies
  within the window the tracker chose, and before the scale because `scale` renormalises whatever
  arrives to the delivery size, which is what lets magnification differ per shot while the output
  dimensions never move. Measured on a synthetic source, a 1.0 → 1.3 step moves mean luma 23.5 → 28.7
  against a flat control that stays at 23.5, so the effect is real and the comparison is not vacuous.

  **R2.4 is a property of the expression's shape, not a check.** Nested `if(lt(on,<frame>))` can only
  change value at a boundary frame, so "never adjust within a shot" holds by construction. An
  interpolated size would *be* a zoom, which is what the requirement forbids.

  **It can only ever magnify, which is why the target is the median.** `compute_crop_size` already
  returns the largest window of the target aspect that fits the source, so there is no room to widen
  and R2.6 forbids reaching outside it. A shot whose subject is *bigger* than the target is therefore
  left alone and recorded rather than approximated by cropping further in. Normalising to the maximum
  would magnify almost every shot to match the tightest one, softening most of the clip to fix a
  minority of it; with the median at most half the shots move, and the ones that do are the wide shots
  where magnification costs least.

  **Bounded to 1.35× (R2.3)**, past which softening is more visible than the mismatch it corrects, and
  which also caps what one mis-detected shot can do. Differences under 8% are left alone. The per-shot
  statistic is a **median**, because a detector that briefly latches onto a background face produces
  one wildly different box that a mean would carry into the whole shot.

  **Shots come from V4's existing cut list** (R2.5) via `reframe.cut_indices` — the same indices the
  tracker already uses for its EMA reset, so the two cannot disagree about where a shot begins.
  Detection *sizes* are read from the `Sample_Report` the reframe pass already holds, because the
  smoothed centre series has been reduced to `(cx, cy)` and lost them.

  **It declines rather than compounding (R2.10).** The mechanism is a magnification, exactly like zoom
  and ken-burns, and two on one shot multiply into a curve neither feature intended. The pipeline —
  not `apply_reframe`, which cannot see the option — records `subject_scale_skipped:zoom_active` and
  changes nothing. `transitions` counts as a zoom too, since it also produces a `zoompan`; gating on
  `zoom` alone would leave the compounding in place for half the cases.

  Off by default (R2.8), and this is the least certain default in the spec rather than merely a
  cautious one: a director may have *chosen* to alternate between close and wide, and normalising that
  removes an intentional edit. Nine mutations confirmed the tests are load-bearing — two initially
  survived, one because a fixture's median and maximum coincided so the choice of statistic was
  untested at all.

### Changed — thirteen settings that did nothing: four plumbed, eight retired, one already fixed

`scripts/check_wired.py` found thirteen `Settings` fields that no production code read. Setting one
did nothing at all, silently — which is worse than an unsupported option, because it reads as
supported. Both dead-code baselines are now **empty**.

**Plumbed (4).**

- **`BACKGROUND_STYLE` / `BACKGROUND_COLOR` (V11).** V11 built the whole
  `blur | mirror | black | color | gradient` vocabulary, and all four `reformat_aspect` call sites
  omitted both arguments — so the function's own parameter defaults won every time and
  `'black' is the honest choice for screen recordings` was advice no operator could act on. Now passed
  at the three `crop_blur` sites. **Deliberately not passed at the `pad` site:** that mode letterboxes
  with black by definition and ignores `background`, so handing it a style would recreate the exact
  defect being fixed. An unknown value falls back to `blur`, and the lookup is case- and
  space-insensitive, because a `.env` file acquires capitals and trailing spaces and neither should
  silently disable a feature.
- **`MUSIC_DEFAULT_VOLUME`.** The literal `0.12` lived in four places — the dataclass default, the
  API model, the API form default and the `from_dict` fallback — and the documented setting in none of
  them. All four now resolve through one function. Out-of-range values are clamped: `amix` takes a
  0..1 level, so `4.0` would be a distorted bed rather than a loud one.
- **`FACE_DETECTOR_BACKEND`.** Documented as "the detector used when a job does not specify one" and
  consulted by nothing: `resolve_detector` is only ever called with the per-job option, whose default
  was the literal `"haar"`. So `FACE_DETECTOR_BACKEND=mediapipe` had no effect on any render. An
  unknown backend name falls back to `haar` rather than reaching `resolve_detector` and disabling
  detection outright.

Every default is unchanged, because each setting's own default already equalled the literal it
replaced — an unconfigured install renders identically.

**Retired (8).** These described behaviour this project does not have.

- `REDIS_URL`, `RQ_QUEUE_NAME`, `USE_INPROCESS_FALLBACK` — there is no `import redis` or `import rq`
  anywhere in the tree, and `JobManager` is a single-worker `ThreadPoolExecutor`. "Fallback" implied a
  primary that has never existed; the README already said Redis + RQ is planned, not implemented.
- `API_HOST`, `API_PORT` — the bind is fixed independently by the container's `CMD`, its `EXPOSE` and
  its healthcheck URL. Two settings that cannot override the process's actual bind are worse than
  none. (Plumbing them instead would mean adding a `python -m api` runner and changing the
  `Dockerfile`, `docker-compose.yml` and smoke script to use it — a deployment change that cannot be
  verified while CI is blocked, so it is deliberately not bundled here.)
- `PUBLIC_BASE_URL` — no reader, and no `description=` either. The one publisher that might need a
  reachable URL uploads binary directly.
- `X_API_KEY`, `X_API_SECRET` — OAuth1 consumer credentials, while `publishers/x.py` authenticates
  solely with a Bearer token. Plumbing them means *implementing OAuth1 signing*, which is a feature,
  not a wiring fix.

`Settings` uses `extra="ignore"`, so a stale key left in someone's `.env` stays harmless.

**Two ratchet tests were measuring the wrong thing.** Clearing the baselines exposed both:
`pytest.mark.parametrize` over an empty sequence produces a **skip**, and this suite has no skips by
design — a skipped ratchet at the moment the debt reaches zero is indistinguishable from one that has
been switched off. And `test_the_checker_does_not_count_itself_as_a_user` asserted real debt existed,
using its presence as a proxy for "the scan is not reading itself"; that proxy was valid only while
the debt was. It now constructs the original confusion on a fixture tree and asserts **both**
directions, so it is independent of the tree's state forever. A test that breaks when the thing it
wants finally happens is measuring the wrong quantity.

### Fixed — A15 sound effects were never called, and the dead-code baseline is now empty

- **`A15` is wired into the audio mix.** `worker/effects/sfx.py` shipped complete and tested with no
  importer outside its own test module, so `sfx_volume` was read by nothing and `SFX_MODE` had no
  code path that would honour any value at all. This was the **last** entry in
  `scripts/check_wired.py`'s `KNOWN_UNWIRED` baseline, which is now empty: every shipped feature
  module in `worker/` and `publishers/` is reachable from production code.

- **Mixed after the music bed and before `loudnorm`.** Both halves are requirements rather than
  preferences. A sting mixed into the *speech* branch would be ducked by AU2 every time it landed
  under the bed — the exact opposite of an accent. Placing it before loudness correction keeps it
  inside the signal being corrected. The known limitation is stated rather than left to be
  discovered: `measure_loudness` reads the source file, so the stings are not in that measurement,
  and AU3's true-peak limiter is what keeps that safe.

- **The input-index contract is the part that breaks silently.** `build_mix` addresses its inputs by
  absolute index, so the sfx argv is collected during the audio pass and appended *after* the emoji
  block, preserving the `base -> engines -> music -> b-roll -> emoji -> sfx` order. Appending it any
  earlier renumbers every emoji input, and the resulting failure — a graph that will not initialise,
  or the wrong image composited — points nowhere near sfx. The test reads the index back out of the
  graph and checks that argv position really is a sting, rather than pinning a number that would
  merely restate the arithmetic.

- **No sting for an emoji that never reached the screen.** The trigger is keyed off the overlay
  *graph*, not the planned cue list: a cue whose asset failed to resolve is not visible, and a pop
  with nothing behind it is an unexplained noise the viewer cannot interpret. Same discipline AU22
  already applies when ducking the bed only under b-roll that actually composited.

- **`SFX_MODE=transitions` makes no sound on a stock install, and now says so.** `TRIGGER_SFX` maps
  the transition trigger to `whoosh`, which is one of the two stings deliberately *not* synthesised
  (a static band-passed noise swell is a hiss, and shipping a hiss under a name promising a sweep is
  the mislabelling A15 exists to avoid), and `assets/sfx/` ships empty. Every such hit now records
  `sfx_missing:whoosh` — emitted once per missing *name*, not once per hit, because twelve emoji with
  no sound available is one missing sound. `.env.example` states the consequence up front.

- **A skip that would have been the worst possible signal.** Clearing the last `KNOWN_UNWIRED` entry
  made `pytest.mark.parametrize` receive an empty sequence, which produces a **skipped** test. This
  suite has no skips by design, and a skipped ratchet at the exact moment the debt reaches zero is
  indistinguishable from a ratchet that has been switched off. The module ratchet is now a single
  assertion over the dict, so the empty case is a genuine pass.

### Fixed — V15 face-aware caption placement was never called

- **`V15` is wired into every caption path.** `worker/caption_placement.py` shipped complete and
  tested with no importer outside its own test module, so `CAPTION_AVOID_FACES=true` did nothing and
  the caption-over-the-mouth collision it exists to prevent still shipped on every clip.

  **Wired at `build_ass`, which covers both caption branches at once.** Placement runs after the
  C23/C24 cue passes — the cue shape has to be settled first — and before the style header, which is
  what consumes `position` to emit `Alignment` and `MarginV`. Reassigning one variable there
  therefore covers the preset path, the legacy `template` path and `rerender` together. The legacy
  path is included deliberately: C20's auto-contrast covers only the preset branch, and a legibility
  feature that silently depends on which caption *look* was chosen is the same defect in a different
  place.

  **The margin V15 reasons about is the margin that will be rendered.** With a C12 safe area or a C13
  offset configured, the caption is not at its default margin, so `resolve_margins` is consulted and
  the result passed through to `caption_band`. Reasoning at the default while the renderer draws
  elsewhere would miss real collisions and invent absent ones.

  **`None` and `""` positions survive an inert pass.** Both mean "inherit the preset's position", and
  writing the resolved name back would produce an identical file today while making every later
  preset change silently ineffective. Found by mutation: the unconditional write passed every other
  test, because a refusal returns the requested position unchanged and the two are then equal — they
  differ only when the position was inherited.

  **The kinetic engine needed a second seam, and the architecture dictated its shape.** That engine
  supersedes the compositor's captions entirely, so `build_ass` wiring alone would leave V15 silently
  absent from a kinetic render. The first attempt read the setting and detected faces inside the
  engine, and two existing gates rejected it: an engine may not create a subprocess, and
  `worker/engines/kinetic.py` pins its import surface to an allowlist that excludes `config`. So the
  impure half belongs to the Pipeline, which now detects once on the delivered frame and publishes
  the boxes on `clip_metadata` — the channel that already carries `hook_text` and `clip_size` — and
  the engine applies only `choose_position`, which is pure geometry. The setting is then honoured by
  construction: no detection, no boxes, nothing to apply.

  **Detected on the delivered frame, not the source.** Boxes from the reframe pass are in source
  pixels and would need mapping through a time-varying crop, and on the `crop_blur` and `pad`
  branches no detection ran at all — so re-detecting on the composited input is both simpler and the
  only option that covers every geometry branch.

  Off by default: this is a decode of the clip, and a render that never had a collision would be
  paying for one to find that out. Eight mutations confirmed the tests are load-bearing, including
  one that exposed a conditional assertion which never ran — a test that stayed green while the
  wiring was deleted, which is the same failure this whole change is about.
  `scripts/check_wired.py`'s baseline shrinks to a single module.

### Fixed — AU12 per-speaker level matching was never called

- **`AU12` is wired into the audio graph.** `worker/turn_gain.py` merged complete and
  property-tested, with an end-to-end test that renders the `volume` expression through real ffmpeg
  — and **no importer outside its own test module**. So diarisation was still never used for gain,
  which is precisely the defect AU12 was written to fix. `TURN_GAIN_ENABLED` did not exist at all;
  there was no way to ask for the feature even in principle.

  **The turn computation was trapped inside the `speaker_reframe` branch.** `slice_turns` and
  `rebase_turns` were called only there, so on the configuration AU12 is actually for —
  `DIARIZATION=true`, `speaker_reframe` off — the clip-relative turns were never derived. Hoisting
  them above the geometry ladder, where `keep_plan` is already final, gives both consumers one
  answer instead of two chances to rebase one and not the other.

  **Placed on the speech branch, before `loudnorm`** (R7.11). Per-speaker gain applied to a signal
  that already contains music would modulate the bed every time the speaker changed — audible as
  pumping, and nobody would attribute that to a level-matching feature. It chains onto whatever
  AU4/AU5 and AU11 produced rather than reading `[0:a]` again, so the cleanup, the presence shaping
  and the gain compose instead of one silently discarding the others.

  **R7.5 is the requirement that needed a test only the pipeline could provide.** Filler removal
  shortens the clip, so a turn at 2.6 s in the source is elsewhere in the delivery, and applying the
  ramp at the un-rebased time corrects the *wrong speaker* — worse than leaving the imbalance alone.
  The new test builds a fixture where the two timelines genuinely disagree and asserts the rebase
  moved something, so the guarantee cannot pass vacuously.

  **The envelope is measured only when it can be used.** It is a pass over the audio, and both cheap
  refusals — diarisation off, fewer than two turns — are decided from the arguments alone. Measuring
  first and discarding the result would be a silent cost on every clip of a single-speaker source,
  which is most of them.

  **Off by default (R7.8), and it never enables diarisation (R7.12).** `diarization_available` is
  read from the job option and deliberately not inferred from the turns being non-empty, because
  `speaker_reframe` already switches diarisation on for its own purposes — inferring would let AU12
  act on a job that never asked for it. With diarisation off it records
  `turn_gain_unavailable:diarization_disabled` and changes nothing.

  Five mutations confirmed the tests are load-bearing: `speaker_turns=` dropped at the call site,
  the hoist reverted, the filter append removed, `diarization_available` forced true, and the chain
  re-pointed at `[0:a]`. Each is caught. `scripts/check_wired.py`'s baseline shrinks by one module.

### Fixed — HDR sources were delivered grey and flat (O13, O14, O15)

- **`O13` — HDR is tone-mapped to SDR Rec.709.** There was no `tonemap`, `zscale` or `colorspace`
  anywhere in the repository, while `probe()` had been fetching `color_transfer` and discarding it
  since it was written. So a PQ or HLG source went through the whole pipeline with its transfer
  function ignored, and PQ-coded values interpreted as gamma render **too bright and desaturated**
  — the "grey and flat" complaint, and the reason this group jumps ahead of the measurement gate in
  the spec's own ordering. This is output that was wrong, not output that could be better.

  Measured on a synthetic PQ/BT.2020 10-bit source through the real pipeline: mean saturation
  **112 → 143**, mean luma **125 → 80**. A controlled demonstration on a drawn source, not a claim
  about real footage — the same caveat the face-detection work recorded for its BlazeFace figures.

  **Classification is tri-state and refuses to guess.** `HDR`, `SDR` and `UNKNOWN` are three
  answers and the third is not a synonym for the second. HDR is read **only** from the transfer
  function — never from bit depth, never from resolution — because 10-bit Rec.709 is ordinary and
  4K SDR is the norm, so either inference would misfire on a large class of normal footage.
  `bt2020-10` is classified SDR and has its own test: it is wide *gamut*, not high dynamic *range*,
  and it contains the string "2020" that a reader scanning for HDR would flag. The asymmetry is
  what drives all of this — tone-mapping a mislabelled SDR source visibly destroys it, while
  failing to tone-map an HDR one leaves things exactly as they were.

  **Converted once, at the cut.** The pipeline runs three passes. The cut is the only one with no
  geometry and no grade of its own, which makes it the only placement satisfying R2.2's "before any
  colour-dependent operation *and* before scaling" — the geometry pass scales, the composite pass
  grades. `Colour_Plan.consumed()` spends the filter chain so no later pass can re-apply it:
  tone-mapping twice compresses the range twice and delivers a flat, muddy picture that still looks
  like a photograph of something, which makes it far harder to diagnose than no tone-map at all.

  **Fails closed, deliberately opposite to `background_style_available`.** That helper answers
  "available" when its probe breaks, which is right there because the fallback is another working
  background. Here it is wrong: claiming `zscale` exists when we do not know emits a chain ffmpeg
  cannot configure, which is a **failed job**, and R2.5 forbids failing a job over tone-mapping.
  Routed through `worker.engines.capabilities` rather than probing locally — that module exists
  because an earlier hand-rolled probe misparsed `ffmpeg -filters` and hid 124 of 486 filters.

- **`O14` — delivered files declare their colour, and declare it honestly.** Nothing set
  `-colorspace`, `-color_primaries` or `-color_trc`, so players guessed. The tags now describe
  **what was delivered, not what arrived**: after a tone-map the file is Rec.709 and says so, and
  the source's `smpte2084` is dropped rather than copied across. Copying it would be *worse than
  writing no tags*, because a player reading it applies an HDR EOTF to SDR content and is
  confidently wrong instead of falling back to a correct assumption.

  The converse is also enforced: a Rec.601 source passed through untouched is tagged `smpte170m`,
  not `bt709`. Tagging everything Rec.709 because almost everything is Rec.709 is the tempting
  version and it makes the file assert something false. Absent source fields produce **no tag at
  all** rather than an invented one.

  Emitted through `h264_args`, so the `libx264`/`-crf` drift pin still holds. `colour_tags` defaults
  to empty for a specific reason rather than convenience: `tests/test_script_and_placement.py`
  asserts that function's exact argv, and re-freezing a pin as part of the change it exists to
  catch is how `font_substituted:Arial` was once baked into a golden as correct.

- **`O15` — full-range footage no longer crushes its blacks.** Phone footage is frequently
  full-range; passing `pc` through to a player expecting `tv` crushes blacks and clips highlights.
  It is now converted, and a source that declares no range records `colour_range_assumed:tv` — one
  of the few *guards* that does emit a marker, because "we assumed limited" is the first fact worth
  having when the blacks look wrong.

  **The conversion uses `scale`, not `zscale`**, and that split is deliberate: `scale` is in every
  ffmpeg build, so the more common defect is fixed unconditionally while only the rarer tone-map
  degrades. Asserted, so unifying the two later for tidiness cannot pass quietly.

- **Defaults ON, which breaks this project's own rule on purpose.** Every other new output setting
  defaults to previously shipped behaviour, so the parity goldens can detect an *accidental*
  change. That rule protects goldens, not defects, and the alternative here is knowingly delivering
  incorrect colour (R2.11). It is only defensible because the conversion cannot fire on a source
  that is not positively HDR — `test_an_sdr_source_produces_no_filters_at_all` pins exactly that,
  and **no golden or parity fixture needed re-freezing**, because an SDR library renders
  byte-identically.

- **Found while building this: an injected capability prober is silently ignored.**
  `get_report(prober)` honours its argument *only on first construction* — its own docstring says
  so — and returns the process-wide singleton otherwise. In any process where something has already
  probed a capability, a test that believes it has removed `zscale` gets the real answer and passes
  for the wrong reason. Two tests here did exactly that before it was caught. `worker/colour.py`
  builds a fresh `Capability_Report(prober)` when given one, and this is flagged in the close-out
  because the wrong pattern is the one that is easy to copy from existing call sites.

- **The mutation run's first result was 10 caught, 3 escaped, one wrongly declared equivalent.**
  Recorded because the final 13/13 is the less useful number. The three escapes were all real gaps:
  no test that switching tone-mapping *off* switched it off (the default is on, so nothing took that
  branch); an inverted range conversion, which yields a washed rather than crushed picture that
  reads as a grading choice and which the end-to-end test **cannot** catch because the tag would
  still say `tv`; and a fail-open capability probe — which turned out to be a defect in a *test*,
  since `Capability_Report._probe` swallows prober exceptions, so the `except` branch was
  unreachable and the test named for it had been passing through the ordinary path all along.
  Baseline **2160 → 2198 passed, 0 skipped, 0 warnings**; `mypy` clean over 102 files.

**Not done, and blocked rather than skipped:** `O16`/`O17`/`O20` each require the default to be
*measured* against a fidelity instrument before it moves (R4.1/R5.5/R7.5), and there is no `vmaf`,
`psnr` or `ssim` in this repository — changing them now would substitute one unmeasured default for
another. `O18` is gated on `render-quality-measurement`'s sync verification by R8.9, and `O19`
derives from it. `V20`/`V21` are buildable but belong *before* the tone-map in the design's fixed
filter order, and `V21` must hand its consumed margin to reframing or the crop drifts outside valid
pixels — neither should ride along with a colour change. See
`.kiro/specs/clip-signal-fidelity/CLOSE_OUT.md`.

### Changed — one formatter, and two rule sets that found real defects (I9)

- **`I9` — `UP` (pyupgrade) and `B` (flake8-bugbear) are enabled, and formatting is now enforced.**
  761 findings; 831 fixes applied by `ruff --fix` (more fixes than findings, because several
  rewrites cascade). `ruff format` then reformatted 181 of 235 files. The suite is **2160 passed,
  0 failed, 0 skipped, 0 warnings** and `mypy` is clean across 101 source files — which is the
  claim worth making, since `UP045`/`UP006` rewrote type annotations in nearly every module and a
  formatter is only safe if it is provably behaviour-preserving.

- **`black` was the item's literal instruction and is not what was adopted.** Measured before
  deciding rather than after: on this tree `ruff format` and `black` disagree on **35 of 195
  files**, under `black` 24.10 — the version `requirements-dev.txt` actually pinned — and not only
  under 25.x. The entire disagreement is redundant parentheses around multi-line conditional
  expressions, which `black` adds and `ruff` does not. Neither output is better, so the decision
  was never about style.

  It was about how many formatters this repository has. Two that disagree on 35 files, with one
  enforced in CI and one not, is the same shape as the defects this project keeps finding: one fact
  stated in two places, where changing one has no effect. Whoever ran the unenforced one would
  produce a diff CI then rejected. `black` is therefore **removed** from `requirements-dev.txt`
  rather than left listed and unused — which is exactly what it had been since the first commit.
  `ruff format` wins the tiebreakers: ruff is already a hard dependency, already blocking, and
  already carries `line-length = 100`, so the linter and formatter cannot drift on line length the
  way a separate `[tool.black]` section could.

- **Three rule sets are ignored with reasons, not silently dropped.** `B905` (`zip()` without
  `strict=`) fires on 33 sites, most of them the pairwise `zip(x, x[1:])` idiom where `strict=True`
  is actively wrong and `strict=False` is noise. `UP042` (`str, Enum` → `StrEnum`) touches 8 enums
  that serialise into stored job records, where `StrEnum` differs in `_generate_next_value_` and in
  how some serialisers treat it — a migration needing its own verification pass, not a lint
  autofix. `UP031` fires on `struct.pack("<%dh" % n)`, where `%d` is building a binary descriptor
  rather than formatting prose. `B008` is scoped to `api/main.py` alone, because FastAPI's
  `File(...)`/`Form(...)` defaults *are* the dependency-injection mechanism.

- **Two real defects, both found by the sweep rather than by review.**
  - `publishers/preflight.py` caught `except (FFmpegError, Exception)`. `FFmpegError` subclasses
    `RuntimeError`, so `Exception` already subsumed it and the tuple was one fact stated twice — it
    read as "handle ffmpeg errors specially, and guard broadly as well" while being identical to
    `except Exception`. `B014` does not catch this: it matches literal duplicates, not subclass
    relationships. Removing the redundant arm then made the import dead and `F401` said so, which
    is the lint chain working as intended.
  - A lambda in `tests/test_kinetic_compositor.py` closed over the loop variable `available`
    instead of binding it (`B023`), so both halves of a two-case parity loop patched
    `font_available` with whatever the *last* iteration set. Bound via a default argument.

- **`RUF100` is enabled, and it is the part of this change that will keep paying.** It reports a
  `# noqa` that no longer suppresses anything, and it is here *because of* the formatter rather
  than alongside it. `ruff format` wraps long calls, and a line-scoped suppression does not travel
  with the code it was written for: four `# noqa: S106` directives in `publishers/` ended up after
  a closing paren during this sweep, at which point they suppressed nothing and the S106 findings
  reappeared. Those announced themselves as errors. **The inverse case is the dangerous one** — a
  suppression left on a line with no violation is invisible, survives indefinitely, and silently
  hides a real finding the day the code beneath it changes.

  It found **76** such directives. 34 had a written rationale, which was preserved as a plain
  comment rather than deleted with the directive — `ruff --fix` removes the whole trailing comment,
  which would have discarded pointers like `# noqa: BLE001 - see below`. 41 had nothing worth
  keeping. 23 of the total were `E402` inside `tests/`, already covered by `per-file-ignores`, so
  the directive was pure redundancy. The rest named rules this project never enabled (`BLE001` 34,
  `N803` 7, `PLC0415` 3) — aspirational suppressions for checks that were not running.

- **Prose can create a blanket suppression, which nothing was checking for.** Two comments
  discussing `noqa` were being *parsed* as directives. One began `# noqa on the message, not the
  rule: S608 ...`, which ruff reads as a bare `# noqa` — a blanket suppression of every rule on
  that line. Harmless here only because it sat on a comment-only line, where `RUF100` could see it
  was unused; inline after code it would have silenced everything with no signal at all. The other
  was the long-standing `Invalid # noqa directive` warning on `worker/engines/base.py:58`, which
  had been emitted on every single lint run and read as cosmetic. Both reworded so the token no
  longer appears in a parseable position.

- **Markdown is excluded from the formatter, after it rewrote 679 lines of design documents.**
  Recent ruff versions format fenced Python blocks inside `.md`, so `ruff format .` silently
  reformatted nine `.kiro/specs/` documents with no Python source involved. Those are records of
  decisions already taken, and their snippets are frequently not valid complete modules — elided
  with `...`, cut to the two lines under discussion, or written in the compressed style of the
  module they describe, which is the same style `publishers/*` is exempted from `E701`/`E702` to
  preserve. Reflowing them buries the real change in a diff that says nothing and edits history to
  match today's house style. `[tool.ruff.format] exclude = ["*.md"]`, which also keeps the CI gate
  stable across ruff upgrades now that the set of file types the formatter claims has changed once.

- **No mutation spec accompanies this batch, deliberately.** The convention is one spec per batch,
  and it does not apply to a change with no behavioural surface: the only semantic edit is an
  exception tuple whose two arms were already equivalent. Mutating reformatted code would measure
  the suite against itself and report a number that means nothing. The evidence here is the 2160
  green tests and a clean `mypy` over annotations that were rewritten wholesale.

### Added — transcript-based trimming (U4)

- **`U4` — click words out of a clip.** The Descript-class feature: a clip's transcript is shown
  word by word, striking words out removes them from the media, and the clip is re-rendered in
  place with its metadata, filename and publish history intact.

  **No new ffmpeg path.** A cut list and filler removal are the same operation with different
  reasons for wanting a region gone, so both resolve to **one** keep list and **one** re-encode
  through the existing `filler.apply_keep_intervals` (`trim`/`atrim` + `concat`, with V10's seam
  fades). Applying them in sequence was the obvious alternative and is wrong twice over: it
  concatenates the clip twice, and the second pass's offsets would be expressed against the first
  pass's output timeline rather than the one the caller's cuts refer to. Where they overlap they
  compose by **union** — both features get what they asked for, rather than the later one winning.

  **Cuts are the wire format, not keeps.** A cut list is what a transcript editor produces, it needs
  no knowledge of the duration the renderer will settle on (which AU7 edge-silence trimming can
  still change), and an empty cut list unambiguously means "change nothing" — where an empty *keep*
  list would mean "remove everything", so a dropped field would destroy the clip instead of no-oping.
  Two independent keep lists also have no correct way to combine; intersecting them is right only
  because they are complements of removals, which is the cut representation in disguise.

  **The cut list is a typed field of its own, not a key inside `settings`.** `settings` is filtered
  against `ProcessingOptions` and unknown keys are dropped in silence, which for a destructive edit
  the user is watching for is the worst available failure. It is also per-clip, where everything in
  `settings` is per-job.

  **Refusals, not approximations**, following the rest of the pipeline. Striking every word records
  `transcript_trim_refused:empty_result` and renders the clip untouched, because an empty keep list
  is a `concat` with no inputs. A list longer than 200 cuts records
  `transcript_trim_refused:too_many_cuts` — the filter graph grows linearly with the cut count, so
  past some point the graph is the problem rather than the edit — and the API answers `422` naming
  the limit rather than leaving the caller to find a marker. Keep segments below 200 ms are dropped:
  rendered, a 50 ms sliver between two cuts is a frame of video and an audible click, not a word.

  **Word timings come from the T8 cache, and ASR never runs to serve the editor.**
  `GET /api/jobs/{job}/clips/{clip}/transcript` reads the cache entry the render itself consumed —
  so the words a user clicks are the words that were burned in — and answers `409` on a miss rather
  than blocking a UI interaction on a multi-minute transcription. The key is derived by
  `transcribe.cache_key_for`, extracted so that the reader and the writer cannot drift apart; the
  same fact stated in two places is a defect the mutation harness has now caught three times.

  One honest gap is reported rather than papered over: a clip whose media was already tightened at
  render time has word times that run ahead of the file being played, and the removed regions are
  not recorded on the clip, so there is nothing to correct with. The endpoint returns `trimmed:
  true` and the editor says so.

- **Fixed: `ClipResult.duration` reported the source window, not the rendered clip.** Pre-existing
  and visible whenever filler removal had tightened a clip — the recorded duration was
  `end - start`, so a successful tightening was reported as having changed nothing. It is now the
  rendered length. `start`/`end` still describe where in the source the clip came from, which is
  what a resume matches windows on. Default runs are unaffected (`filler_removal` is off by
  default and an absent cut list changes nothing), so the parity goldens are untouched.
### Fixed — three defects that only appeared outside a prepared machine

- **`C1`/`C2`/`M7` — a vendored caption face is available because we ship it.** `font_available`
  enumerated families with `fc-list` only, so it could not see `assets/fonts` — the directory
  `subtitles_filter` hands to libass as `fontsdir`. Anywhere the faces were not *also* installed
  system-wide, every vendored face probed as missing and `resolve_font` substituted it away:
  measured with fontconfig present and the faces unregistered, **all fourteen built-in presets
  rendered in Noto Sans**. C1's failure mode one layer up — substituting *away* from a font that
  would have rendered.

  The repository already knew the shape of this. `discovered_fonts` records that A5 faces render
  with no `fc-cache` run because libass reads the directory, and `scripts/setup_dev_env.sh` states
  that the probe "reads the *system* font list, so without this `font_available()` disagrees with
  what will actually render" — then installs the faces system-wide to paper over it, as the
  Dockerfile does. `.github/workflows/ci.yml` never did, so the preset assertions in
  `test_fonts_real_binary.py` were failing there while every local run was green. Consulting the
  shipped manifest removes the need for the workaround rather than adding a third copy of it.

  Variable faces stay excluded, matching `available_fonts`: libass' directory provider cannot
  select a named instance, so counting them would substitute *towards* a face that cannot render.
  That exclusion is now asserted, so widening it later cannot pass quietly.
- **A green suite no longer hides an unusable vision stack** (`tests/test_vision_runtime.py`).
  `detect_faces` imports cv2 lazily and returns `[]` on any failure, which becomes
  `ReframeUnavailable` and then a static `crop_blur`. Correct at runtime and completely silent:
  with `libGL.so.1` absent, `import cv2` raises and the suite still reported **1037 passed with
  nothing skipped or failed**, so every test that looks like face-tracking coverage was running the
  degraded branch. `setup_dev_env.sh` prints the opencv version, but a printed version nobody
  asserts on is not a gate. These skip when the libraries are unusable, which the no-skips rule
  turns into a CI failure with a named reason.
- **A test that could not run anywhere but one machine.**
  `test_a17_selection_does_not_use_python_s_salted_hash` spawned `.venv/bin/python` with
  `check=True`, so it raised `FileNotFoundError` wherever that path did not exist — including CI,
  which installs via `actions/setup-python` and creates no `.venv`. Now uses `sys.executable`,
  which is also the interpreter the test actually wants.

**Baseline: 1880 → 1900 passed, 0 failed, 0 skipped**, measured with the vendored faces *absent*
from fontconfig and no `.venv` present — the environment CI actually has, rather than a prepared
one.


### Added — modern face detection and measured detection confidence

- **A second face-detector backend, opt-in.** `face_detector_backend` (`FACE_DETECTOR_BACKEND`)
  accepts `haar` (the shipped
  OpenCV cascade) or `mediapipe` (BlazeFace, via the vendored model). **`haar` stays the
  default**, so an unchanged configuration reproduces v0.11.0 output exactly — which matters
  more than it sounds: the golden and parity renders only detect an *accidental* change while
  they are not re-frozen each release, and switching the default detector would move the crop
  path, and therefore the pixels, in every one of them.

  Measured on a synthetic source containing a frontal drift, a profile turn and a two-shot:
  Haar reaches **0.886** detection coverage overall and **0.60** across the profile turn;
  BlazeFace reaches **0.971** and **0.90**. The profile turn is the case the upgrade exists
  for — a frontal cascade loses the face as the head rotates, which on screen is the crop
  freezing while the subject keeps moving. Both figures come from drawn faces rather than
  photographs, so they are a controlled comparison and a demonstration that the measurement
  works, not a claim about real-world accuracy.

- **Detection coverage is now measured and reported**, which is the first quantitative
  statement this subsystem has made about itself. `reframe_low_confidence:<coverage>` lands on
  a clip whose framing was built from sparse detections, so a batch review can concentrate on
  those and trust the rest. Below-floor *and* at least one detection: zero coverage is already
  reported by the existing no-faces degradation, and emitting both would be two names for one
  condition — the duplicated-fact pattern mutation testing has caught twice here.
  `reframe_sample_rate:<fps>` appears only when the sampling cap actually reduced the rate,
  because a marker emitted on every clip is noise, and noise is what stops a marker being read.

- **The marker names the backend that ran, never the one that was asked for.**
  `face_detector:haar|mediapipe|injected`, and `face_detector_substituted:mediapipe:haar` when
  MediaPipe was requested and Haar ran. The resolver returns the label from the branch that
  succeeded rather than letting the caller infer it, because inference is exactly how
  `font_substituted:Arial` got frozen into a golden file as correct. An injected detector
  reports `injected` rather than borrowing a backend name: a test double is not evidence that a
  backend works.

- **The BlazeFace model is vendored, licensed and verified offline**, following the
  `assets/emoji/` precedent exactly — `assets/models/blaze_face_short_range.tflite` (229,746
  bytes, Apache-2.0) with its licence and provenance alongside, and
  `scripts/fetch_models.py --check` verifying it by SHA-256 against a manifest with no network
  access, wired into CI. This is what moves the item out of the weights-blocked bucket: a
  quarter-megabyte graph under a permissive licence can live in a git repository, where an
  active-speaker or body-detection checkpoint cannot. Neither MediaPipe nor OpenCV ships a
  model in its wheel — verified, not assumed — so without vendoring the backend has nothing to
  load. A digest rather than an existence check, because a half-downloaded `.tflite` is a file
  that exists, has a plausible size, and fails deep inside the native graph at the first frame
  of a render.

- **A coordinate-conversion function with its own tests, verified against the real library.**
  This is the part of the feature that would have failed silently. Measured on mediapipe
  0.10.35, the tasks API returns **absolute pixels** and has no `relative_bounding_box` at all
  — the normalised box belonged to the `mediapipe.solutions` namespace, which was **removed**.
  Had a normalised box leaked through as pixels, the result is a one-pixel face at the frame
  origin and *nothing objects*: `pick_main_face` returns a centre, `FaceBox` validates,
  `build_face_tracks` builds tracks, `build_sendcmd` clamps to a valid window, ffmpeg encodes,
  and the clip record says `reframe`. Every clip would be cropped to the frame's left edge and
  the only evidence would be the pixels. A suite of fake detectors returning pixel tuples
  cannot catch that, because the fakes would be right and the real backend wrong — so
  `tests/test_face_detection_real_binary.py` runs the real library against a real image with no
  mocking, and cross-checks the conversion through arithmetic written out in the test that
  shares no code with the function under test.

- **Confidence-aware main-face selection.** Where a backend supplies scores, the main face is
  ranked on `score × sqrt(area)` so a large low-confidence box loses to a smaller confident
  face — the crop should follow a face rather than a bookshelf, and a bookshelf is usually
  bigger. Linear extent rather than area because weighting by area makes confidence nearly
  irrelevant: a 400×400 false positive at 0.10 beats an 80×80 face at 0.95 by more than two to
  one on `score × area`. Where no scores are available — Haar supplies none, and inventing one
  from reject levels would be false precision — selection is largest-area *verbatim*, which is
  what makes the byte-identical default achievable rather than merely likely.

- **Fixed: the speaker-aware path reported coverage 0.0 for every clip.** `FaceBox` carries a
  leading `t`, so it is not a 4-tuple, and the detection coercion silently rejected it — which
  meant footage the same run had just tracked successfully was reported as having no faces.
  Found by a test written for the marker, not by review.

### Added — scripts, placement, stings and hardware encoding (C21, V15, AU9, O8)

- **`C21` — a caption font that can actually render what was said.** Captions were drawn in a Latin
  display face regardless of the language. On Arabic, Hebrew, Chinese, Japanese, Korean or Thai that
  produces a line of `.notdef` boxes — tofu — and **nothing in the pipeline can see it**: libass
  reports no error, the ASS file is valid, the encode succeeds, and the clip record says `captions`.
  The only symptom is in the pixels.

  Coverage is decided by reading the font's own `cmap`, never by asking fontconfig. `fc-match` is a
  *best match*, not a coverage test — it always answers, and `fc-match ':lang=ar'` on the machine
  this was written on returns `NotoSans[wdth,wght].ttf`, which contains **no Arabic at all**.
  Candidate families come from `fc-list :lang=xx`, which returns nothing when there is nothing, and
  are then re-verified against each file's `cmap` anyway.

  **The vendored gap is now stated rather than assumed.** The plan's note says "Noto covers CJK",
  which is true of the Noto *project* and not of the vendored `NotoSans[wdth,wght].ttf` — CJK lives
  in Noto Sans CJK, a separate family of ~100 MB per weight. Measured: Latin everywhere, Cyrillic
  in four faces, Greek in two, Devanagari in four (including all three Poppins), and **nothing
  vendored for Arabic, Hebrew, Thai, Han, Hiragana or Hangul**. `unsupported_scripts()` makes that
  answerable without rendering a clip and looking at it.

  Resolution keeps the requested font when it covers the script — a creator's chosen face is a brand
  decision, and overriding it because a clip contains one Greek letter would be worse than the
  problem. Otherwise a vendored face (so an offline install still works, and the manifest's
  display-first order keeps the substitution close to the intended look), then a verified system
  family. When nothing can render it, the requested font is kept and the clip records
  `caption_script_unsupported:<script>`: substituting a different Latin face would not help either,
  so the honest outcome is to render the same thing *and say so*.

  Finally, **measured wrapping is switched off for shaping scripts**. C6 sums per-glyph advance
  widths, which is a fair approximation for Latin and simply wrong for Arabic, where letters join and
  a word's rendered width is not the sum of its isolated forms — and for Devanagari and Thai, where
  marks reorder. Those get `WrapStyle: 0` so libass wraps them: less control, but control based on a
  wrong measurement is worse than none. Hebrew is deliberately *not* in that set — it is RTL but
  unjoined, so widths add up and the reordering is libass' job via FriBidi. No bidi reordering
  happens here, or the text would be reversed twice.
- **`V15` — captions off the speaker's mouth.** Caption position is a fixed choice and the face is
  wherever the footage put it; on a lot of vertical footage those collide and three lines of heavy
  display type land across the speaker's mouth. The crop is right and the captions are right — only
  their combination is wrong, which is why nothing upstream catches it.

  Three rules keep this from being a look change. It **only acts on an actual overlap**, so a
  library of clips that had no problem comes back identical. It **only moves between the nine C13
  positions**, keeping the horizontal alignment the preset chose — answering "bottom-left covers the
  mouth" with "centre-top" changes two things to fix one — and never invents a pixel offset nobody
  picked. And when **no position clears the face** (a close-up filling the frame, or a three-speaker
  panel that occupies every band) it changes nothing and records
  `caption_face_overlap_unavoidable`, because moving the text from the mouth to the eyes is not an
  improvement — and that marker is what distinguishes the case from "no faces detected".

  The mouth is taken as the lower third of a detected face box, generously: the cost of being wrong
  that way is moving a caption that would have been fine. It is not mouth *detection*, and it is
  not called that. Bands are unioned across every sampled frame, because a caption that is clear for
  two seconds and covers the mouth for one is still wrong — and that is the case hardest to notice
  when reviewing a still. Off by default: it costs a face-detection pass a collision-free render
  would be paying for nothing.
- **`AU9` — sound-effect stings on cuts and emoji.** A cut or an emoji arriving is a visual accent
  with no audible counterpart, so the edit reads as a picture change rather than as a beat.

  **What is synthesised and what is not is the substance here,** because A15 already recorded what
  goes wrong when that is blurred. `pop` and `click` **are** generated, honestly: a pop *is* a short
  band-passed noise burst with a fast attack, so generating one is not an approximation of the real
  thing — hence no degradation marker, because there is no degradation. `whoosh` and `swipe` are
  **not** generated at all: a whoosh is noise under a filter that *moves* across the sound, ffmpeg
  cannot express a time-varying filter frequency in one pass (`bandpass` takes no expression and has
  no `eval=frame`), and a static band-passed noise swell is a hiss. Shipping a hiss under a name
  that promises a sweep is exactly the mislabelling A15 exists to stop, so those require a file in
  `SFX_DIR` and record `sfx_missing:<name>` without one.

  Mixed with `amix normalize=0`, which matters more than it looks: with normalisation on, `amix`
  divides every input by the input count, so adding one sting would make the **speech** 1/n quieter
  for the whole clip — a global change caused by a local accent, and one nobody would attribute to
  the feature that caused it. Verified by measurement, not by reading the flag. `duration=first`
  keeps the clip's own length, and `adelay=...:all=1` delays every channel — without `all=1` a
  stereo sting arrives on the left and then the right, audible as a flam.

  Two stings within 350 ms become one, and a **transition wins a contested slot at its own moment**:
  a cut is structural and an emoji is decoration. Applied within the gap window rather than only at
  exactly equal times, because a single pass keeping the earlier candidate would drop a transition at
  1.05 s in favour of an emoji at 1.00 s.
- **`O8` — optional hardware H.264 encoding.** Routed through the single `h264_args` builder, so an
  encoder swap cannot reach seven of the eight encode sites and leave clips whose quality depends on
  which stage wrote them.

  **Availability is decided by actually encoding a frame,** not by reading `ffmpeg -encoders`. The
  distinction is not hypothetical: the ffmpeg this project develops against *lists* `h264_v4l2m2m`
  and fails the moment it is asked for a frame, and `h264_nvenc` does the same on a host with the
  libraries and no card. Reading the list turns a missing GPU into a failed job at the point where
  the transcription has already been paid for. The probe is cached, so it costs one frame per
  process.

  **The quality flag is not `-crf` anywhere else,** and three encoders use a different *scale*:
  NVENC needs `-rc vbr -cq` (without `-rc vbr`, `-cq` is accepted and ignored and the encoder uses
  its default bitrate), QSV uses `-global_quality`, VAAPI uses `-qp`, and VideoToolbox uses `-q:v` on
  a **1–100 scale where higher is better** — inverted. Passing `-crf 20` to VideoToolbox is not an
  error; it is ignored. Passing `20` to `-q:v` asks for near-worst quality. Either way the output is
  wrong and nothing says so, so the mapping is a table with the scale written down. Presets are
  translated too (`veryfast` → NVENC `p2`) or omitted, since VideoToolbox has no preset concept and
  an unrecognised one is a hard error on some builds.

  `h264_v4l2m2m` is **deliberately refused with a reason** rather than quietly absent: it has no
  constant-quality mode, only `-b:v`, so using it would switch the whole pipeline from a quality
  target to a bitrate target without saying so.

  The default stays `libx264`, **not** `auto`. These encoders are not comparable with x264 at the
  same nominal quality, so `auto` would change the output of every existing install the first time
  it landed on a machine with a GPU, with no setting changed — and would make the M1 golden renders
  machine-dependent. A *named* request that falls back records `encoder_unavailable:`, because
  silently ignoring it is how someone spends a week believing their GPU is in use; `auto` falling
  back records nothing, since it asked for "the best available" and software is available.

### Fixed — deployment and ingest, both previously unexercised (I7, I10, I12, I13)

- **`I12` — the Docker image had never been built.** A Dockerfile nobody has run is a deployment
  story rather than a deployment. It builds, and `scripts/docker_smoke.sh` now boots the image and
  probes it in CI, gating the deploy job. Three things can be wrong here that nothing else catches,
  because each is invisible from a working checkout: the build failing; the build succeeding and
  the app not starting; and both succeeding while an **asset is missing** because `.dockerignore`
  excluded it. The last is why the check queries the fonts *through the API* rather than running
  `fc-match` — `fc-match Anton` succeeding only proves the Dockerfile registered the faces with
  fontconfig, not that the app's own manifest and files both arrived under `/app`. The font chain
  has already broken exactly that way once (C1), and the only symptom was captions in a substituted
  face in rendered output. Verified: `/healthz`, the SPA from the frontend build stage, 8 fonts,
  14 caption presets, 326 vendored emoji.
- **`I13` — URL ingest had never been run either.** It downloads. `download_video` is the *first*
  thing a "paste a link" user touches and every failure inside it raises `DownloadError` carrying a
  yt-dlp message, so four things could have been silently wrong: the format selector (a malformed
  one falls through to `best` rather than erroring, so an ignored height cap looks exactly like one
  that worked); `outtmpl`; the returned path; and the progress hook, where a wrong key name means
  the UI shows nothing for the whole download — indistinguishable from a hung job.

  Verified against the real network (Wikimedia Commons: metadata, a 5.6 MB download, 14 progress
  events, the height cap honoured at both 240p and 480p) and then pinned by tests that serve the
  media from a **local HTTP server**. yt-dlp's `generic` extractor treats a plain media URL like
  any other input, so the local server exercises the whole path with none of the flakiness that
  would make this test the reason CI is red.

  One branch could not be reached that way and is now reachable: `prepare_filename` reports the
  pre-post-processing name, and `merge_output_format` rewrites the container **only when a merge
  actually happened**, so the prepared name is right for a progressive source and wrong for a
  merged one — with both returning a plausible path and only one existing. Extracted as
  `resolve_downloaded_path` so both halves can be tested rather than only the half a single-file
  source happens to take.
- **`I7` — image size: 1.83 GB → 1.48 GB.** Two findings, one of them a plain dependency bug:
  - **Two OpenCVs were installed.** `requirements.txt` pinned `opencv-python` while mediapipe
    depends on `opencv-contrib-python`, so pip installed both — and each ships its own ~91 MB
    directory of near-identical native libraries. contrib is a superset in the same `cv2`
    namespace, so dropping the explicit pin changes nothing that imports. Left implicit rather
    than re-pinned to contrib: two places to state it is how the tree ends up with two wheels
    again.
  - **Node is now opt-in** (`--build-arg INSTALL_WHOP_BRIDGE=true`). Debian's `nodejs`+`npm` is
    around 200 MB of image for one optional publisher. That exposed a real gap: the Whop
    publisher's availability check looked for the *bridge script*, which is committed source and
    therefore present in every image, so it reported the publisher **ready** on an image with no
    Node and then failed at publish time with a `FileNotFoundError` from `subprocess` — the least
    actionable place to learn that Node is missing. It now checks for the interpreter too, and the
    message names the build arg.
- **`I10` — the npm advisories.** 11 → 0 reported by `npm audit`, via `vite` 5→8, `vitest` 2→3 and
  `@vitejs/plugin-react` 4→5. The chain that mattered was `esbuild`: it let any website send
  requests to a running dev server and read the responses.

  The upgrade broke all 81 component tests while `npm run build` stayed perfectly green — vitest
  transformed `.jsx` files with the *classic* JSX runtime, so every one failed with
  "ReferenceError: React is not defined". Fixed by stating `esbuild: { jsx: "automatic" }` in the
  vite config, which makes both pipelines agree rather than depending on which transform sees a
  file first. Node is pinned to 22 in CI and in the Dockerfile's frontend stage, because vite 8
  requires `^20.19.0 || >=22.12.0` and `node:20-slim` satisfies that only on its newest patch
  releases.

  **A residual 9 advisories are deliberately not "fixed":** one `brace-expansion` DoS reachable
  through eslint's own `minimatch` chain. Both available fixes are worse than the finding, and both
  were tried — `npm audit fix --force` *downgrades* `eslint-plugin-react` to 7.22.0, and overriding
  `brace-expansion` to a patched 5.x breaks `minimatch@3` so eslint crashes outright. CI therefore
  blocks on `--audit-level=critical`, because a gate that cannot be satisfied without breaking the
  build is a gate people learn to ignore.

### Added — the asset libraries (A5, A9, A13, A17, A19, A22)

- **`A9` — the emoji keyword map, 85 keywords to 1190.** On real speech the old map produced no
  emoji at all on most clips: `standard` intensity allows one every five seconds, so a 60-second
  clip has to contain a dozen mapped words *spread across it* to fill even half its slots. The
  overlay was decorative on the few clips that happened to say "money" or "fire". All 326 glyphs
  are vendored (7.3 MB), so rendering still never touches the network.

  **What it deliberately does not contain** is the more interesting half. Homographs are excluded:
  a `bank` is 🏦 or a river's edge, `spring` is a season, a coil or a verb, `wave` is water or a
  hand, `beat` is a victory or a rhythm, `second` is a place or a duration. Each would raise the
  keyword count and lower the hit *quality*, and an emoji illustrating the wrong sense of a word
  reads as a machine that did not understand the sentence — the same trade C14 refused when it
  declined to pad the caption presets with hue-only variants.

  Function words are excluded too, **even where the picture is apt**. "Like" is 👍 in one sense and
  a filler in most others; "this", "here", "there" and "off" are grammatical far more often than
  deictic. A11 scores stopwords at zero, so such a word only ever wins a slot when nothing better
  is in the clip — which is exactly the wrong moment for a weak match, because that clip has no
  other emoji to distract from it. Enforced by a test against the caption stopword list rather
  than by care, with the four that shipped that way (`yes`, `no`, `up`, `down`) named explicitly:
  the failure mode of adding one is a plausible-looking overlay on the word "just".
- **`A13` — Noto, Twemoji or OpenMoji.** Which artwork set appears is a *look* decision, and the
  three read very differently over footage. Three things differ per set and each produces a 404
  rather than an error anyone can read: the base URL, the case of the hex in the filename
  (OpenMoji upper-cases it), and the prefix plus separator (Noto writes
  `emoji_u1f9d1_200d_1f3eb.png` where the others write `1f9d1-200d-1f3eb.png`). Encoding all three
  per style is what stops "switch to OpenMoji" from meaning "silently render no emoji".

  **Only Noto is vendored**, and that is a decision rather than an omission: three sets over 326
  glyphs would triple the committed asset weight to ship two looks most installs never select. The
  others are fetched on demand or vendored once with `scripts/fetch_emoji.py --style`. A glyph
  missing from the selected style falls back to the Noto file rather than dropping the overlay —
  a mixed-style overlay is a cosmetic inconsistency that is visible and correctable, a missing one
  looks like the feature is broken — and the clip records `emoji_style_degraded:<name>` when that
  happens, because a cosmetic difference nobody is told about is a bug report later. Twemoji's
  72px is stated in the docs as *smaller than every size this tool composites at*, so choosing it
  is knowingly choosing a soft overlay.
- **`A5` — operator-supplied caption fonts.** Drop a TTF into `FONT_ASSETS_DIR` and it appears in
  the picker. A **server-side directory, not an upload endpoint**, for the reason U6 recorded for
  the brand logo: an upload needs a storage location, a cleanup policy and a retention rule, none
  of which exist for assets, and inventing three of them to accept a font file is a larger
  decision than the feature.

  The family name is read from the font's own `name` table, never from its filename. libass selects
  a face by that name and answers an unknown family by *silently substituting another* — so a
  picker offering `MyBrandFont.ttf` as "MyBrandFont" would be offering a name that resolves to
  nothing. That is exactly the C1 defect this codebase has already shipped once, which is why a
  font whose real name cannot be read is not offered at all, and why a bold-only file is offered as
  "Family Bold" rather than "Family". Variable fonts are excluded for the same reason the manifest
  excludes them.

  Two smaller things fell out of it. The reported `weight` is converted from the file's OS/2 class
  to **fontconfig's scale**, which is what `assets/fonts.json` records and what libass prints —
  mixing the two would make a user-supplied regular face (400) look nearly twice as heavy as a
  vendored black one (210). And a *missing or corrupt* manifest no longer empties the picker: the
  twelve files are still on disk and readable, so the declaration failing no longer takes the
  assets down with it.
- **`A17` — several music tracks per mood.** One track per mood meant every clip in a batch of ten
  carried the same bed, which is the most obvious way a set of clips reads as machine-produced.
  Three layouts are accepted, because the natural way to hold twenty tracks is not the natural way
  to hold one: `upbeat.mp3`, `upbeat/*.mp3`, or `upbeat_2.mp3` siblings. A separator after the mood
  name is required, so `upbeat_2` matches and `upbeatish` does not.

  Selection is **deterministic, not random**. A batch wants variety *between* clips and a re-run
  wants the same output: the M1 golden renders depend on the second, and a creator re-rendering one
  clip of ten to fix a typo does not want a different bed underneath it. The key is hashed from the
  source filename, the clip's ordinal and its start time — not the clip id, which carries a
  `uuid4`. The hash is blake2b and **not** `hash()`: Python salts string hashing per process unless
  `PYTHONHASHSEED` is set, so a `hash(key) % len(tracks)` selection would be stable within one run
  and different on the next — reproducible in exactly the tests that would catch it, and not in
  production. A `music_track:2/5` marker makes the variety visible, since the path is not in the
  clip record.
- **`A19` — b-roll matched by tag, not by filename substring.** The old rule had three failures and
  all three were silent. `stem in token` with a two-character stem is nearly always true, so a file
  called `on.mp4` answered the keyword "money" and `ca.mp4` answered "car". The *first* match won
  rather than the best, so renaming an unrelated file changed the b-roll. And a filename is not a
  description — a stock clip is called `pexels-4276282.mp4`.

  Now an optional `tags.json` describes the library, matching *scores* rather than short-circuits,
  the best-scoring file wins with ties broken by name so two machines agree, and both sides are
  length-filtered. A missing or malformed manifest degrades to filename matching, which is what the
  library did before — not to a library that silently has no tags.

  **The synonym source is the emoji keyword map**, inverted: ~1190 words already cluster into ~326
  groups, so there is one word list to maintain rather than two that drift. What that table asserts
  is narrower than synonymy, and the difference is stated rather than glossed: two words share a
  group only when they share a *picture*, so money/wealth/fortune are one group and "cash" is in
  another because 💰 and 💵 are different images. Expansion is therefore conservative — it adds
  true synonyms and misses some near ones. That is the right error direction: a missed synonym
  costs one weaker match, a wrong one puts unrelated footage on screen. Synonyms also score below
  an explicit tag, so inference can never override something the operator actually said.
- **`A22` — motion on b-roll stills, and a dip in the bed under b-roll.** A still that sits
  motionless for three seconds over moving footage is the clearest sign a clip was assembled rather
  than edited. `BROLL_KEN_BURNS` adds a slow zoom whose convergence point supplies the drift, with
  the anchor rotating per cue so four stills in one clip do not all move identically.

  The zoom is an explicit function of the output frame number, not the usual accumulating
  `z='zoom+step'` recipe — accumulation makes the final framing depend on how many frames were
  produced, so the same still would zoom twice as far on a 60fps render as on a 30fps one. Off by
  default because it *is* a visible change: `zoompan` requires an explicit output size and the
  default graph scales stills to an unknown height, so with it on stills are cover-cropped into a
  fixed 16:9 box. Video assets already move and are never touched. Verified against a
  half-transparent PNG that alpha survives the filter, and the box is forced even-sided because
  4:2:0 subsampling requires it and an odd height fails the *encode*, several stages after the
  filter string that caused it.

  `BROLL_DUCK` dips the music under each b-roll window, so a visual accent has an audible one.
  Applied to **the bed only, never the mix**: the b-roll is illustrating what is being said, so
  ducking the speech would invert the point. Built as one `volume` expression taking the *deepest*
  applicable dip rather than a chain of filters, because a chain multiplies and two adjacent cues
  would drive the bed to silence. Each dip is ramped, since a hard step in level is audible as a
  click on a sustained bed — a worse artefact than the one being fixed. Only windows that actually
  composited duck, so a cue whose asset failed to resolve does not leave a hole in the bed with no
  picture to explain it.

### Added — what the words say, and who they say it to (C19, S7, S8, S12, T9, T10)

- **`S7` — question/answer and list structure.** Selection measured how a moment was *delivered* —
  pace, energy, how promptly it opens — and nothing about what it says. A question with its answer,
  or an enumeration ("three things you need to know", "here's why"), is a self-contained unit by
  construction: it opens with an implicit promise and closes by keeping it.

  The ordering is the substance. An **unanswered** question scores *below* a passage with no
  structure at all (0.25 against 0.5), because it opens a loop the clip never closes — the single
  most common way an auto-cut moment feels unfinished. Any implementation that treats structure as
  a bonus to be added would score it neutrally and still look like it worked.

  Questions are detected from opener words as well as `?`, because Whisper omits question marks on
  rising-intonation questions routinely, and relying on punctuation makes the signal a property of
  Whisper's formatting rather than of the speech. Trailing conversational tags are stripped first:
  `"we rebuilt the whole thing over one weekend, you know?"` is a statement, and reading it as an
  unanswered question would apply the lowest score there is to the way people talk.
- **`S8` — lexical emotional intensity.** How strongly a passage is *worded*, which is not what
  energy (S2) measures. A shouted list of ingredients and a quietly devastating sentence sit at
  opposite corners of the two, so both signals exist.

  A **density**, not a count: a raw count ranks a three-minute passage containing two strong words
  above a ten-second one containing the same two — higher purely for being longer, the same defect
  S11 exists to remove. Strong terms are weighted twice the merely-emphatic ones; a grouping that
  did not affect the score would be two word lists pretending to be one.
- **`S12` — standalone completeness.** Whether a window makes sense without what came before. A clip
  opening on "and *that's* why he did it" is a fragment of a conversation, and no amount of pace or
  energy makes it publishable — this is invisible to every other signal.

  The penalties are deliberately **unequal**: a stated back-reference ("as I said") 0.5, a dangling
  conjunction 0.4, a demonstrative opener 0.15, an unfinished ending 0.1. A demonstrative is weak
  because "this is the part where…" is a *good* clip opening — the word is as often forward-looking
  as back-looking. An unfinished tail is lightest because it is the one failure the boundary logic
  (S9 snapping, AU7 trimming) can still fix, whereas nothing downstream can supply missing context.
  A back-reference in the *middle* of a clip is not penalised at all: that is a speaker recapping,
  which usually helps the clip stand alone.

  All three are **lexical rules, not model calls**, for the reason S4 records: a per-segment request
  costs money per job, and nothing can yet tell whether a scored signal improves selection because
  the S1 labelled dataset does not exist. A paid, unvalidated signal makes an improvement and a
  regression indistinguishable *while charging for it*.

  Weighted **below** the acoustic signals (`SELECTION_WEIGHT_STANDALONE=0.20`, `_STRUCTURE=0.15`,
  `_INTENSITY=0.10`): these read ASR output, so they are the least certain measurements here — one
  mis-transcribed opener makes a complete thought look like a fragment. `standalone` leads the three
  because nothing downstream can supply context; `intensity` trails because it overlaps energy, and
  double-counting emphasis would let one loud, strongly-worded moment dominate a whole source. They
  also annotate the S10 prompt in **words, not numbers**, and only where the passage departs from
  ordinary — annotating everything is the same as annotating nothing.
- **`T9` — per-segment language detection.** Whisper reports *one* language for a whole file, so on
  bilingual content that label is wrong for part of every transcript, and wrong **silently**: the
  text appears, the timings are right, and the only symptom is degraded recognition on the passages
  in the other language.

  The two halves of this are not equally trustworthy and it says so. **Script switching is a fact** —
  Devanagari, Cyrillic, Hangul, Arabic, Hebrew, Greek and Thai occupy disjoint Unicode ranges — and
  is reported with high confidence. **Latin-script languages are only weakly separable**, from
  function words and diacritics, which works on a sentence and is noise on three words; under six
  words it declines to answer rather than guessing, and a tie between two languages counts as
  evidence for neither (Spanish and Portuguese share enough function words that a count-based
  detector would confidently pick whichever came first in the table).

  **Han script deliberately returns no language.** It is used by Chinese *and* Japanese, and choosing
  between them from characters alone is exactly the confident-and-wrong answer this exists to avoid.
  Combining marks are excluded from the script census, or a diacritic-heavy script outweighs a Latin
  passage of the same length and a mostly-English sentence comes back as Hindi.

  It reports **suspected switches** rather than correcting them, because correcting would mean a
  Whisper pass per segment. That belongs behind a setting, measured against M3's WER benchmark.
- **`T10` — a translated subtitle *track*, not a replaced transcript.** `task=translate` rewrites the
  transcript in English, so asking for a translation cost you the original-language captions
  entirely: a Spanish creator's clip came back with English burned into the pixels. Now the burned
  captions stay in the source language and English arrives as a second selectable track plus a
  `clip_N.en.srt`/`.vtt` sidecar, so one render serves both audiences.

  A **boolean**, not a target language (`SUBTITLE_TRANSLATION=false`): Whisper only ever translates
  *to* English, so a target-language field would be a control that silently ignores its own value.

  Both tracks are muxed in **one** ffmpeg call. `-metadata:s:s:N` numbers subtitle streams by their
  position in the *output*, so a follow-up remux of a file that already carries a track would either
  re-label the first one or need to know how many the input had; supplying every track at once makes
  the indices a property of the argument list. The original-language track goes first so a player
  defaults to it, and is now labelled with the language actually spoken rather than the hard-coded
  `eng` the single-track path used — a menu offering two entries both called English is no menu.
  Unknown codes become `und` ("undetermined", a real code) rather than an invalid two-letter one.

  Skipped, with a `subtitle_translation:skipped_*` marker on the clip, when the source is already
  English or `translate` was requested for the main pass — a second ASR pass to turn English into
  English is minutes spent for nothing, and an absent track with *no explanation* is
  indistinguishable from a broken one. A failure in the translation pass records
  `subtitle_translation:failed` and ships the clips: it is an extra track on a job whose expensive
  work is still ahead of it. The translated words are rebased onto the filler-removal keep plan
  alongside the originals, or the track drifts by the total removed duration and reads as a sync bug
  in the player.

  Off by default because it costs a full second ASR pass — cached separately by T8, so re-runs are
  free, but the first run roughly doubles transcription time.
- **`C19` — emoji placement, agreeing with the captions.** Two halves. First, the emoji planner now
  takes the **actual** highlighted word indices from the caption highlighter, and a highlighted word
  outranks every unhighlighted one regardless of salience. A11 already ranked by the same salience
  scorer the highlighter uses, which makes them agree *most* of the time — but the highlighter
  applies a per-cue budget and a floor, so its final selection is not a pure function of salience,
  and where they disagreed the emoji illustrated one word while the caption emphasised another. To a
  viewer that is a bug even though both components behaved exactly as written.

  Second, `EMOJI_PLACEMENT=caption` sits the glyph just clear of the caption block instead of in one
  of three fixed frame slots — above bottom captions, below top ones — so the glyph and the word it
  illustrates read as one element. That placement is only defensible *because* of the first half: an
  emoji pinned beside a caption while illustrating a word three seconds earlier looks like a
  mistake. Default stays `spread`, the shipped behaviour, so an upgrade does not move existing
  output.

### Added — caption typography and previewing (C6, C9, C14, C16, C17, C18, C20)

- **`C6` — real line wrapping, from measured text.** Captions relied on ASS `WrapStyle: 2`, which
  means *no automatic wrapping at all*: libass breaks only where the text already contains a `\N`,
  and nothing inserted one. Every cue was laid out as a single line and either ran past the frame
  edge or was shrunk by libass' own fitting, depending on the build. Neither is a decision anyone
  made.

  Width is **measured** from the vendored fonts' own advance widths, not counted in characters. A
  character budget is the obvious fix and is wrong for these faces: in Anton a `W` is roughly three
  times the advance of an `i`, so a 24-character budget is a comfortable line of `MINIMUM WIDTH` and
  an overflowing one of `WWWWWWWWWWWWWWWWWWWWWWWW`.

  Break positions are computed from the **plain words** and applied to the tagged spans, because a
  single `{\kf36}` is longer than the word it decorates — measuring the joined span text would count
  overrides as letters and break after roughly every word. Stated plainly: advance widths are not
  shaped text, so kerning and ligatures are not applied and a measurement can be a few per cent
  wide. That is the right error direction, and getting it exact would mean reimplementing HarfBuzz.
- **`C16` — a line budget per preset.** `max_lines` (default 2, matching the kinetic engine) is
  threaded through *cue building*, which is what makes C6 and C16 one feature rather than two: the
  wrapper deliberately never drops a word, so a cue holding more text than fits simply produces more
  lines than the budget. Measured on an 11-word sample: fitted grouping produces cues needing at most
  2 lines, unfitted grouping produces one needing **4**.
- **`C9` — a per-word pill.** A mainstream look with no equivalent here. Drawn as a heavy border in a
  solid colour rather than a box layer: ASS has no per-word background, and a real rectangle needs
  the rendered text width, which is not known where these tags are emitted. The trade is a
  glyph-hugging shape instead of a rectangle, which is closer to the reference anyway.

  Applied *inside* the highlight wrap. Both orders render acceptably — the pill sets `\3c` and the
  highlight sets `\c`, so they do not contest the same attribute — but the documented contract is
  that a highlight only *wraps* the plain span, and that is checked by substring. A contract enforced
  by substring has to stay syntactically true, not merely true in spirit.
- **`C17` — a dual stroke.** ASS carries one border width and one border colour, so a genuine
  two-tone stroke needs the text drawn twice. The shadow slot is repurposed instead: at zero offset
  with its own colour it renders as an outer stroke around the inner one. The honest trade, and why
  it is opt-in: a preset cannot have both a drop shadow and an outer stroke this way. It cannot
  produce a *gradient* stroke; nothing in libass can.
- **`C14` — 6 presets to 14.** `pill`, `pill_green`, `sticker`, `comic`, `headline`, `subtitle`,
  `karaoke_bold`, `spotlight`. Each is a **combination that was previously inexpressible** rather
  than a colour change — the pill, the dual stroke and measured wrapping are what make them
  distinguishable. Eight rather than fourteen deliberately: a preset whose only difference is a hue
  is a colour picker pretending to be a style. Every one names a vendored face, so none can silently
  substitute (`C1`).
- **`C18` — a real caption preview.** `POST /api/captions/preview` renders a two-second libass
  sample. `U5`'s picker previews in CSS, which shows the typeface, colours, case and placement and
  cannot show the word-by-word fill, the punch, the pill, the stroke or the wrapping — which is most
  of what a preset *is*. Two seconds rather than a still, because a still cannot show a sweep. The
  background is a generated mid-grey field, not the user's footage: a preview should show the
  caption, and a real frame makes it a test of that frame's legibility instead.
- **`C20` — auto-contrast.** `CAPTION_AUTO_CONTRAST` samples three frames from the region the caption
  will actually occupy — derived from the same position and safe-area maths the renderer uses, so it
  measures the pixels the text sits on rather than the frame average — and picks a dark or light
  outline. It **never** changes the fill: that is a brand decision (`U6`), and silently recolouring
  it because a shot was bright would overrule the one thing the creator chose. One decision per clip,
  not per cue, because an outline that changes colour partway through draws more attention than a
  slightly suboptimal constant choice.

### Fixed

- **A property test that held only because a feature was unused.**
  `test_p6_keyword_highlight_distinct_and_timing_preserving` asserted `plain in highlighted` — that a
  keyword highlight only wraps the span a plain word would produce. `C10`'s active-word punch is
  deliberately *suppressed* on a highlighted word (two competing `\fscx` spans would fight, and which
  applied would depend on tag order), so the plain span carries a ramp the highlighted one does not
  and containment cannot hold. No shipped preset set `punch_scale`, so nothing exercised it until
  `karaoke_bold`. The test now asserts against the animation core, which is what the requirement
  actually says.

### Added — caching, resumable jobs, and golden-output tests (I3, I5, I8, M1)

- **`I3` — cache the other whole-file decodes.** `T8` cached transcription and stopped there, so
  three full decodes still ran on every job for the same source: silence detection
  (`silencedetect` cannot work without decoding the audio), the energy envelope (one `astats` pass
  over the whole file), and keyframe sampling (48 seeks at 480 px). None depends on anything a user
  changes between runs, so re-running a source to try a different caption preset paid for all three
  again.

  Keyed on **content**, following `T8`: the usual path-and-mtime shortcut is wrong in exactly the
  case that matters — footage re-exported over the same filename. Every parameter that shapes a
  measurement is in the key too, because a silence map measured at −30 dB is not interchangeable
  with one at −25, and serving the wrong one silently is worse than having no cache. An **empty**
  result is cached like any other: "no detectable silence" is a real and expensive answer, and
  treating it as a miss would re-decode the whole file on exactly the sources where the measurement
  costs most.

  The keyframe half needed one line to be worth anything: the sampler now skips a frame whose file
  already exists. Without it the cache handed back a directory of correct frames and then overwrote
  every one of them. A zero-byte file counts as absent, since a run killed mid-write leaves one.
- **`I5` — resume a partially-completed job.** An interrupted job was marked failed *wholesale*:
  the clips it had already rendered were on disk and listed in the record, and the only way forward
  was to re-submit the source and pay for everything again, including re-rendering the clips that
  had succeeded.

  `Job.planned_clips` records the windows selection chose, before any rendering starts. A resume
  then renders only the windows with no clip — reusing `U7`'s explicit-candidate path, so selection
  does not re-run. That last part is correctness, not speed: selection is non-deterministic with an
  LLM in it, so re-running it could leave the user with clips from two different selections.

  Window matching allows a second of slack, because `AU7` silence trimming and `S9` cut snapping
  both move a clip's boundaries *after* the plan is recorded — exact equality would re-render clips
  that already exist. The interrupted-job message now distinguishes the two cases, since telling
  someone to re-submit a job whose finished clips are on disk costs them the whole render twice.
- **`I8` — frontend coverage beyond `api.js` and `Dropdown`.** 33 tests to 98, adding `ClipCard`,
  `JobCard`, `ReviewBar`, `ClipPlayer`, `CaptionStylePicker` and `BrandKitPanel`. The `U11`
  keyboard-shortcut tests matter most: a handler bound on the *window* is exactly the kind of code
  that works when tried by hand and misfires in a situation nobody demonstrated. The one that would
  hurt is typing — `a` must insert an `a` while someone edits a caption, not approve the clip behind
  it, and that failure is silent.

  Building them surfaced a real UI defect: the batch and per-clip verdict buttons had **identical
  accessible names**, so "Approve" was ambiguous to a screen reader as well as to a test. The batch
  ones are now "Approve selected clips".
- **`M1` — golden-output rendering tests.** The v0.8.0 parity gate compares *filter graphs*, which
  is blind to everything that changes pixels without changing the graph: a font resolving to a
  different face, a LUT that silently did nothing, an overlay drawn off-frame, a colour-matrix shift
  from an encoder upgrade.

  Renders are compared by **perceptual** frame hash rather than exact hash. An exact hash is
  reproducible for one ffmpeg build only, so a golden of exact hashes fails on every upgrade, gets
  re-frozen without inspection, and stops meaning anything — the failure mode a golden exists to
  prevent.

  Finding a signal that actually discriminated took measuring rather than reasoning. An average hash
  compares each cell to the frame's own mean, so it is invariant to contrast **by construction**: a
  burned-in caption bar moved 11 bits, a contrast/saturation change moved 2. Adding the mean luma
  did not separate them either — the graded clip moved 0.47 levels against 0.06 for a CRF 20→32
  re-encode. The luma **spread** is what does: 49.5 unchanged, 49.5 re-encoded, 75.5 graded. All
  three signals are stored, each covering what the others miss.

  Stated plainly: this detects *visible* change, not pixel-exact equality. A one-pixel caption
  offset or a metrically similar font substitution is below its resolution.

### Added — review workflow and brand kit (U3, U5, U6, U7, U9, U11)

- **`U3` — a review player instead of a playback one.** The clip surface was a bare
  `<video controls>`, which cannot step a frame or seek to a time you can name. Judging a clip
  means checking the specific things that go wrong in this pipeline — does it open mid-word, is the
  caption in sync, does the reframe lose the speaker, is the last frame a blink — and every one of
  those needs a frame you can land on and hold. Adds scrubbing, ±1 frame, ±1 second and a time
  readout. Frame stepping assumes 30 fps, which is what output is normalised to (`O3`).
- **`U5` — a caption style picker with a live preview.** The presets were a dropdown of six names,
  so choosing between "pop", "typewriter" and "hormozi" meant rendering a clip to find out what you
  had picked. `/api/info` now reports `caption_preset_details` — the presets' real values, with
  `#RRGGBB` equivalents added alongside the ASS originals, because no colour input accepts
  `&H00FFFFFF`.

  **The preview is labelled as an approximation.** libass does the word-by-word fill, the per-word
  punch, the outline geometry and the exact metrics; CSS reproduces none of them faithfully. What it
  shows honestly is what decides the choice: typeface, weight, colour pair, case, box and rough
  placement. A preview that overstates itself is trusted once and disbelieved thereafter.
- **`U6` — a brand kit: font, colour pair, logo and standing CTA.** These lived in places that could
  not be saved together — the caption font and colours inside a preset editable only in source, the
  CTA regenerated per clip by the LLM (so a creator with one standing ask got different wording on
  every clip), and no way to put a logo on a clip at all.

  The kit **overrides the preset** rather than the reverse: a preset is a *look* (how captions
  animate, where they sit), the kit is an *identity*, so `hormozi` plus a brand font should give
  hormozi's animation in the brand's typeface. It lives inside `settings`, so saved profiles
  persist it with machinery that already exists. Colour conversion is one named function with tests
  because ASS stores colours **byte-reversed** (blue-green-red) and getting that wrong does not
  fail — it renders a brand's red as its blue and reports nothing.

  The logo is drawn with the `movie` source filter, **not** a second ffmpeg input: the compositor's
  input indices are load-bearing (engine contributions, music, b-roll and emoji all compute offsets
  from them, which is what keeps the v0.8.0 parity guarantee), so an extra input would risk all of
  those to save nothing. It is composited above the captions and emoji — a watermark an overlay
  could cover is not a watermark — and the logo width is forced even, since an odd width in a 4:2:0
  frame makes ffmpeg pick a chroma alignment rather than fail.
- **`U7` — re-render one clip instead of the whole job.** Changing a caption preset or a colour
  grade meant resubmitting the source and paying for everything again: the download, the
  transcription, the selection call, the metadata generation and every *other* clip. Worse, it
  produced a **different set of clips**, because selection is not deterministic with an LLM in it.

  `run_pipeline` now accepts explicit candidates, which skips selection entirely. That is a
  parameter rather than a separate clip-render function on purpose: the per-clip path is two hundred
  lines of filler removal, diarisation rebasing, b-roll, engine stages, captions and thumbnailing,
  and a second copy would drift from it within a release. `Job.source_path` records the resolved
  local file, without which a URL job's source is only a URL. Edited metadata, the review verdict
  and the filename are all carried across — the filename because every clip URL, publish record and
  history row already points at it. The new media replaces the old only after the render succeeds.
- **`U9` — batch review.** A job produces up to ten clips, each had to be judged individually, and
  there was nowhere to record "I have looked at this". A review pass over twenty clips left no trace
  and had to be redone from the top after any interruption. Clips gain `review_state`
  (`pending`/`approved`/`rejected`) — defaulting to `pending`, so nothing is silently approved — with
  per-clip and batch endpoints and a tally bar. A batch with one stale id applies to the rest rather
  than failing wholesale, because discarding the other nineteen decisions to report one missing clip
  is the wrong trade.
- **`U11` — keyboard shortcuts for review.** `j`/`k` move, `a`/`x` judge, `s` selects, space plays,
  `,`/`.` step a frame, arrows skip a second. Bound on the window rather than per card, because the
  target is "the clip I am looking at" — and deliberately inert while a text field has focus, since
  `a` must type an `a` when someone is editing a caption. Getting that wrong would approve clips
  while a user wrote metadata.

### Added — publishing reliability and scheduling (PB4, PB5, PB6, PB7)

- **`PB5` — automatic retry of transient publish failures.** A publish attempt had exactly one
  chance: any failure wrote `state=failed` and stopped until a human pressed Retry, which makes a
  network blip indistinguishable from a rejected video and leaves an overnight batch at the mercy
  of whichever minute the upload landed in. Transient failures are now retried with exponential
  backoff (`PUBLISH_MAX_RETRIES`, `PUBLISH_RETRY_BASE_SECONDS`, `PUBLISH_RETRY_MAX_SECONDS`).

  Three details carry it. **The default is not to retry** — an error has to be *recognised* as
  transient to earn one, because a too-long clip or a revoked permission will fail identically
  forever and retrying it burns quota while hiding the real problem. **Permanent patterns take
  precedence over transient ones**, since a platform saying "video too long, please try again with
  a shorter clip" contains "try again" while being the least retryable error there is. And the
  backoff carries **proportional jitter**, because every attempt failing against one platform
  outage becomes due at the same instant and would otherwise retry in lockstep.

  Automatic retry never touches a `review_required` attempt — that is waiting on a person, and
  re-queueing it would either loop or silently escalate a review-mode submission into a live post.
- **`PB4` — token refresh and expiry.** YouTube exchanged its refresh token on *every* publish
  while discarding the expiry Google returns, so there was one extra round trip per upload and
  nothing in the product could say when a credential would stop working. Access tokens are now
  cached in a new `oauth_tokens` table with their expiry and renewed just ahead of it
  (`PUBLISH_TOKEN_REFRESH_MARGIN_SECONDS`, default 5 minutes — an upload takes tens of seconds, so
  a token expiring mid-request costs the whole file). A 401 on a cached token triggers exactly one
  forced refresh and retry; a second 401 is a credential problem, not a stale cache.

  The other four publishers **cannot** refresh, and now say so rather than being silently
  hopeless: TikTok, Instagram and X use long-lived tokens an operator pasted into config, Whop an
  API key. `PublisherStatus` gained `token_kind` (`refreshable` / `static` / `none`) and
  `token_expires_at`, so a dashboard can distinguish "nothing to expire" from "an expiry we cannot
  see" — reporting both as "no expiry" is what tells an operator their Instagram token is fine
  right up until the day it is not. `POST /api/publishers/{platform}/refresh` returns
  `refreshed: false` for those four instead of pretending to act.
- **`PB6` — per-platform caption and hashtag fitting at publish time.** Metadata is generated once
  for one platform and the same text was sent everywhere; the per-platform limits were applied only
  at generation, so publishers chopped the result at a character index
  (`f"{title}\n\n{caption}"[:280]`). That cuts mid-word and removes the call to action and the
  hashtags — the parts doing the work — and on X the title could consume the whole budget.

  Copy is now *fitted* per destination: hashtags beyond the platform's count are dropped, and the
  description is shortened at a sentence boundary, then a clause, then a word. The limit is applied
  to the **rendered caption**, with the CTA and tags reserved first, because a per-field clamp
  produces a caption that overflows the moment the tags are appended. The stored request keeps the
  full text, so a retry or a re-route re-fits from the original rather than compounding.
  `PUBLISH_TAILOR_WITH_LLM` (off by default) regenerates the description for each destination
  instead, which is what the plan asks for and costs one model call per platform per clip.
- **`PB7` — scheduling.** Scheduling was a single `datetime-local` input: an operator could set a
  time and then had no way to see what was scheduled, move it, or cancel it — the only recourse for
  a wrong time was to let it publish. Added `GET /api/schedule` (a window of attempts),
  `PATCH /api/publish-attempts/{id}/schedule`, `POST /api/publish-attempts/{id}/cancel`,
  `GET /api/schedule/suggestions`, and a month-calendar UI under a new **Schedule** tab.

  The calendar shows every state, not only pending ones: hiding what had already gone out would
  show an empty week the operator had in fact filled. Cancelling records the attempt as failed with
  an explicit reason rather than deleting the row, because a row that vanishes is
  indistinguishable from one that never existed when a post is later found missing. Rescheduling
  into the past is recorded as `queued` rather than left `scheduled` behind the clock.

  **The best-time suggestions are labelled honestly.** They are published third-party heuristics,
  not measurements of your audience — per-account timing needs post-publish engagement data
  (`PB8`), which this installation does not collect. The API returns a `basis` string saying so and
  the UI renders it, because presenting a guess as an analysis is the actual harm available here.
  Suggestions also skip slots already scheduled, since otherwise the calendar keeps recommending
  the one best hour that is already full and following its advice stacks four posts at 7pm.

### Added — speech repair and per-platform output (AU4, AU5, O7, O12)

- **`AU4` — speech de-noise.** `SPEECH_DENOISE` (off | light | standard | strong) applies
  `afftdn` to the speech before anything is mixed into it. Off by default and conservative when
  on: over-reduced noise leaves speech sounding underwater and gated, which is a worse defect
  than the room tone it removed.

  The plan item notes that `arnndn` "is available in ffmpeg". The *filter* is; the *capability*
  is not, because ffmpeg ships no `.rnnn` models — they live in a separate repository. So
  `arnndn` is used only when `SPEECH_DENOISE_MODEL` points at a real model file, and a
  configured-but-missing model degrades to `afftdn` rather than failing the render. A test
  asserts both halves of this, so nobody removes the `afftdn` path believing `arnndn` was ready.
- **`AU5` — de-esser.** `DEESSER` (off | light | standard | strong) reduces sibilance on a harsh
  or close-miked source. Even "strong" is intensity 0.6 rather than 1.0: at full intensity the
  4–8 kHz band loses so much energy that consonants lose definition, which is a different defect
  rather than a fix.

  **The de-reverb half of AU5 is deliberately not implemented.** ffmpeg has no de-reverb filter —
  not one this build lacks, one that does not exist upstream. Real de-reverberation needs
  spectral deconvolution or a trained model, i.e. an external dependency. A high-pass plus a gate
  is sometimes sold as "de-reverb"; it is not, and shipping it under that name would be worse
  than the gap. A test asserts no such function appears and that the de-esser chain contains
  neither filter, so the gap cannot be quietly papered over later.
- **`O7` — per-platform output profiles.** `OUTPUT_PLATFORM` selects a destination profile
  (`tiktok`, `instagram`, `youtube`, `youtube_shorts`, `x`, `whop`) which sets the resolution,
  the VBV ceiling and the clip-length cap. Capping length at render time matters: a clip longer
  than the destination accepts otherwise fails at upload having already cost a full render, and
  the pre-flight check that catches it (`O10`) runs at the end.

  Three boundaries were drawn deliberately. **The aspect is advisory only** — reported for the UI
  to recommend, never applied, because silently re-shaping an explicit 9:16 request into 16:9
  because a platform was named is a setting that fights the interface. **An explicit
  `OUTPUT_SHORT_SIDE` or `OUTPUT_MAX_BITRATE_KBPS` always wins**; the profile only fills in what
  the operator has not chosen. And **this selects one profile, not N renders** — a variant per
  platform is N encodes, N thumbnails and N publish records per clip, a job-model change rather
  than an encoder setting.

  Duration ceilings are read *from* `publishers.preflight.PLATFORM_LIMITS` rather than restated,
  so the encoder and the validator cannot disagree about the same number. `youtube_shorts` is the
  one override: preflight has no Shorts entry — correctly, since a Shorts upload *is* a YouTube
  upload and is validated as one — so reading the table gave a Shorts profile with a one-hour
  ceiling. It is pinned to the Shorts product limit of 3 minutes.
- **`O12` — burned-in vs soft captions.** `CAPTION_MODE` (burned | soft | both). Burned-in
  remains the default and is right for short-form — the feeds autoplay muted and nobody enables a
  subtitle track — but it is a permanent, untranslatable, un-hideable decision baked into the
  pixels. `soft` adds a selectable `mov_text` track instead; `both` does each.

  `mov_text` is plain text, so preset animation, per-word highlighting, glyphs and positioning
  cannot survive in the soft track. That is a limit of what MP4 can carry, not of this tool, and
  it is why `both` exists rather than `soft` simply replacing `burned`. The mux is a stream copy,
  so it costs a remux rather than a re-encode — verified by comparing the video stream's MD5
  before and after. It runs in the pipeline on the finished file rather than in the compositor,
  because a subtitle stream added mid-pipeline would be silently dropped by any POST-stage engine
  that replaces the media.

### Fixed

- **One filter-path escaper, not four.** `captions`, `overlays` and `reframe` had each grown their
  own copy and they had *diverged*: two resolved the path and escaped backslashes, the third
  (added with the V18 LUT) did neither and rewrote backslashes as forward slashes. Both are
  defensible; the combination is not, since which behaviour you got depended on which effect you
  enabled. All four now delegate to `ffmpeg_utils.escape_filter_path`.

### Added — visual polish: framing, grading and motion (V5, V6, V8, V14, V16, V18, V19)

- **`V16` — de-letterbox before reframing.** Source footage is very often already boxed: a 16:9
  export inside a 1:1 frame, anything re-uploaded from another platform. Reframing that
  *centred the crop on the bars* and baked black bands into the middle of the vertical output.
  The content rectangle is now detected first and the crop is confined to it. Detection
  deliberately skips the opening second, because an opening fade from black looks exactly like a
  fully-letterboxed frame to `cropdetect` — probing from zero would crop the picture away
  entirely. The cost of that choice is that clips shorter than the skip window go undetected,
  which falls back to using the frame as-is.
- **`V5` — split-screen tiles now move.** Each tile was frozen on the *mean* of its track's boxes
  for the whole clip: a position the speaker occupied only on average, so anyone who leaned in
  and back sat off-centre for most of the clip and anyone who moved was cropped out of their own
  tile — while the single-speaker path has followed faces since v0.7.0. Each tile now gets its
  own smoothed, clamped centre path.

  The mechanism is worth recording. `sendcmd` addresses filters **by name**, and a split-screen
  graph contains several `crop` filters, so the obvious implementation broadcasts every tile's
  commands to every crop. Verified by building that version: the second tile stops moving
  entirely. Each crop is therefore given an *instance* name (`crop@t0`, `crop@t1`).
- **`V6` — three and four speakers.** Split-screen supported exactly two. More than two now lay
  out as a 2-column grid rather than a stack: four tiles stacked in a 1080x1920 frame are
  1080x480 slots — a 2.25:1 letterbox holding a crop of a face — whereas two columns give
  540x960 portrait slots that match the shape of a head and shoulders. An odd final tile spans
  the full width rather than leaving a black half-cell that reads as a dropped participant.
  Landscape targets keep their side-by-side layout, which already produces portrait-shaped slots.
- **`V8` — crop-update rate.** Was a hardcoded 12/s, visible as stepping when the subject moved
  quickly. Now `REFRAME_COMMAND_FPS`, defaulting to 24. This only lengthens the `sendcmd` script;
  the expensive part of reframing is face sampling, which has its own rate.
- **`V18` — 3D LUTs.** `COLOR_LUT` applies a `.cube`/`.3dl` after the colour preset, for a look
  the five presets cannot express — a house grade, or a client's brand LUT. After rather than
  before, because a LUT maps final values; the reverse would feed its output back into a contrast
  curve. A missing or non-LUT file is ignored rather than failing the render, since `lut3d`
  rejects the whole filtergraph on a file it cannot parse.
- **`V19` — eased and beat-synced zoom.** `ZOOM_EASE` replaces the linear Ken Burns ramp with a
  smoothstep: a constant rate is the one thing a camera move never does, and on a long clip the
  frame visibly creeps. Same start and end zoom; only the curve changes. `BEAT_SYNC_ZOOM` adds a
  short scale bump at detected audio accents — onset detection over the energy envelope already
  measured for `S2`, *not* beat tracking, so every bump sits on a real transient instead of an
  estimated tempo (on speech-led footage there is often no tempo to track). The bump is a
  *multiplier*, so it composes with Ken Burns and the opening punch instead of pushing the total
  zoom past what either intended.
- **`V14` — end card.** Clips ended the instant the speech did, wasting the one moment a viewer
  has already decided to watch to the end. `END_CARD_TEXT` draws a call-to-action over the tail.
  It is a libass event rather than `drawtext`, like every other piece of text here, so it
  survives an ffmpeg built without freetype; it is a *standalone* ASS so it works with captions
  off and with captions owned by the kinetic-typography engine; it does not fade out, because the
  clip ends under it; and the hold is capped at half the clip so a 3-second clip does not become
  an advert with a clip attached.

`ZOOM_EASE`, `BEAT_SYNC_ZOOM`, `COLOR_LUT` and `END_CARD_TEXT` all default to the previously
shipped behaviour, following the same convention as `TRANSITION_STYLE` and the `PROGRESS_BAR_*`
settings. That is what keeps the v0.8.0 byte-parity gate meaningful: a new setting defaulting to
on would force the goldens to be re-frozen each release and stop them detecting accidental
change.

One existing property test changed. `test_p16_split_screen_tiles_target_exactly` asserted that a
portrait target always stacks full-width tiles, which `V6` makes false. It now checks the
partition property layout-agnostically — non-overlapping, in-bounds, areas summing to the frame,
which is sufficient for an exact cover — rather than pinning one arrangement.

### Added — clip selection now measures the audio and scores the opening (S2, S6, S10, S17)

- **`S2` — audio energy.** One `astats` pass per source produces an RMS envelope at one reading
  per second, from which each candidate gets its mean/peak level, how far it sits above the
  source's own median, and what fraction of it is silence. Two details carry it: readings are
  regrouped with `asetnsamples` first, because otherwise "RMS per frame" silently means a
  different window length per codec; and `-inf` (which ffmpeg reports for digital silence) is
  floored, because letting it into a mean or median poisons every comparison downstream without
  raising anything.
- **`S6` — hook scoring** for the first 2.5 s specifically. Retention is decided there and
  nothing modelled it: a clip whose best line sat forty seconds in scored the same as one that
  opened on it. Combines how promptly speech starts, pace and energy *relative to the clip's own
  average* (a hook is front-loading, not loudness), and a low-weighted textual-opener signal.
  Silence at the opening is disqualifying rather than merely costly.
- **`S10` — the measured signals now reach the LLM.** Transcript lines in the selection prompt
  carry a short delivery note (`fast`, `loud`, `mostly silent`) where a segment departs from the
  speaker's own norm. Words rather than numbers, on purpose: the model is picking moments, not
  doing arithmetic, and raw figures invite it to invent a formula from a scale it cannot
  calibrate. Ordinary segments get no note, so the annotated ones stand out — which is the
  entire signal.
- **`S17` — every weight is a setting.** The defaults are a starting point, not a measured
  optimum; tuning them against the `S1` benchmark is now a config edit rather than a release.

### Changed — the fallback ranks on measured signals instead of clip length (S11, S14, S15)

- **`S11`** — `segment_video` capped the count by keeping the **longest** segments, which is
  worse than arbitrary: the longest silence-delimited segment is usually the stretch where
  nobody paused, so a monologue with no beats outranked every punchy exchange. And this was not
  a rare path — it runs with no API key, on any failed LLM call, and on every `fixed`/`silence`
  run by choice. The fallback now takes every segment, scores it on hook/pace/energy/length-fit,
  and caps afterwards. Verified on a 120-second render: the loud, fast window ranks first where
  the old rule put a quiet 45-second opening there.
- **`S15` — overlapping and duplicate candidates are dropped, before the count is capped.** The
  model routinely proposes `12.0-45.0` and `14.5-47.0` for one moment and both used to ship.
  Overlap is measured against the *shorter* candidate, so a clip wholly inside another reads as
  1.0 rather than the low IoU that would let it through. De-duplicating before the cap is what
  lets a genuinely different moment take the freed slot instead of the user simply getting fewer
  clips.
- **`S14`** — keyframe sampling raised from 12 frames at 160 px to 48 at 480 px. Twelve frames
  across an hour is one every five minutes, so every candidate in a five-minute stretch scored
  identically; and at 160 px the motion proxy averages away the frame-to-frame difference it
  exists to detect.

### Fixed — three defects found while building the above

- **Measured features were silently discarded whenever visual selection ran.** `merge_scores`
  and `_snap_candidates` rebuilt `ClipCandidate` without copying `features`, so every S2/S4/S6
  measurement vanished — and `U1` had made visual selection a default, so that was the normal
  path. Nothing failed; the clips were fine and the features were simply gone.
- **Sentence snapping could annex a neighbouring clip.** Snapping moves the start to the nearest
  segment start and the end to the nearest segment end, so on a coarsely-segmented transcript any
  window inside one long segment became that entire segment — two distinct moments collapsing to
  one identical window, and **two byte-identical clips shipping**. Snapping is now skipped when
  it would swallow another candidate's midpoint. Found because `S15` spotted the collision and
  dropped a clip, which is how the pre-existing duplicate came to light.
- **De-duplication compared word *sets*, which called different clips identical.** Two long
  windows drawn from a small vocabulary shared a content-word set and scored 1.0, so a real
  120-second render returned two clips where three were asked for. Now count-aware, which scored
  that pair 0.25 while still scoring a genuinely reordered recap above 0.9. A minimum token count
  guards the same error in the other direction: Jaccard over two words is noise, and acting on
  noise here deletes a moment the user wanted with no trace it was ever a candidate.


### Added — transcription can be told what to expect, and says when it is unsure (T4, T5, T7, M3)

- **`T4` — vocabulary prompt.** Whisper has no reason to expect a person's name, a product or
  domain jargon, so it mis-hears the same word the same way every time it is said — and that
  mistake is burned into every clip's captions. A per-video **Names & jargon** field now feeds
  the decode, alongside a standing `WHISPER_INITIAL_PROMPT` for terms a channel always uses. The
  per-video terms go last, because Whisper's conditioning weakens with distance from the audio.
- **`T5` — VAD is adjustable.** Voice-activity detection was switched on with every parameter at
  the library default, so nothing could be tuned for difficult audio. Threshold, minimum
  silence, minimum speech and speech padding are now settings — **at faster-whisper's own
  defaults**, so behaviour is unchanged until something is changed deliberately.
- **Both are part of the transcript cache key.** A transcript decoded with a vocabulary prompt,
  or with VAD tuned to keep quiet speech, is not interchangeable with one decoded without.
  Omitting them would serve the stale transcript forever — silently, with nothing downstream
  able to notice, which is worse than having no cache. The VAD parameters are excluded when the
  filter is off, since they cannot affect the output then and would only cause pointless misses.
- **`T7` — low-confidence words are dimmed rather than asserted.** Captions stated every word
  with identical confidence, including the ones the model barely guessed at; on difficult audio
  that is the pipeline presenting a mis-transcription as fact in its most visible artefact. Off
  by default. Deliberately a *dim*, not a colour or a `[?]` marker: a colour would collide with
  keyword highlighting, and a marker draws the eye to the pipeline's own uncertainty rather than
  to the speech. A word with no probability reads as **confident**, not doubtful — treating
  "unknown" as "unsure" would dim every caption on any transcript without per-word confidence,
  the same failure mode `C11` had.
- **`M3` — a WER benchmark** (`evaluation/wer.py`, `scripts/eval_transcription.py`). `T1` raised
  the default model on argument; this measures it on *your* audio. Errors are reported by kind,
  because deletions point at VAD (`T5`) and substitutions at vocabulary (`T4`) and the two call
  for opposite fixes; the table names whichever dominates. Aggregation pools errors before
  dividing rather than averaging per-file rates, which would let one short difficult file
  dominate a figure meant to describe the dataset.

### Fixed

- **`evaluation/wer.py` normalisation folded no typographic apostrophes.** Unicode NFKC does not
  touch U+2019, so a reference typed in a word processor split `don't` into `don` and `t` and the
  contraction table never matched — two substitutions per contraction on every human-written
  reference. It would have inflated each model's score roughly equally, so the comparison would
  still have looked entirely plausible.
- `CaptionPreset.to_dict`/`from_dict` now carry the T7 fields, so the setting survives being
  persisted in a saved profile instead of appearing to work until reload.


### Added — the hard-coded look becomes a choice (V9, V11, V13, O5, O9, O11)

- **`V11` — letterbox background styles.** The background was one look: `boxblur=40` plus a
  slight darkening. It suits talking-head footage and actively hurts other things — a blurred
  screen recording is an unreadable smear. Now `blur | mirror | black | color | gradient`.
  `black` is the honest choice for a screen recording, where any derived background competes with
  text the viewer is reading; `mirror` reads as intentional where a blur reads as a mistake.
- **`V13` — progress bar position and style.** It was a 12px bar in one cyan, always at the
  bottom — where on a 9:16 clip it sits directly under the captions. `top` exists for that
  reason rather than for variety. The new `track` style adds a dim full-width rail, so how much
  is *left* is visible and not only how much has passed.
- **`V9` — opening transition styles**: `punch_in` (the original), `zoom_cut` (a step with no
  easing, so it reads as an edit rather than a movement), `whip_pan` (a lateral slide that
  decelerates into place) and `dissolve`. **Note on scope:** the plan asks for *clip-to-clip*
  transitions, and there is nowhere in this product for one to live — every clip is an
  independent deliverable published separately, and a transition needs two shots that meet. What
  those named effects can be here is the treatment of a clip's own opening, which is where they
  would be seen anyway.
- **`O5`/`O9` — output resolution is selectable**: 720, 1080, 1440 or 2160 by short side. It was
  fixed at the 1080-class values, so a 4K source was always downscaled and a low-powered host
  could not trade quality for encode time. Both dimensions are forced even, since libx264's 4:2:0
  subsampling requires it and an odd dimension fails the encode outright.
- **`O11` — sidecar `.srt` / `.vtt` export**, alongside the burn-in rather than instead of it.
  Burn-in is right for short-form, but it is not the only thing captions are for: a sidecar is
  what lets a platform show selectable captions and lets a viewer using a screen reader reach the
  text at all. Two formats because they are not interchangeable — they differ in timestamp
  separator, a mandatory header, and escaping, and each difference breaks a player silently
  rather than raising. Sidecar cues are grouped longer than the burned ones: three-word cues are
  right read at full width in a glance and flicker once a second in a player's own small type.

### Changed

- **`V4` — reframe tracking resets at shot changes.** The EMA smoother carried the previous
  shot's framing across a cut and converged on the new one over the following second, which reads
  on screen as the crop *searching* for the subject after every cut. The subject did not move; the
  camera changed, and the right response to a discontinuity in the input is a discontinuity in the
  output.
- **`V17` — the thumbnail frame is chosen, not assumed.** It was taken at
  `min(1.0, duration / 2)` — a fixed position with no relationship to what is in the frame, so a
  clip opening on a cut or a blink had exactly that as the still representing it everywhere.
  Three candidates are now scored on sharpness (motion blur is the most common way an automatic
  thumbnail looks bad, and unlike a dark frame it is unrecoverable) and on mid-range exposure, so
  a blown-out frame loses as heavily as a black one. Deliberately no face detection: the only
  detector available is the 2001-era cascade `V2` exists to replace, and its false positives on
  texture would actively mislead this.

### Fixed — the long-standing kinetic Property 12 counterexample

- A synthesised caption word could ship **shorter than its documented `MIN_WORD_S` floor** — 0.0769 s
  against 0.08 at 13 fps. The cause was not the widening arithmetic that previous analysis
  suspected: the disjointness cursor in planner step 5 runs *after* `normalize_segments` has
  dropped sub-minimum cues, so it could shave a cue's front down to a single frame that
  normalisation never saw. The widening below then had nowhere to widen *into*, because the cue
  was itself shorter than the minimum.
- Such a cue is now dropped, matching the rule normalisation already applies. Dropping rather than
  relaxing the floor, because a cue that short is drawn for less than one frame — nobody sees it —
  whereas keeping it means emitting a word whose interval contradicts the contract every consumer
  reads. Verified over an 810-configuration deterministic fps sweep (0 violations), and confirmed
  load-bearing by reverting it.


### Added — jobs can be stopped, and the time they take is visible (I4, I6, M5, U8)

- **`I4` — job cancellation.** There was no way to stop a running render: a job submitted by
  mistake occupied the single worker until it finished and, because the pool is serial, held up
  everything queued behind it. The only remedy was restarting the process, which lost every other
  job's state. `POST /api/jobs/{id}/cancel`, and a Cancel button on the job card.
  - **Cooperative, not pre-emptive**, and the difference is stated rather than glossed: a
    **queued** job stops immediately, a job **between stages** stops in well under a second, and
    a job **inside an ffmpeg pass finishes that pass first**. `subprocess.run` exposes no handle
    to signal, so terminating mid-encode means restructuring every encode site around `Popen` —
    a change with its own failure modes that belongs with the concurrency work (`I1`).
  - **`cancelled` is a distinct status, not `failed`.** A job the user stopped did not go wrong,
    and collapsing the two would both mislead the operator and inflate any failure rate computed
    from these records.
- **`I6` — every log line carries its job id and stage.** A line could not previously be
  attributed to a job; survivable with one worker by reading timestamps, and not survivable at
  all once `I1` lands and two renders interleave their output. The lines that matter most are the
  degradation markers, which are exactly the ones needing to be traced to one clip in one job.
- **`M5` — per-stage render timings**, on the job record, at `GET /api/jobs/{id}/timings`, and
  behind a click on a finished job. Nobody knew where the minutes went: which stage dominates is
  not guessable, because the pipeline performs at least three full re-encodes per clip, so on a
  short source with a long clip list the encodes can outweigh transcription entirely. Timings are
  recorded for **failed** stages too — a stage that reliably burns ninety seconds and then throws
  is the most useful row in the report and would be invisible if only successes were measured.
- **`U8` — progress shows structure.** It was one coarse fraction and a free-text stage string,
  so the UI could only show a bar and a sentence. Jobs now report which step of how many, so a
  long stage reads as progress rather than as a stalled bar.

### Changed

- **`U10` — failures and empty results explain themselves.** The raw exception text a job fails
  with is written for a log, not a person. Recognised causes — a missing source, no ffmpeg, a
  timeout, an undecodable file, a full disk — now carry what to do about them, with the original
  message kept below as the evidence. A completed job with no clips gets its likely causes
  instead of a bare "no clips were generated".
- **`U13` — the fallback landing page reports real state**: version, whether ffmpeg actually
  resolves, the model, the storage backend, job counts and the registered engines. Anyone reaching
  that page got there by accident and needs "is the backend healthy" answered; a page that only
  says "the UI is not built" answers neither that nor "what is missing".

### Fixed

- **`I11` — the two `exhaustive-deps` warnings are gone, and one hid a real bug.** The job-polling
  effect depended on `jobs.length`, which does not change when a job merely goes from *processing*
  to *completed* — so the fast 1.2 s poll continued indefinitely after everything had finished.
  The activity check is now derived outside the effect, which both silences the warning and lets
  the poll actually slow down.

### Changed — one name for the shared encoder arguments

- Two branches independently centralised the duplicated libx264 flags, under two names:
  `video_encode_args()` and `h264_args()`. They are now one function, `h264_args()`, which keeps
  the settings-driven `-crf`/`-preset` from the first and the `normalise_fps` (O3) and `vbv_cap`
  (O4) switches from the second. The compatibility flags (`-pix_fmt`, `-profile:v`, `-level`) live
  in `H264_COMPAT_ARGS` and stay deliberately non-configurable: there is no good reason to ship a
  clip a player will refuse to open.
- Clip-start snapping (S9) now runs *before* speech-rate measurement (S4), so the features describe
  the window the viewer actually receives rather than the one the model proposed.


### Fixed — clips could open mid-shot (S9)

- Clip starts now snap to a nearby shot boundary. A clip beginning two seconds into a shot opens
  on a fragment — half a gesture, the tail of a camera move — and reads as careless before the
  viewer has heard a word. The `S1` harness showed the scale: at IoU 0.7, the threshold that asks
  whether *boundaries* are right rather than whether the right moment was found, the selector
  scored **zero across the board**.
- **ffmpeg's scene score rather than PySceneDetect.** The plan names PySceneDetect and it is the
  standard tool, but this needs one number — "is there a hard cut near here" — and ffmpeg is
  already the dependency every other stage shells out to.
- **Narrow windows, not a full scan.** Detection decodes video, so only a couple of seconds either
  side of each candidate start is examined. Scanning an hour-long source to move a boundary by
  under a second would be wildly disproportionate.
- **Only the start moves, and only within a cap.** The ending is chosen for content reasons — a
  punchline, a completed thought — so a shot change near it is not a reason to truncate. Beyond
  the cap the boundary the selector chose is kept: moving a start several seconds to reach a cut
  is not snapping, it is choosing a different moment.
- **A documented blind spot.** ffmpeg scores scene change on luma, so a cut between two shots of
  similar brightness is invisible to it. A test pins that deliberately so it is not later mistaken
  for a regression. A missed cut leaves the boundary exactly where it was, which is the previous
  behaviour.


### Added — the first audio-derived selection signal (S4)

- **There were no audio features in clip selection at all** — verified by grep: no pitch, no
  energy, no speech rate, no laughter. The LLM saw only `[i] start-end: text` lines, so it could
  not tell that a moment was delivered fast, slowly, or after a pause. Speech rate is the
  cheapest of those signals because the data already existed: word timestamps are already
  produced, so this is one pass over a list.
- **Relative rate is the signal; absolute rate is context.** A measured lecturer and an excitable
  streamer sit at very different words-per-second and neither figure says which *moment*
  mattered. `relative_speech_rate` normalises against the source's own median, so 1.0 is that
  speaker's normal pace and 2.5 is a burst.
- The baseline is a **median, not a mean**: a silent stretch or music interlude produces near-zero
  slices, and a mean would sink the baseline until ordinary speech read as fast — inverting the
  signal exactly where footage is hardest.
- A `reliable` flag distinguishes **"not measurable" from "average pace"**, since both report a
  relative rate of 1.0. Two words in 0.3 s is 6.7 words/sec, which describes a measurement
  artefact rather than fast speech.
- **Nothing here changes ranking**, and there's a test asserting scores and ordering are
  untouched. The features are attached to every candidate — including the fallback path, so the
  two selection paths can be compared on the same terms — so that `S1` can judge whether they
  *should* influence ranking. Choosing a weight first would make an improvement and a regression
  look identical.


### Fixed — invented transcript text reached captions and the selector (T3)

- Whisper invents text over music, applause and silence, and gets stuck in decode loops that
  repeat a phrase for tens of seconds. **Nothing filtered either**, so hallucinated text was fed
  to the LLM selector as though it were speech and burned into captions.
  `worker/transcript_filter.py` now drops segments that look invented or looped.
- **Whisper's own two indicators are captured** — `no_speech_prob` and `avg_logprob` were being
  discarded by `TranscriptSegment`. `no_speech_prob` is the model's estimate that a span
  contains no speech, which is precisely the condition it hallucinates under.
- **Every rule requires two independent signals to agree**, because the error costs are
  asymmetric: a false positive deletes real speech and nothing downstream can notice, while a
  missed hallucination is visible in the clip. `no_speech_prob` is never acted on alone — it
  runs high on whispered, distant and accented speech that is entirely real.
- **There is deliberately no boilerplate phrase list.** Whisper's inventions cluster around
  "Thanks for watching" and "Subscribe to my channel", and those are things people genuinely
  say — a phrase list would silently delete the outro of every video that has one.
- **A circuit breaker**: if fewer than half the segments would survive, nothing is dropped. If
  most of a transcript looks invented the thresholds are wrong for that audio, and emptying it
  turns a poor transcript into no clips. Keeping a bad transcript is recoverable.
- Filtering runs **after** the transcript cache, so tuning a threshold takes effect on the next
  run instead of invalidating hours of ASR, and the cache stays lossless. Cache schema bumped
  to v2 to carry the new signals.


### Fixed — output could exceed full scale and clip (AU3)

- A true-peak limiter now ends the audio chain, at `LOUDNESS_TRUE_PEAK_DB`. `loudnorm`
  *targets* a true-peak ceiling and lowers its gain to respect one, but only on the path where
  it runs: with normalisation disabled or the source unmeasurable, nothing constrained the
  output. Measured on a mix of a −0.1 dBFS source and a bed, the result reached **+5.5 dBFS
  true peak**; with the limiter, −1.0.
- **`level=disabled` is the load-bearing argument.** `alimiter`'s `level` defaults to *on*,
  which auto-levels output up to the ceiling — so a filter whose job is to attenuate would, in
  its default configuration, make quiet audio *louder*. Measured: a quiet input came out 1 dB
  hotter and 1 LU higher, which would have silently shifted every clip off the platform
  loudness target `AU1` just set.
- Placed after loudness normalisation, since anything that can raise a level belongs upstream
  of the thing that catches it. No `effects_applied` marker: a limiter that does not engage is
  inaudible, and those markers describe choices rather than guards.


### Added — transcripts are cached (T8)

- ASR is the most expensive stage in the pipeline and the most repeated: re-running a source to
  try a different caption preset, aspect ratio, or any of the effect toggles re-transcribed
  audio that had not changed. `worker/transcript_cache.py` caches by source content hash, and
  `transcribe()` now consults it.
- **The ASR parameters are part of the key**, not just the file. A transcript from the `base`
  model is not interchangeable with one from `small` — which `T1` just changed — and
  language/translate/beam size change the output too. Keying on the file alone would have
  served stale `base` transcripts to the upgraded model silently and permanently, which is
  worse than no cache.
- **The file is hashed by content, not path and mtime.** Hashing a gigabyte costs seconds where
  transcribing it costs minutes, so correctness is cheap here — and path-and-mtime keying is
  wrong in exactly the case that matters: footage re-exported over the same filename.
- Writes are atomic (temp file plus rename), so a job killed mid-write cannot leave a truncated
  entry. Any cache failure — unwritable directory, unhashable source, corrupt entry — degrades
  to plain transcription: a cache is an optimisation and must never fail a job.
- The `S1` harness now delegates to the same module instead of carrying its own cache keyed on
  path/size/mtime with its own JSON shape. Two caches of the same thing had been disagreeing
  precisely where it mattered.


### Added — selection evaluation harness (S1)

`docs/IMPROVEMENT_PLAN.md` puts this first in §3 and says why: *"Without this every change
below is unmeasurable."* Every proposed selection improvement — audio energy, pitch, speech
rate, hook scoring, scene awareness — is a change to a ranking, and a ranking cannot be
improved by inspection.

- **`evaluation/`** — label format, IoU matching, precision@k / recall@k, naive baselines, and
  a runner. **`scripts/eval_selection.py`** is the CLI: `template`, `validate`, `run`,
  `compare`. `eval/README.md` covers how to label.
- **Baselines run on every evaluation**, and this is the point of the design. Whether
  precision@5 of 0.40 is good depends entirely on what picking clips *without thinking* scores
  on the same footage, and that number is not guessable — it moves with source length and with
  how many moments were labelled. `uniform` is the no-information floor, `random` is chance,
  and `longest` reproduces what the shipped deterministic fallback does when it caps the count.
  If the LLM selector cannot beat `longest`, the fallback is not a fallback — it is the product,
  and the report says so in those terms.
- **Matching is one-to-one.** Without that, a selector could return five near-identical clips
  over one good moment and score five hits — rewarding exactly the redundancy S15 exists to
  remove. IoU also uses union as its denominator, so returning the whole video scores ~0.01
  rather than "containing" every moment.
- **Results are reported at IoU 0.3 / 0.5 / 0.7**, because the threshold is a judgement: 0.3 is
  "found the right part of the video", 0.7 is "got the boundaries too". A single number would
  hide a change that improved targeting while worsening cuts.
- **`mean best IoU` is reported separately** as the diagnostic precision cannot express. Two
  selectors scoring zero at 0.5 are not equivalent — one landing at 0.45 is cutting the right
  moment badly, one at 0.05 is looking in the wrong place. The report flags the first as a near
  miss and points at S9 rather than at the scoring signals.
- **Transcripts are cached** (keyed on path, size and mtime), so only the first run pays for
  transcription and iterating on scoring afterwards is fast. The deterministic strategies need
  no transcript and no Whisper model at all, so the fallback's own score can be established
  before paying for anything.

Verified end to end on real media: with `--strategy silence` the selector scores *exactly* the
same as the `longest` baseline, which is correct — that strategy **is** the segmentation
fallback — and is the harness detecting a tie it was built to detect.


### Fixed — audio was never mastered (AU1, AU2, AU8)

Verified absent across the whole repository before this: no `loudnorm`, no `dynaudnorm`, no
`sidechaincompress`, no LUFS target, no `-ar`/`-ac`. Music was mixed at a flat `volume=0.12`.

- **Loudness is normalised to the target platform's level** (`AU1`), two-pass: an analysis
  pass measures the source, then one linear gain reaches the target without touching
  dynamics. A clip quieter than the platform's target is turned *up* on playback, lifting its
  noise floor along with the speech; a louder one is turned down, losing the headroom it was
  mastered with. Targets are per platform because the platforms differ — YouTube about
  −14 LUFS, TikTok and Instagram nearer −11 — with a −1 dBTP ceiling so the lossy encoder
  has room. Failure to measure degrades to the source's own level and records
  `loudness_degraded:unmeasurable` rather than failing the clip.
- **Music ducks under speech** with `sidechaincompress` (`AU2`). A flat bed has no good
  level: loud enough to hear between sentences is loud enough to fight the speech during
  them, and quiet enough not to fight it is inaudible — no music, at the cost of an extra
  encode. `MUSIC_DUCK_RATIO=1.0` restores the flat mix.
- **Output sample rate and channel count are pinned** to 48 kHz stereo (`AU8`). Neither was
  set anywhere, so output was whatever the source happened to be: 44.1 kHz mono from a
  phone plays out of one side on some players, and a 5.1 layout is downmixed by whatever
  decoder sees it first, if at all.

### Fixed — keyword emphasis is budgeted per cue, not per clip (C11 follow-up)

- The first `C11` fix replaced an absolute confidence threshold with a salience ranking and a
  budget, but applied that budget to the whole word list a caller passed in. The strongest few
  words in a clip tend to sit near each other, so emphasis clustered: `scripts/smoke_reel.py`
  rendered a clip with **two highlights in the opening cue and none in the four after it**.
  A viewer reads one cue at a time, so "the important word" is a question about the cue in
  front of them — the budget now applies per cue, and the same clip gets one emphasis in each.
- **A one-word cue has to earn its emphasis.** A flat floor of one highlight per cue would
  emphasise every single-word cue, and rapid speech with pauses produces runs of them — which
  reinstates the original defect (everything highlighted, therefore nothing) one cue at a
  time. A number or an ALL-CAPS run still pops, because those are emphatic in themselves
  rather than by comparison; a merely long word does not.
- Grouping uses the renderer's own `words_to_cues`, so emphasis is budgeted against the cues
  actually drawn. Failure to group falls back to a single group, because `plan_keywords` is
  handed adversarial word objects by the property tests and must stay total.

### Added — approve and retry are reachable from the dashboard (PB2)

- `/api/publish-attempts/{id}/approve` and `/retry` existed with **zero references anywhere
  in `frontend/src/`**. Three of the five publishers can return `review_required` — Instagram
  and X without direct-publish approval, Whop when the upload cannot be attached to a target —
  so an attempt in that state stopped permanently and the only way out was a hand-written HTTP
  request. The history table now offers both actions.
- They stay **separate controls**, not one "resume" button. Approve escalates a held
  submission into a live post; retry re-runs it exactly as submitted, for an expired token or a
  network blip. One button would have to guess, and guessing wrong publishes something that was
  deliberately withheld. Approve is offered only for `review_required`; retry for
  `review_required` and `failed`; neither for a published or in-flight attempt, since re-posting
  a published clip duplicates it on a real account.
- 9 frontend tests cover the state gating, that each action calls only its own endpoint, error
  surfacing, and double-click protection.

### Fixed — filler removal left audible clicks (V10)

- Each kept segment now gets a few milliseconds of audio fade at its cut edges. `concat` joined
  segments sample-exactly, so every removed "um" was a step discontinuity in the waveform —
  measured on a continuous tone, the largest sample-to-sample jump at the seam drops from
  **164 to 19**.
- Deliberately **not** `acrossfade`: crossfading overlaps segments, so the result is shorter
  than the sum of its parts at every seam, and `rebase_words` maps caption timings onto the kept
  timeline using cumulative segment durations. An overlap would drift captions out of sync by a
  growing amount across the clip — a worse artefact than the click. Video cuts stay hard; a jump
  cut is normal short-form grammar.

### Added — measurement (M2, M6)

- **`scripts/smoke_reel.py`** renders one clip with every effect on, prints the effect markers
  (flagging degradations) and the before/after loudness, and lists what to look at. Explicitly
  not a test: it asserts nothing about appearance, because the defects it exists to catch —
  a wrong font, a soft emoji, a drone instead of music — are only visible to a person.
- **A loudness gate on rendered output** (`M6`): renders through the real compositor and fails
  if the result is more than 1.5 LU from its platform target, in both directions.

### Added — clips are checked before they are uploaded (O10)

- **The only pre-flight in the publish path was `video_path.exists()`.** Nothing checked
  aspect, duration, resolution, file size, codec or frame rate, so a clip a platform would
  refuse was discovered *by uploading it*: the failure arrived as whatever that platform's
  API chose to say, after spending an upload attempt and a rate-limit slot. `publishers/
  preflight.py` now validates against per-platform limits first, and a rejected clip never
  reaches the publisher.
- **Errors block, warnings do not.** A wrong codec or an over-limit duration should not cost
  an upload attempt to discover. A landscape clip going somewhere that prefers 9:16 will
  publish letterboxed, and that is the user's call — blocking it would be us overruling them
  on taste.
- The limits are deliberately looser than the strictest figure each platform advertises, so
  a *false* rejection is unlikely. They are approximations collected in one place, not a
  specification: platform limits change without notice and differ by account tier. Since the
  publishers have never run against a live platform (`PB1`), none of this has been confirmed
  against a real rejection — it guards against obvious mistakes, it does not certify.

### Fixed — clips no longer open on dead air (AU7)

- Leading and trailing silence is trimmed from each clip by moving the **cut points**, which
  costs no extra pass and keeps video and audio in step by construction (filtering the audio
  alone would desynchronise them). Capped at 1.25 s per edge: `silencedetect` reports a
  *pause*, and a pause at a boundary is often the breath before the first word or the beat
  after a punchline. Silence in the middle of a clip is speech rhythm and is left alone.
- Unlike `filler_removal`, this only moves boundaries and cannot cut anything out of the
  middle, which is why it is a safe default and filler removal is not.

### Fixed — a busy clip could balloon past platform size limits (O4)

- `-maxrate`/`-bufsize` VBV caps on delivered clips. `-crf` sets a *quality* target with no
  bitrate ceiling, so confetti, fast pans, grain or a gameplay scene could produce a file a
  platform rejects on upload. Off for intermediates, which would only lose quality the final
  pass could have used.


## [0.11.0] - 2026-07-29

A minor bump rather than a patch: the visible output of a default run changes. No API
signature changes, no removed fields, and every new field is additive with a default - but
a caller who relied on the shipped defaults will get different-looking clips, so it does not
belong in a patch release.

### Added — opinionated profiles (U2)

- **Four shipped profiles — "Podcast", "Gaming", "Talking head", "Educational"** — each a
  coherent bundle rather than a label. The settings panel exposes thirteen independent
  toggles and no opinion about which combinations work together; a user editing a podcast
  has to already know that a two-host shot needs speaker-aware reframing and that a slow
  zoom on top of it reads as restless. Pass `profile: "<name>"` with a process request:
  the bundle is expanded into the individual options and **anything else in the request
  overrides it**, including an explicit `false`.
- Two features the global defaults deliberately leave off are used deliberately here, which
  is the point of having profiles: `filler_removal` in Podcast and Educational, where
  unscripted speech makes it worth its cost, and `kinetic_typography_enabled` in Gaming —
  the one audience that asks for animated captions, and a reasonable place for a feature
  that takes ownership of the caption layer.
- `GET /api/profiles/builtin` returns the bundles in full, with the reasoning behind each,
  so a client can show what picking one will change. `GET /api/info` carries the names.
  Distinct from `GET /api/profiles`, which lists profiles a *user* saved.

### Changed — the synthesised music bed says what it is (A15)

- **`worker/effects/audio.py` does not play music — it synthesises a drone**, two sine tones
  with tremolo and a low-pass, identical for every clip of a given mood. Nothing recorded
  which of its two sources a clip got, so a synthesised bed was reported as `music:upbeat`,
  indistinguishable from a licensed track. `assets/music` ships empty, so in practice it was
  always the drone.
- `resolve_music_bed` now returns a `MusicBed` naming its source, and a clip using the
  fallback is marked **`music_degraded:synthesised`** alongside `music:<mood>`.
- `MUSIC_ALLOW_SYNTHESIS` (default on) lets an operator refuse the fallback entirely and get
  silence instead of a drone. Real licence-clean beds (`A14`) have not shipped.

### Changed — defaults (U1, V1, V12, T1)

- **A default run now enables the features that decide how a clip looks**: auto-reframe
  (`V1` — the default was a static centre crop that decapitated any off-centre speaker),
  Ken-Burns zoom, punch-in transitions, fades, the hook title (`V12`), the progress bar,
  emoji at `standard`, keyword highlighting, in-caption emoji and visual selection. Out of
  the box the tool used to enable only captions, 9:16, the `ai` strategy and metadata, so it
  shipped looking worse than it is capable of and every feature had to be found one checkbox
  at a time.
- Keyword highlighting is only a sane default because of the `C11` fix below; before it, the
  rule emphasised nearly every word.
- **Four features stay off deliberately**, each because enabling it today would make output
  worse rather than better: background music (`A14` — `audio.py` synthesises two sine waves
  per mood and `assets/music` is empty, so it would add a drone), b-roll (`A18`/`A21` — the
  library is empty, so it would only add degradation markers), kinetic typography (an AV
  engine that takes ownership of the caption layer, which belongs to a profile rather than
  the global default) and filler removal — which, unlike the rest, removes *content*, and
  cuts hard on sparse-speech footage: a 3.0 s fixture came out at 1.33 s.
- `WHISPER_MODEL` defaults to `small`, not `base` (`T1`). A mis-transcribed word is burned
  into the video, and `base` is a noticeable accuracy step down.

### Fixed — captions rendered in the wrong font, at the wrong weight (C1-C5, C7, C8, C11)

- **The fallback for an unavailable font was `Arial` — the font every preset requested, and
  one installed on no Linux host.** The substitution branch replaced a missing font with the
  same missing font, reported `font_substituted:Arial` naming the font that had *failed*, and
  left libass to metric-alias to whatever the host had, with synthesised bold. Confirmed by
  reading libass' own resolution: `fontselect: (Anton, 700, 0) -> NotoSans-Bold.ttf`.
  `captions.FALLBACK_FONTS` is now an ordered ladder of faces that verifiably resolve, and
  the marker names the font actually used — which is what `worker/models.py` always
  documented and the code never did.
- `subtitles_filter` passes `fontsdir`, so bundled faces work with no system install at all.
- **A heavy face is no longer asked to be bold on top of itself** (`C3`). ASS has one bold
  flag, libass turns it into a request for weight 700, and when the face cannot supply it
  libass *synthesises* the emboldening — thickening a face already drawn heavy. Both the
  broken and fixed versions resolve to the same file, so this is asserted on the requested
  weight, not the resolved font.
- Cues group at 3 words with sizes raised to match (`C5`); presets can ask for upper-case
  (`C7`) and set their own outline and shadow (`C8`), replacing values inferred from the
  animation style that were near-invisible at 1080x1920.
- **Keyword highlighting fired on almost everything** (`C11`), which is visually identical to
  highlighting nothing: the rule treated Whisper probability >= 0.9 as evidence a word
  mattered, and `_word_probability` returns 1.0 for a word carrying no probability at all —
  so a transcript without per-word confidence emphasised *every* non-stopword. Emphasis is
  now a salience ranking with a budget.
- The karaoke fill swept to pure green, the ASS default rather than a choice, and disagreed
  with the preset path (`C4`). Both now share one emphasis colour.

### Fixed — declared assets that were never shipped (A1-A3, A6-A8, A10)

- **12 caption faces vendored** with their licences and a manifest (`assets/fonts.json`).
- **The emoji set is vendored** (`A7`). `.gitignore` claimed "Emoji assets are downloaded at
  build time" and nothing did that, so `assets/emoji` was empty and every render either made
  a per-clip HTTP request or silently dropped the overlay. `scripts/fetch_emoji.py` is that
  build step; CI fails if a glyph is missing.
- Emoji artwork is Noto Emoji 512px rather than Twemoji 72px (`A6`), which was a 2.1x
  upscale at the size the overlay renders. `emoji_allow_download` now defaults off.
- **Emoji were sized against a hard-coded 1080** (`A8`) while overlay *placement* used
  ffmpeg's real `W`, so on any other width the two disagreed about the frame.
- **Inflected speech now matches the keyword map** (`A10`): `winning`, `wins`, `won` and
  `fired` all missed while `win` hit.

### Fixed — delivered files some platforms refused to decode (O1, O2, O3)

- `-pix_fmt yuv420p`, `-profile:v high -level 4.0` and constant frame rate are now applied
  through one shared builder instead of seven hand-written argument lists. Without them a
  4:2:2 or 10-bit source produced a file that plays locally and is refused by Safari, many
  Android decoders and several upload pipelines — a failure that only appears at upload
  time. Verified by probing the output of a real 4:2:2 15 fps source.

### Added — tests that assert resolved values (M7)

- `tests/test_fonts_real_binary.py` checks the font libass *resolved* and the weight it
  requested, by parsing libass' own output — an independent mechanism, in the spirit of
  `tests/test_capabilities_real_binary.py`. Reintroducing the font defect fails four of them.
- `tests/test_output_compat.py` asserts the probed pixel format, profile, level and frame
  rate of a delivered file, plus a control proving the flags are what made the difference.
- `.kiro/steering/working-agreement.md` records the gates and the "assert the resolved value,
  not the requested one" rule that both files exist to enforce.


### Fixed
- **`.env.example` had drifted from the code.** `config.Settings` points at it for "the full
  list", but it documented 67 of 93 settings and carried one key —
  `PUBLISH_DEFAULT_INTERVAL_SECONDS` — that no longer exists. Since `Settings` uses
  `extra="ignore"`, setting that key was accepted and discarded, so it read as a working
  control that did nothing. All 93 are now documented and
  `tests/test_config_documentation.py` fails on drift in either direction.
- `render.yaml` set `ENVIRONMENT=production` but never `CORS_ORIGINS`, so a Render deploy ran
  the `*` wildcard in production — which also disables credentialed cross-origin requests. The
  blueprint now asks for an explicit origin.
- `test_visual_selection_leaves_no_keyframe_temp_directory` is gated on ffmpeg. It drives the
  real `select_moments_visual`, whose transcript-free path probes the source before sampling;
  without the binary that probe fails, sampling is never reached, and the test's own guard
  correctly reported that it proved nothing.

### Planned
- RQ-backed distributed worker (currently in-process, and **not** yet wired up — see the note
  under 0.9.0). `redis` and `rq` are declared dependencies but no code imports them.
- Adopt ruff's `UP` (pyupgrade, ~450 findings) and `B` (bugbear, ~30) rule sets; each is a
  mechanical sweep of its own.
- Enforce formatting. `black` is a dev dependency but has never run, so adopting it would
  reformat essentially every file.

## [0.10.0] - 2026-07-29

Reliability and tooling hardening. No feature work; every item below is a defect found by
running the application and its tooling for real rather than by reading it.

### Added
- **`POST /api/publish-attempts/{id}/approve` and `/retry`.** `review_required` was a
  reachable dead end: `instagram.py`, `whop.py` and `x.py` all return it — X's own message
  reads "approve review before posting" — but no route could move an attempt out of that
  state, so such posts stopped permanently. `approve` rewrites the stored request to
  `mode="auto"`; `retry` preserves the mode so a review submission is never silently
  escalated into a live post. Approving a platform that lacks direct-publish permission is
  refused with a 409 carrying the platform's own explanation, because re-queueing it would
  simply reproduce `review_required` on the next tick — an invisible infinite bounce.
- **Durable job state** (`worker/job_persistence.py`, `JOBS_DB`). The job store was process
  memory only, so any restart discarded every job while the clips stayed on disk and in the
  publish history — which is why the history view listed clips whose downloads 404'd. Jobs
  are now written through to SQLite on every mutation. A job stored as `queued`/`processing`
  is resolved to `failed` on load, since no worker thread exists to advance it and a
  perpetually spinning progress bar is worse than an honest failure.
- **Upload validation.** `POST /api/upload` accepted any file of any size. There is now an
  extension allow-list (400), a size ceiling enforced while writing rather than trusting
  `Content-Length` (413), an empty-file check, and deletion of partial writes. A rejected
  file in a batch rolls the whole request back, so no orphaned uploads are left behind.
- **`requirements-ml.txt`** plus `--build-arg INSTALL_ML=true`, making real stem separation an
  explicit opt-in. `torch`/`demucs` were absent from `requirements.txt`, the Dockerfile *and*
  `render.yaml`, so every deploy silently took the crude ffmpeg approximation with no
  documented way to enable the real path.
- **`pyproject.toml`** with pytest and ruff configuration; see below.
- **Frontend lint and tests.** `npm run lint` previously failed outright — the script existed
  but eslint was not a dependency and no config file was present. Added a flat eslint config
  and 24 vitest tests covering the API client's URL/error handling and `Dropdown`.
- **Real-binary capability tests** (`tests/test_capabilities_real_binary.py`), which
  cross-check the probe against `ffmpeg -h filter=<name>` — an independent mechanism that
  shares no parsing code with the `-filters` table. Verified to fail on the bug they guard.
- `diarization_handoff_gap`, `visual_selection_weight`, `disk_usage_cache_seconds`,
  `ffmpeg_timeout_seconds`, `ffprobe_timeout_seconds`, `max_upload_bytes`,
  `max_persisted_jobs` settings.

### Fixed
- **No ffmpeg call outside the stem engine had a timeout.** `worker/ffmpeg_utils._run` called
  `subprocess.run` with no `timeout`, so every render, extract, thumbnail and remux was
  unbounded. Because jobs run in a thread pool with a single worker, one hung ffmpeg blocked
  the entire queue forever — silently, since a stalled process yields neither output nor an
  exception. Bounded now, with the ceiling chosen by binary (probe vs encode) and `0` as a
  documented opt-out.
- **Diarisation invented speakers who never spoke.** Label assignment advanced a round-robin
  on every silence longer than `pause_gap` (0.9s). Pauses just over that are routine inside
  one person's speech, so a monologue was reported as two speakers and speaker-aware reframe
  cut back and forth between two "speakers" who were the same person. Ending a turn and
  changing speaker are now separate thresholds, and attribution is biased toward keeping
  words with the current speaker.
- **Per-publisher rate limits were dead code.** The scheduler applied
  `max(publisher.min_interval_seconds, publish_default_interval_seconds)` with the latter
  defaulting to 30s, and every publisher declares 2–18s — so all of them were overridden and
  publishing ran roughly twice as slowly as intended. The setting is now
  `publish_min_interval_floor_seconds`, defaulting to 0.
- **`sample_keyframes` leaked a temp directory per run.** It created its scratch directory
  with `mkdtemp` and nothing ever deleted it, leaving a `kf-*` directory of JPEGs in the
  system temp space on every visual-selection run — unbounded growth, and outside the
  retention sweeper's remit.
- **`/api/storage` walked the whole storage tree on every poll**, and the storage panel polls.
  Area sizes are now cached briefly and computed with `os.scandir` instead of
  `rglob` + `stat`; the cleanup endpoint passes `refresh=True` so it cannot report
  pre-cleanup totals. Volume free/total figures are never cached.
- **CORS advertised credentials it could not deliver.** `allow_credentials=True` was hard-coded
  while `cors_origins` defaulted to `*`; the CORS specification forbids that combination and
  browsers reject the response, so the default configuration broke every credentialed
  cross-origin request while appearing to permit it. Credentials are now derived from the
  origin list, and a wildcard on a non-development environment logs a warning.
- The visual/transcript blend weight was effectively hard-coded: `merge_scores` took a
  `weight` argument that its only call site never passed. Now `visual_selection_weight`,
  clamped to `[0, 1]`.
- Nine unclosed-file leaks in the test suite, surfaced by making warnings errors.
- 14 unused imports and 27 unsorted import blocks.

### Changed
- **CI is now able to fail.** `ruff check . || true` could not, and with no `[tool.ruff]`
  config the enforced rule set was whatever the installed ruff version defaulted to — 857
  findings on 0.16 versus a handful on older releases. The rule set is pinned and the step
  blocks. The suite must also run clean of *skips*: a skipped test is not a passing test, and
  that is how the earlier missing-ffmpeg gap went unnoticed. The frontend job now lints and
  tests as well as building, and uses `npm ci`.
- **The deploy job's secret checks never worked.** `if: ${{ secrets.X != '' }}` does not
  evaluate as intended, because the `secrets` context is not resolvable inside an `if`
  expression. The values are surfaced as job-level `env` and tested via `env.*`.
- `pytest` treats warnings as errors, with `--strict-markers`/`--strict-config`.
- `api/main.py` uses a lifespan handler instead of the deprecated `@app.on_event("startup")`.

## [0.9.0] - 2026-07-29

### Added
- **Stem-aware audio repair (`stem_inpainting` engine, default OFF).** An AUDIO-stage AV engine
  that separates clip audio into a `vocals`/`music`/`other` Stem_Set, applies per-stem gains, and
  repairs the waveform joins that filler-word removal leaves behind.
  - Two separator backends behind one file-based protocol: a local `demucs` checkpoint (`ml`),
    and a dependency-free ffmpeg approximation (`music := clip - vocals`) that is only ever
    reached carrying a `degraded:` marker so it is never mistaken for real separation.
  - Seam repair as an equal-power V-notch, `sin(PI/2*|t-c|/h)`, evaluated **per sample**. Fixed
    at exactly two media passes per clip regardless of seam count: extract (`-vn`) and remux
    (`-c:v copy`).
  - Mix presets `speech_focus` / `music_focus` / `clean_speech`, plus `custom` gains over
    `0.0-4.0`; repair modes `off` / `crossfade` / `spectral`; optional declick and retained
    per-stem WAVs as durable artifacts.
  - Eleven new `ProcessingOptions` fields (`stem_inpainting_enabled` plus ten `stem_*`),
    accepted by `OptionsModel` and `/api/upload`, advertised under
    `/api/info` → `capabilities.stem_inpainting`, and exposed as a "Stem repair" group in the
    settings panel. `spectral` is shown disabled with a "needs local model" hint when
    `model:htdemucs` is unavailable.
- `demucs` and `torch` remain **optional**: they are not in `requirements.txt`, and a stock
  install runs the engine via the ffmpeg approximation.

### Changed
- **`Engine_Host` now adopts replacement media from a `degraded` engine**, not only an `applied`
  one (`_MEDIA_BEARING_STATUSES`). Degradation describes fidelity, not usability — an engine that
  fell back and still produced a usable file has produced usable output. Requirement 8.3 is
  unchanged, because it is carried by `media is None` rather than by status.
- `Engine_Host.run_stage` gained an additive, keyword-only `notes` parameter for caller-supplied
  Engine_Context notes. Existing call sites are unaffected.
- `Dropdown.jsx` supports per-option and whole-control `disabled`, so a mode that exists but is
  unavailable can be shown with its reason instead of hidden.
- CI installs `libgl1`/`libglib2.0-0`. `opencv-python` was already installed but could not be
  imported without them, so the vision code paths were never actually loaded in CI.

### Fixed
- **The `ffmpeg_filter:` capability probe could not see 124 of ffmpeg's 486 filters**, so every
  engine requiring one of them reported `unavailable` on every host regardless of how ffmpeg was
  built. `ffmpeg -filters` prints a three-character flags column per row (`T..`, `..C`), and the
  parser identified it with `not parts[0].isalnum()`. A filter with *every* flag set prints a
  dot-free group (`TSC highpass`), which that test rejects, so the row fell through to a
  bare-name branch and recorded `"TSC"` as the filter name while losing `highpass`. Affected
  `highpass`, `lowpass`, `bass`, `treble`, `equalizer`, `afftdn`, `arnndn` and 117 more. In
  practice this made the `stem_inpainting` ffmpeg backend permanently unreachable, since it
  requires `highpass` and `lowpass`. The flags column is now recognised by its alphabet, and rows
  are only accepted when the pad-spec column (`A->A`) is present. Every canned test listing had
  used dot-bearing flag groups only, which is why the whole suite passed against a feature that
  could not run.
- **Seam repair never applied.** The repair filter was specified and implemented as `volume` with
  `eval=frame` and a time-dependent expression. Against ffmpeg 7.x that is a silent no-op — `t`
  does not take the values a per-frame evaluation implies, so a `between(t,…)`-gated expression
  never fires, and the output is byte-identical to the input with no error. Replaced with
  `aeval`, which evaluates per sample.
- **Separated stems failed their own integrity check.** They were verified against the clip
  container duration, but a lossy audio stream carries encoder padding (2.000 s of AAC decodes to
  ~2.020 s of PCM), so every real separation was rejected. Stems are now checked against the
  decoded audio they were separated from.
- **Repaired clips grew by ~20 ms per pass** as that same padding compounded through
  extract + re-encode. The remux is now bounded with `-t` taken from the original clip's audio
  stream duration. (Deliberately not `-shortest`, which truncates to whichever stream happens to
  be shorter — an input-dependent change rather than a measured one.)
- `tests/test_engine_host.py` no longer leaves the `worker.engines` process globals cleared. It
  cleared the default registry and `MODEL_LOCATORS` for isolation without restoring them, and
  because `loader.py` populates those by import side effect they could not be repopulated —
  making later test files depend on pytest's file ordering.

## [0.8.0] - 2026-07-23

### Added — Speaker Diarisation & Multi-Speaker Reframe
- **Speaker diarisation** (`worker/diarization.py`): segments a source into
  ordered, non-overlapping `Speaker_Turn`s from the offline Whisper word
  timeline — **CPU-only, no GPU, no network**. An optional dependency-injected
  diarisation backend is supported but never required; it degrades to
  word-timeline segmentation on absence/error. Diarisation runs **once per
  source** and is capped at a configurable max-speakers (default 2).
- **Speaker-aware reframe** (`worker/effects/reframe.py`): multi-face detection
  + face-track grouping + a face↔speaker associator drive two output layouts —
  **follow-active** (a single dynamic crop that glides to whoever is speaking)
  and **split-screen** (a 2-up composite of the most-talkative speakers) — with
  **subtle / standard / heavy** smoothing and smooth transitions on speaker
  change. All geometry is applied in the **existing single ffmpeg pass**.
- **Graceful degradation**: an explicit precedence ladder — speaker-aware
  reframe → the existing single-speaker reframe → the static blurred reformat —
  guarantees a clip is always produced, recording the fallback in
  `effects_applied` (`speaker_reframe:<layout>`, `speaker_reframe_degraded`,
  `speaker_reframe_substituted`, `diarization:transcript`/`:model`/`_degraded`).
- **Permissibility-aware**: under permissibility mode diarisation uses only the
  offline word timeline (any external backend is bypassed) and no network call
  occurs.
- **API + Web UI**: `/api/info` advertises `reframe_layouts` and
  `reframe_intensities`; `POST /api/upload` accepts `diarization`,
  `speaker_reframe`, `reframe_layout`, and `reframe_intensity`; the settings
  panel gains Speaker-aware-reframe + Diarisation toggles and Reframe layout /
  intensity dropdowns.

### Changed
- The pipeline geometry stage now routes through the speaker-aware precedence
  ladder; when both new toggles are off it takes the exact v0.7.0 path.

### Notes
- **Every new capability defaults OFF** — an "all-off" run reproduces v0.7.0
  output and `effects_applied` exactly. Transcript-first diarisation is a
  CPU-only heuristic best suited to turn-based interviews/podcasts; an acoustic
  BYOK backend can be injected without other changes. Enabling speaker-aware
  reframe adds roughly 1.0–1.1x the single-speaker reframe render time
  (follow-active) or ~1.1–1.3x (split-screen); disabled it adds zero cost.

## [0.7.0] - 2026-07-23

### Added — Tier 1 Creator Output Upgrade
- **Animated caption presets** (`worker/effects/caption_presets.py` +
  `worker/captions.py`): a serializable `CaptionPreset` model and registry
  covering the three legacy templates (karaoke / boxed / minimal) plus new
  animated presets — **pop**, **typewriter**, and **hormozi** — rendered purely
  with **libass ASS tags** (no `drawtext`). Per-word animation is anchored and
  time-bounded to each spoken word.
- **Keyword highlighting**: a deterministic keyword planner (stopword / length /
  ALL-CAPS / numeral / high-confidence rules) with an optional **AI
  (context-aware) mode** that only ever *extends* the deterministic set;
  highlighted words get a distinct colour/scale while their timing is preserved.
  Optional **in-caption emoji** rendered inline (independent of the overlay
  emoji effect).
- **B-roll auto-insertion** (`worker/effects/broll.py`): a pure cue planner
  (`plan_broll_cues`) bounded by an **intensity** cap (off / subtle / standard /
  heavy) on both count and total on-screen time, plus a provider layer —
  **LocalProvider** (from `broll_dir`, no network) and an optional BYOK
  **ExternalProvider** (injectable downloader, records
  provider/source_id/license/attribution). `asset_sourcing_mode`
  (off / local_only / local_then_external) governs sourcing; unknown-license and
  failed assets are dropped. Overlays composite **below captions** in the
  existing single ffmpeg pass, and only composited assets are recorded on
  `ClipResult.broll_assets`.
- **Prompt / visual clip finding** (`worker/visual_selection.py`): an optional
  **selection prompt** plus cheap **CPU-only** keyframe sampling (bounded,
  once-per-source) and brightness/motion proxies merged with the transcript
  ranking; degrades cleanly to transcript-only selection when sampling fails, no
  provider/LLM is configured, or the feature is off.
- **Permissibility mode**: a single toggle that forces `asset_sourcing_mode` to
  **local_only**, disables added music, and blocks any external download — for
  music/sourcing-sensitive workflows.
- **API**: `/api/info` now advertises `caption_presets`, `caption_animations`,
  `asset_sourcing_modes`, `broll_intensities`, `broll_providers`, and
  `broll_available`; `POST /api/upload` accepts the twelve new option fields.
- **Web UI**: the settings panel gains a caption-preset dropdown, keyword /
  AI-highlight / in-caption-emoji toggles, a b-roll section (enable, intensity,
  sourcing mode, provider), a selection-prompt textarea + visual-selection
  toggle, and a permissibility-mode toggle.

### Changed
- Clip selection now routes through `select_moments_visual`, which delegates
  back to the v0.6.0 transcript-only selector whenever visual selection is off
  or degraded.

### Notes
- **Every new capability defaults OFF** — an "all-new-options-off" run
  reproduces v0.6.0 output and `effects_applied` exactly. No external network is
  used unless BYOK external b-roll is explicitly enabled and configured.

## [0.6.0] - 2026-07-23

### Added — Phase 5: storage, settings profiles & updates
- **Storage backends implemented** behind one interface (`storage_backends/`):
  a full `LocalStorage` and `S3Storage` (`save`/`open`/`url`/`delete`/`exists`/
  `list`/`size`, presigned URLs, injectable boto3 client) selected by
  `STORAGE_BACKEND` — **the code path is identical for local and S3**.
- **Retention & cleanup** (`storage_backends/retention.py`): a user-exposed
  retention window — **7 / 14 / 30 / 60 / 90 days** or **Keep forever** (default
  **30**), enforced by a background sweeper that **never touches source video**;
  plus `disk_usage()` (with a low-space warning) and manual "clean up now".
- **Temp auto-delete** toggle (removes a job's scratch files when it finishes)
  and a **delete-local-copy-after-publishing** toggle (guarded to clip files;
  never the source).
- **Sidecar metadata**: a `<clip>.json` capturing title/description/hashtags/
  effects is written next to every clip (and mirrored to the backend).
- **Protected source deletion**: original source video is only ever removed via
  an explicit, confirmed `DELETE /api/jobs/{id}/source?confirm=true`.
- **Runtime-mutable settings** (`runtime_config.py`): retention window and the
  two toggles are editable from the UI and persisted to
  `storage/runtime_config.json`, layered over the `.env` defaults.
- **Saved settings profiles** (`profiles.py`): snapshot the full configuration
  (clip length, aspect, caption style, effects, publishing targets) as a named
  profile; multiple profiles, quick-switch, edit/delete, and a **default**
  profile that pre-fills settings on load.
- **Update checking** (`updates.py`): compares the `VERSION` file to the latest
  GitHub release (cached, failure-tolerant) and drives an **"update available"**
  banner in the UI.
- **API**: `GET/POST /api/storage`, `/api/storage/settings`, `/api/storage/cleanup`,
  `DELETE /api/jobs/{id}/source`, `GET/POST /api/profiles`,
  `POST /api/profiles/{id}/default`, `DELETE /api/profiles/{id}`, `GET /api/updates`;
  the app version now comes from the `VERSION` file and `/api/info` reports the
  storage backend + retention choices.
- **Web UI**: a **Settings** tab with a **Storage** group (disk usage meter +
  low-space warning, retention, toggles, cleanup), a **Settings profiles** bar
  (save/switch/default/delete + prefill), the running **version**, and the
  update banner.

### Changed
- Default clip retention is now **30 days** (was 7); it is adjustable at runtime.
- **CI/CD**: the workflow adds a `deploy` job that auto-deploys from `main` to
  Render/Railway via a deploy-hook secret; a `render.yaml` Blueprint is included.
- Job pipeline mirrors finished clips (+ sidecar + thumbnail) through the storage
  backend on the same code path for local and S3; `README` documents the
  one-command update (`git pull && docker compose up --build`).

## [0.5.0] - 2026-07-23

### Added — Phase 4: visual effects (all individually toggleable)
- **Easy effects** (`worker/effects/overlays.py`), composed into a single
  efficient video pass: **zoom / Ken-Burns**, **punch-in intro**, **fade in/out**
  (video + audio), **colour grade** presets (vivid / warm / cool / cinematic /
  b&w), and a growing **progress bar**.
- **Hook title overlay**: the AI-generated hook text is burned in at the start
  (rendered via libass so it needs no `drawtext`/freetype build of ffmpeg).
- **Background music** (`worker/effects/audio.py`): **mood-selectable** beds
  (upbeat / chill / dramatic / corporate / suspense). Uses your own licensed
  track from `assets/music/<mood>.*` if present, otherwise synthesises a soft,
  copyright-free ambient bed and mixes it under the speech with a configurable
  volume (and matching fades).
- **Face-tracking auto-reframe** (`worker/effects/reframe.py`): detects the main
  speaker (OpenCV Haar cascade, MediaPipe-ready), smooths the crop path
  (EMA + resample) so the "camera" glides, and applies the moving crop in one
  ffmpeg pass via `sendcmd` + `crop`. Replaces the static centre-crop when
  enabled and **degrades gracefully** to the blurred-background reformat if no
  face is found or OpenCV is unavailable.
- **Auto-emoji overlays** (`worker/effects/emoji.py`) synced to spoken words via
  the Whisper word timestamps: a built-in **keyword→emoji map** plus an optional
  **AI (context-aware) mode**, four **intensity** levels (Off / Subtle /
  Standard / Heavy), Twemoji PNGs (fetched + cached from the CDN), and an
  optional alpha **pop** animation.
- **Filler-word / dead-air removal** (`worker/effects/filler.py`): cuts
  "um"/"uh" and long pauses, then **rebases** the word timeline so captions and
  emoji stay in sync.
- **Caption Template & Position**: templates (karaoke / boxed / minimal) and
  placement (bottom / center / top), surfaced in the UI.
- **Compositor** (`worker/effects/compositor.py`): applies all enabled effects
  in a single ffmpeg pass, stream-copying any track it doesn't change and doing
  nothing (fast path) when no effect is enabled. Each clip records which effects
  were applied (shown as badges in the gallery).
- **Pipeline & API**: per-clip flow is now cut → (filler trim) → geometry
  (reframe or reformat) → single-pass compositor → thumbnail; all effect options
  are accepted by the upload / URL / batch endpoints and `/api/info` advertises
  the available moods, colour presets, emoji intensities, and caption templates.
- **UI**: a **Visual effects** settings section with every toggle, the caption
  template/position, colour grade, music mood + volume, and emoji controls.

### Changed
- `Dockerfile` installs `fonts-liberation` + `fontconfig` so burned-in
  captions/hook titles render with a metric-compatible font.

## [0.4.0] - 2026-07-23

### Added — Phase 3: auto-publishing
- **Common publisher interface** (`publishers/base.py`): a platform-neutral
  `PublishRequest` / `PublishResult` / `PublisherStatus` contract with a shared
  `BasePublisher`, so platforms plug in through one registry
  (`publishers/__init__.py`).
- **Platform adapters**, each reporting a clear configured/limited/ready status
  and degrading gracefully:
  - **Whop** (`publishers/whop.py` + `publisher_bridge/`): uploads via the
    official **`@whop/sdk`** through a Node bridge, then attaches the file to a
    **chat**, **forum**, or **course** target; uploads with no supported target
    return `review_required` for manual placement.
  - **YouTube** (`publishers/youtube.py`): Data API v3 resumable upload over the
    OAuth refresh-token flow; vertical clips publish as **Shorts** (review mode
    uploads privately).
  - **TikTok** (`publishers/tiktok.py`): Content Posting API. Uploads to the
    creator's **inbox as a draft** until Direct Post is approved
    (`TIKTOK_DIRECT_POST_APPROVED`).
  - **Instagram** (`publishers/instagram.py`): Graph API resumable **Reels**
    upload/publish; runs in review mode unless content-publish is approved.
  - **X** (`publishers/x.py`): chunked media upload + post; returns
    `review_required` unless an approved user-context token is present.
- **AI metadata on upload**: each adapter attaches the clip's generated title,
  description/caption, and hashtags automatically (per-platform limits applied).
- **Multi-channel routing** (`publishers/history.py` campaigns): tag a clip with
  a **campaign** that maps each platform to an account/target; clips route to
  the right destination.
- **Throttled scheduling** (`publishers/manager.py`): a persistent background
  scheduler posts **now or at a chosen time**, enforcing a minimum per-platform
  interval to respect rate limits.
- **Metadata download bundle**: the primary clip download now returns a **ZIP**
  containing the MP4 plus a `_metadata.txt` file with the title, caption, and
  hashtags; a raw video-only download remains available.
- **Persistent history** (SQLite): every created clip and every publish attempt
  (platform, account, time, state, link, error) is recorded and survives
  restarts.
- **API**: `GET /api/publishers`, `GET|POST /api/campaigns`,
  `POST /api/jobs/{job}/clips/{clip}/publish`, `GET /api/history`,
  `GET /api/publish-attempts/{id}`, ZIP + video-only clip downloads; upload/URL
  jobs accept `publish_to`, `campaign_id`, `publish_mode`, and `schedule_at` for
  auto-publishing on completion.
- **Web UI**: a **Publishing settings** panel (Publish To multi-select with live
  per-platform status, Campaign, Mode auto/review, Schedule, and campaign
  saving), **per-clip publish/schedule buttons** with live attempt status, the
  metadata-bundle download, and a dedicated **History** view.

### Changed
- `Dockerfile` now installs Node.js and the `publisher_bridge` dependencies so
  the Whop `@whop/sdk` bridge runs inside the container.
- All publisher secrets and scheduler tuning are read from `.env`
  (see `.env.example`); the history store persists IDs/status only — never
  tokens.

## [0.3.0] - 2026-07-23

### Added — Phase 2: smart selection & metadata
- **Pluggable LLM client** (`worker/llm_client.py`): OpenAI or Anthropic
  (key from `.env`), unified `complete` / `complete_json` interface with lenient
  JSON parsing, a `MockLLMClient`, dependency-injection override
  (`set_llm_client`), and an availability check.
- **LLM highlight selection** (`worker/selection.py`): replaces fixed-length
  cutting. Sends the transcript to the LLM to find hooks, punchlines, complete
  thoughts, and emotional peaks; returns candidates with a **virality score**
  and rationale. Honours *Clip Topic/Keywords* and *Vibe/Tone*, respects clip
  count + target length, snaps start/end to sentence boundaries, and falls back
  to deterministic segmentation when no LLM is configured.
- **AI metadata generation** (`worker/metadata.py`): per-clip title (+ 2-3
  alternatives), description/caption, hashtags (configurable count), on-screen
  hook text, CTA, @mentions, and thumbnail text idea — tone tailored **per
  platform** (YouTube / TikTok / Instagram / X / Whop / generic) with character
  and hashtag limits enforced. Individual fields can be regenerated.
- **Pipeline integration** (`worker/pipeline.py`): AI selection + per-clip
  metadata, an optional **Process Range**, and graceful fallback throughout.
- **API** (v0.3.0): extended options (topic, vibe, platform, hashtag count,
  process range, selection strategy); `PATCH /api/jobs/{job}/clips/{clip}` to
  edit clip metadata and `POST .../regenerate` to regenerate a single field;
  `/api/info` now reports platforms, strategies, and LLM availability.
- **Web UI**: an **Advanced settings** section (Clip Topic, Vibe/Tone, Process
  Range, Platform, Hashtag count, selection method) and an editable clip gallery
  showing the **virality score**, editable title (with alternative chips),
  description, hashtags, hook, CTA, and thumbnail text — each with a per-field
  **regenerate** action.

## [0.2.0] - 2026-07-23

### Added — Phase 1: core clip-generating engine
- **FFmpeg utilities** (`worker/ffmpeg_utils.py`): probe, frame-accurate
  segment cut, aspect reformat (9:16 / 1:1 / 16:9 / 4:5) with blurred-background
  fill or padding, audio extraction, and thumbnail generation.
- **Transcription** (`worker/transcribe.py`): faster-whisper with word-level
  timestamps, lazy cached model, auto CPU/GPU device selection, and translate
  mode.
- **Captions** (`worker/captions.py`): word-grouped cues rendered to styled ASS
  with karaoke-style highlighting, burned in via libass.
- **Segmentation** (`worker/segmentation.py`): fixed-length and silence-based
  chunking, with UI Clip Length / Number of Clips option mapping.
- **Ingest** (`worker/download.py`): yt-dlp URL download with progress + cheap
  metadata fetch for preview cards; URL/file classification.
- **Pipeline & jobs** (`worker/pipeline.py`, `worker/jobs.py`): end-to-end
  orchestration with live progress; in-process background job manager with a
  thread-safe store; batches processed in line.
- **Watch-folder mode** (`worker/watch_folder.py`): toggleable folder monitor
  that auto-processes dropped videos with the current settings.
- **API** (`api/main.py`): preview, single-URL, batch, and multi-file upload
  submission; job status/progress; clip listing, static preview, and download;
  watch-folder toggle. Serves the built React SPA.
- **Web UI** (`frontend/`): dark, Opus-Clip-style dashboard — URL/upload/batch
  input, preview card, settings panel (Language, Clip Length, Aspect Ratio,
  Number of Clips), a full-width green "Get Clips" button, per-video progress,
  and a clip gallery with inline preview + download.

### Changed
- Multi-stage `Dockerfile` (builds the SPA, then the Python runtime with FFmpeg).
- `docker-compose.yml` simplified to a single app service for Phase 1
  (in-process jobs); RQ + Redis reserved for a later phase.

## [0.1.0] - 2026-07-23

### Added
- Initial project scaffold (foundation only; no features implemented).
- `config.py` pydantic settings + `.env.example` covering LLM, transcription,
  storage/S3, and all publisher credentials.
- Backend package skeleton: `api/`, `worker/` (+ `worker/effects/`),
  `publishers/`, `storage_backends/` with documented stubs.
- FastAPI app that boots and serves a dark-themed placeholder page plus
  `/healthz` and `/api/info` endpoints.
- React + Tailwind dark-themed frontend skeleton.
- `docker-compose.yml` bundling the app (with FFmpeg), a worker, and Redis.
- GitHub Actions CI workflow (lint + import/boot smoke check).
