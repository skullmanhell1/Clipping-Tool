# Session Handoff

Rewritten after the reliability and tooling pass that followed the specs completing. The
previous version described the work that is now merged as "what is left", so it has been
replaced rather than amended.

## 1. Status

**All five specs are complete — 388/388 tasks, nothing open.** Version `0.10.0`.

| Spec | Tasks |
| --- | --- |
| `av-engines-foundation` | 91/91 |
| `kinetic-typography` | 76/76 |
| `tier1-creator-output-upgrade` | 69/69 |
| `speaker-diarization-reframe` | 66/66 |
| `audio-stem-inpainting` | 86/86 |

Two AV engines are registered and advertised by `/api/info`, both **default-off**:
`kinetic_typography` (COMPOSE) and `stem_inpainting` (AUDIO).

No PRs are open. [#42](https://github.com/skullmanhell1/Clipping-Tool/pull/42) (stem epics
9-20) and [#43](https://github.com/skullmanhell1/Clipping-Tool/pull/43) (reliability and
tooling) are both merged.

## 2. Test baselines

Run with `python -m pytest` from the repo root. Configuration lives in `pyproject.toml`.

| Environment | Result |
| --- | --- |
| Full `requirements-dev.txt` + ffmpeg + fonts + `libGL` (what CI and Docker have) | **724 passed, 0 failed, 0 skipped, 0 warnings** |
| No ffmpeg on `PATH` (a bare developer checkout) | **616 passed, 108 skipped, 0 failed** |

Frontend: `npm run lint` → 0 errors (2 `exhaustive-deps` warnings, see §5), `npm run test:run`
→ 24 passed, `npm run build` → OK.

Three things to know before trusting a green run:

* **A skip is not a pass.** The 108 skips are the `requires_ffmpeg` gate. CI installs ffmpeg
  and **fails if any test is skipped**, so a skip there means a dependency went missing and
  the coverage it gated has silently stopped running.
* **Warnings are errors** (`filterwarnings = ["error", ...]`). Adding a dependency that emits
  a new deprecation will fail the suite until it is triaged.
* **CI needs full git history.** `.github/workflows/ci.yml` sets `fetch-depth: 0` on the
  backend job, because two parity guards diff against `origin/main`. With the default shallow
  checkout they skip, which the no-skip gate then reports — that is how it was discovered
  they had *never* run in CI.

## 3. The most important lesson from the last pass

The `stem_inpainting` engine shipped, merged, and **could not run on any machine** — while
598 tests passed.

`ffmpeg -filters` prints a three-character flag column per row. The capability probe
identified it with `not parts[0].isalnum()`, which is true for `T..`/`..C` but **false for
`TSC`** (every flag set, so alphanumeric). Those rows fell through to a bare-name branch that
recorded `"TSC"` as the filter name and dropped the real one — hiding **124 of ffmpeg 7.0's
486 filters**, including the `highpass` and `lowpass` that the stem ffmpeg backend requires.

It was invisible because **every capability test mocked the probe**, and every canned
`-filters` fixture used dot-bearing flag groups only. `tests/test_capabilities_real_binary.py`
now cross-checks the probe against `ffmpeg -h filter=<name>`, an independent mechanism that
shares no parsing code with the `-filters` table. Reverting the fix makes 12 of those fail.

Generalise from it: **for anything that parses another program's output, test against the real
program.** Three defects in the stem repair filter itself were found the same way, by running
ffmpeg rather than reading the design.

## 4. Kinetic P12: could not be reproduced

An older handoff described `test_p12_malformed_timings_degrade_instead_of_raising` as
known-red and asked for its own bugfix spec. **It does not reproduce.**

* 4000 fresh Hypothesis examples with the example database disabled — clean.
* A deterministic sweep of 1020 cases (5 degenerate timeline shapes × 17 fps values, including
  the `14.0` the old analysis named, × 3 sample rates × 4 durations) — clean.
* The predicate that analysis blamed (`later_start > cue_start`, `worker/engines/kinetic.py`
  ~line 1639) is **still present**, so this is not a silent fix — the diagnosis was wrong.

That handoff also blamed ffmpeg being on `PATH`, which is definitely wrong: P12 is a pure
planner property with no ffmpeg dependency. The likeliest explanation is a stale counterexample
in a local `.hypothesis` database.

**Recommendation:** keep the property test as the guard, do not open the spec. Absence of a
counterexample is not proof of absence — if it reappears, capture the counterexample *and* the
generator versions before analysing.

## 5. What is actually left

No spec work. Everything below is unplanned, and none of it is a regression.

### Only you can close these — they need credentials or a real deploy

1. **Publishers are entirely unverified.** TikTok, Instagram, X, YouTube and Whop have never
   been exercised against a live platform, including the `/approve` and `/retry` endpoints.
   Their logic is covered by test doubles only. **This is the largest untested surface in the
   repository** — treat it as unproven, not as working.
2. **URL ingest is untested.** Only local files have been pushed through the pipeline; `yt-dlp`
   has never actually downloaded anything in a verified run.
3. **The Docker image has never been built end to end.** The `INSTALL_ML` shell logic and the
   frontend stage were validated in isolation; a full build was not run.

### Deliberate deferrals — each is its own change, not a loose end

4. **Formatting is not enforced.** `black` is in `requirements-dev.txt` and has never run;
   adopting it reformats essentially every file.
5. **ruff `UP` (~450 findings) and `B` (~30) are not selected.** Both are worth adopting; each
   is a mechanical sweep that should not be mixed with behavioural changes. The enforced set is
   pinned in `pyproject.toml` to `F`/`E4`/`E7`/`E9`/`I`.
6. **`redis` and `rq` are declared dependencies that no code imports.** `worker/tasks.py` is
   imported by nothing. Either wire up the distributed worker or drop them — right now the
   dependency list overstates the architecture.
7. **11 dev-only npm advisories** (`brace-expansion` via eslint, `esbuild` via vite/vitest).
   None reach the shipped bundle; clearing them needs `vite@8`, a breaking upgrade.
8. **Frontend test coverage is thin.** Only `api.js` and `Dropdown` are covered; the other ten
   components have none.
9. **Two `react-hooks/exhaustive-deps` warnings** in `App.jsx` (`jobs`) and `HistoryView.jsx`
   (`load`). Both are polling effects where naively adding the dependency causes a
   re-subscribe loop, so they need thought rather than a quick edit.

### Notes for whoever works here next

* **Node 20 is the target.** CI and the Dockerfile both use it. `frontend/package.json` declares
  `engines` and `frontend/.npmrc` sets `engine-strict=true`, so an incompatible dependency now
  fails at install time — added after `jsdom@30` (Node ≥22) was installed on a newer local
  runtime and broke CI with `webidl.util.markAsUncloneable is not a function`. `npm ci` had
  exited 0 with only warnings.
* **Job state is durable** (`storage/jobs.db`, `JOBS_DB`). A job stored as `queued`/`processing`
  is resolved to `failed` on load, because no worker thread survives a restart to advance it.
* **Real stem separation is opt-in.** `torch`/`demucs` are not in `requirements.txt`; see
  `requirements-ml.txt`. Without them the engine degrades to an ffmpeg approximation and records
  `degraded:python_pkg:demucs`. The checkpoint is a *separate* step on purpose — the engine treats
  a model that would need downloading as unavailable, so that probing a capability can never
  become a silent network fetch.
* **`.env.example` is a contract**, not a sample: `config.Settings` points at it for the full
  list, and `tests/test_config_documentation.py` fails if a setting is undocumented or a
  documented key is not a real setting. It had drifted to 67 of 93, with one stale key that
  silently did nothing.
