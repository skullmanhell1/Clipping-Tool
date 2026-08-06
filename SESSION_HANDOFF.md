# Session Handoff

Current as of `VERSION` **0.11.0**, with `main` at PR **#83**.

Maintained on the principle that a handoff document which is wrong is worse than none — so where a
section has stopped being true it is **replaced**, not amended, and the correction says what it used
to claim. §1 has been through that twice now: it once described five completed specs and "no PRs are
open", then described a Phase 1–4 stack stranded off `main`. Both were true when written. Neither
is now.

## Start here

**1. `main` has the work. Branch off it.** See [§1](#1-where-the-work-is). The Phase 1–4 pass is
merged; there is nothing stranded and nothing to check before starting.

The previous version of this section gave a `git cat-file` probe to prove `main` might be behind.
It has been removed rather than updated, because it could only ever return one answer now, and a
check that cannot fail teaches the reader to skip checks.

**2. Then read `docs/IMPROVEMENT_PLAN.md`.** It is the prioritised backlog — 154 numbered items with
a priority and effort estimate each. **141 are now implemented**, and the 13 that remain are listed
in [§3](#3-what-is-actually-left); most are blocked on something other than effort.

> Read the plan as an **audit of v0.10.0**, which is what it says it is. Its "Current:" lines and
> "current value" tables describe 0.10.0 and most are now wrong — §6 in particular now asserts the
> opposite of the truth about loudness. The *item tables* are the backlog; the prose around them is
> history, and each affected section carries a marker saying so.

> If you recount these by grepping the codebase for item IDs, note two traps that produced wrong
> figures once already. `P0`–`P3` are *phase* rows, not items, and must be excluded — but excluding
> everything starting with `P` also silently drops `PB1`–`PB9`. And an item can be implemented
> without carrying its own ID: `PB3` is satisfied by `publishers/preflight.py`, which is labelled
> `O10` because the plan entry says "see O10". Grep undercounts; check before believing it.

**3. Before changing anything with a measurable output, read
[§5](#5-conventions-that-are-not-obvious-from-the-code).** Several conventions here are deliberate
and will look like mistakes: every new visual setting defaults *off*, some tests exist purely to fail
when a constant changes, and one module reports "I cannot do this" rather than doing it badly.

## 1. Where the work is

**On `main`. All of it.** Version `0.11.0`.

This section used to say the Phase 1–4 pass had been merged "into **each other**, not into
`main`", and named `integrate/main-sync` as the branch that would carry it across. That was true
when it was written and is not true now: **PR #76 merged `integrate/main-sync`**, and `main` has
advanced well past it — PR **#83** (`spec/face-detection-upgrade`) and several others have landed
since. None of the `integrate/*` or `feat/*` branches described here still exist.

The rewrite is deliberate rather than an amendment. This document opens by saying a handoff that
is wrong is worse than none, and the old §1 was the most costly kind of wrong: it instructed the
reader to redo completed work.

**What this means in practice:** branch off `main`, and trust it. The historical PR table has been
removed — `CHANGELOG.md` is the record of what landed, and it is maintained; a second, staler copy
of the same history in here was only ever going to drift again.

Note that the sandbox cannot `git push` or `git fetch` with ordinary credentials — only the GitHub
tooling authenticates. That part is still true, and still the thing that surprises people.

## 2. Test baselines

Do not let these go down. A drop means something stopped running, which is worse than a failure
because it looks like success.

| Gate | Expected |
| --- | --- |
| `pytest` | **2076 passed, 0 skipped, 0 warnings** |
| `mypy .` | clean (99 modules) |
| `npm run test:run` | **141 passed** |
| `ruff check .` | clean |
| `python scripts/fetch_emoji.py --check` | `all 326 noto emoji vendored` |
| `scripts/docker_smoke.sh` | builds and serves; image ~1.48 GB |
| `npm audit --audit-level=critical` | exits 0 |

**Warnings are errors** and **a skipped test fails CI** — both deliberate, both explained in the
README's Testing section. The full backend suite takes about five to six minutes.

**Run `scripts/setup_dev_env.sh` first.** Without ffmpeg the suite does not fail, it *skips* — and
a skip is what the gate below exists to catch.

**The no-skip gate reads the JUnit XML**, not pytest's summary text. Same rule — any skip fails the
build — but it no longer depends on prose pytest is free to reword, and it also catches
`xfail`/`xpass`.

**`mypy` is blocking.** It had never been run before; the first run found 69 errors in 17 files and
all of them are now fixed or covered by one of two narrow per-module overrides in `pyproject.toml`,
each carrying its reason. `disallow_untyped_defs` is ratcheted on `worker/models.py`,
`worker/selection.py`, `worker/candidate_ranking.py`, `publishers/base.py` and
`storage_backends/base.py`.

### Coverage

Measured and **reported**, not gated — a threshold picked before anyone has seen the number either
passes by accident or blocks the first honest measurement.

| Surface | Coverage |
| --- | --- |
| Backend | **90%** (13,213 statements) |
| Frontend | **36.81%** statements (1,438 / 3,906) |

The frontend figure is the honest headline: `App.jsx` (614 lines) and six components including
`SettingsPanel.jsx` (969 lines) have no tests at all.

**Do not add `--cov` to `addopts`.** Coverage instrumentation changes garbage-collection timing,
which is enough to move an unclosed-socket `ResourceWarning` raised inside yt-dlp out of teardown
and into the middle of a test — where `filterwarnings = error` correctly fails it. CI runs coverage
as a separate pass so the run that gates the build is the same one you run locally.

**Do not install `pip-audit` (or anything else pulling `requests`) into the test venv.** yt-dlp
prefers its `requests`-based request handler when the package is importable, and that handler leaks
a socket. Measured: with `requests` present, 3–4 of the `tests/test_url_ingest.py` I13 tests fail
non-deterministically under `--cov`; uninstalling it makes the identical run green. CI audits in an
isolated venv for this reason, and `requirements-dev.txt` records it.

### npm advisories: the 9 high findings are gone

This section previously recorded **9 high advisories** as a deliberate decision — a
`brace-expansion` DoS reachable only through eslint's own `minimatch` chain, where
`npm audit fix --force` *downgraded* `eslint-plugin-react` to 7.22.0 and pinning
`brace-expansion` broke `minimatch@3` so eslint crashed outright.

**`npm audit` now reports `found 0 vulnerabilities`** (verified on the committed lockfile). The
chain was resolved upstream. The gate stays at `--audit-level=critical`: the reasoning for *why*
it is set there is still sound, and lowering it now would be gambling that the next advisory is
also fixable without breaking the build.

## 3. What is actually left

13 items. Only two are a matter of effort.

**This list is authoritative; the count is not.** 154 − 141 = 13, but the total depends on a
judgement: `M4`/`S1` are one gating item carrying two IDs, and `S16`/`S18` are consequences of it
rather than independent work. `docs/IMPROVEMENT_PLAN.md` once said "14 remain" while this section
said 13, purely because the plan's header had not yet counted `U4` as done. If you need to know
what is left, read the list below rather than reconciling the arithmetic.

**`U4` (transcript-based trimming) is done** — see the CHANGELOG's Unreleased section. It is worth
knowing where the seams ended up, because the next person to touch trimming will meet them:
`worker/transcript_trim.py` owns the geometry (cuts in, keeps out) and does no I/O;
`worker/clip_transcript.py` recovers a clip's words from the T8 cache and **never runs ASR**;
`ClipCandidate.cuts` carries the list; and the render still goes through
`filler.apply_keep_intervals`, so there is exactly one multi-range concat in the worker. Filler
removal and a cut list compose by union into **one** keep list and **one** re-encode — do not add a
second pass.

### Buildable now

| Item | What | Why it was left |
| --- | --- | --- |
| **U12** | Multi-user auth and per-user storage | Single-tenant today. A product decision as much as a technical one. |
| **I9** | Adopt `black`, plus ruff `UP` (~450 findings) and `B` (~30) | **Own branch, nothing else in flight.** It touches nearly every file. The "after the chain merges / conflicts with all four open PRs" note that used to be here is obsolete — the chain merged at PR #76 and those PRs are closed — but the underlying advice stands for the ordinary reason: do not mix a mechanical sweep with a behavioural change. `black --check` has never run; `ci.yml` documents the waiver. |

### Blocked on model weights CI cannot have

`S5` laughter/applause detection (YAMNet) · `S13` real vision signals · `T2` forced alignment
(wav2vec2) · `T6` pyannote diarisation · `V3` active-speaker detection · `V7` subject detection ·
`AU6` demucs source separation · `I2` GPU support.

Each of these already has the *seam* built — an injectable backend and a degraded fallback that is
labelled as degraded. What is missing is a checkpoint file and a runtime dependency, and the
no-skips rule means a test needing either cannot be added. `requirements-ml.txt` and the
`INSTALL_ML=true` build arg exist for whoever has the hardware.

> The `INSTALL_ML=true` image has **not** been built. Only the default path is verified.

### Blocked on credentials or API access

- **PB9** — more publishing destinations (LinkedIn, Facebook Reels, Snapchat, Threads). P3/L. Each
  needs an app registration and review before a single line can be tested against anything real. The
  `publishers/base.py` interface plus `publishers/preflight.py` is the seam; adding a class is the
  small part.
- **PB1** is *implemented* but cannot be exercised here — it needs live publisher credentials. The
  same applies to verifying any real upload.

### Blocked on data that does not exist yet

- **M4 / S1** — the labelled selection benchmark. **This is the gating item for all selection
  quality work.** Do not tune ranking before it exists, or improvements and regressions are
  indistinguishable.
- **S16** — recording published-clip performance. Needs `PB8` and real posted clips.
- **S18** — calibrating the virality score. Depends on M4; today it is an LLM's unanchored 0–100
  opinion and is documented as a shortlist rather than a verdict.

## 4. Environment

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
bash scripts/setup_dev_env.sh    # ffmpeg, Liberation fonts, the opencv runtime libs
```

**Run `scripts/setup_dev_env.sh` before trusting a green suite**, and re-run it if ffmpeg
disappears from `PATH` — which happens on a sandbox recycle, since only the workspace survives. It
prints what it found, so a partial setup is visible immediately rather than surfacing later as a
skip. This matters more than it looks: the suite has no skips by design, and an earlier ffmpeg gap
went unnoticed for several releases precisely because ~90 tests quietly stopped running while CI
reported green.

- The venv is at `.venv/` (Python 3.11). Use `.venv/bin/python`, not bare `python`.
- **`/tmp` does not persist between separate shell invocations** in the sandbox. Stage anything that
  must survive somewhere under the workspace.
- The sandbox has **open internet** and **docker**. That is what made `I12` and `I13` verifiable at
  all; earlier sessions could not run either.
- `git fetch origin` fails with `Missing header field, please provide AuthToken`. Only the GitHub
  tooling authenticates — use it for push and PR operations, never bare `git push`.

## 5. Conventions that are not obvious from the code

These cost real time to rediscover. Each one is deliberate.

**Every new visual or output setting defaults to previously shipped behaviour.** `ZOOM_EASE=false`,
`CAPTION_AUTO_CONTRAST=false`, `EMOJI_PLACEMENT=spread`, `BROLL_KEN_BURNS=false`, `BROLL_DUCK=0.0`,
`SFX_MODE=off`, `CAPTION_AVOID_FACES=false`, `VIDEO_ENCODER=libx264`, `SUBTITLE_TRANSLATION=false`.
The alternative — defaulting a feature on and re-freezing the parity goldens — means re-freezing them
every release, which stops the gate detecting an *accidental* change. That is the whole value of
having them.

**Drift pins: tests that exist to fail when a constant changes.** They are not redundant, and
updating them is part of the change rather than a nuisance:

- Adding a field to `CaptionPreset` → `tests/test_kinetic_compositor.py::test_caption_preset_values_are_unchanged`
  needs three updates: the pinned dataclass field list, the six frozen `to_dict` goldens, **and** the
  `sorted(BUILTIN_PRESETS)` name list.
- Adding a `selection_weight_*` setting → `tests/test_selection_scoring.py` has a
  `SELECTION_WEIGHT_NAMES` tuple compared against `type(settings).model_fields`.
- Adding any setting → `tests/test_config_documentation.py` requires a matching `.env.example` entry.
- Naming `libx264` or `-crf` outside `worker/ffmpeg_utils.py` / `worker/video_encoders.py` →
  `tests/test_output_compat.py` fails. Both guards exist because those flags were once duplicated
  across seven call sites, which is how three of them came to be missing from all seven.

**Deliberate refusals.** Several modules decline to do something rather than doing it approximately,
and each refusal is load-bearing:

- `worker/effects/sfx.py` synthesises `pop` and `click` but **not** `whoosh` — a whoosh needs a filter
  that moves across the sound, ffmpeg cannot express a time-varying filter frequency in one pass, and
  a static band-passed noise swell is a hiss. Shipping a hiss called "whoosh" is the mislabelling
  `A15` exists to stop.
- `worker/language.py` returns **no language** for Han script. It is used by Chinese *and* Japanese.
- `worker/script_support.py` reports `caption_script_unsupported` rather than substituting a font
  that cannot help. Nothing vendored covers Arabic, Hebrew, Thai or CJK — the plan's "Noto covers
  CJK" is true of the Noto *project*, not of the vendored `NotoSans`.
- `worker/video_encoders.py` refuses `h264_v4l2m2m`: no constant-quality mode, so using it would
  switch the pipeline from a quality target to a bitrate target.

**Ask the font, not fontconfig.** `fc-match` is a *best match* and always answers —
`fc-match ':lang=ar'` returns a font containing no Arabic. Use `fc-list :lang=xx` (which returns
nothing when there is nothing) and verify against the file's `cmap`.

**And do not ask fontconfig whether a *vendored* face exists.** `font_available` consults
`assets/fonts.json` before `fc-list`, because the renderer passes `font_assets_dir` to libass as
`fontsdir` and a face we ship renders whether or not the host installed it. Probing only
fontconfig made all fourteen presets substitute to Noto Sans anywhere `fc-cache` had not been run
over `assets/fonts` — which `setup_dev_env.sh` and the Dockerfile both do and
`.github/workflows/ci.yml` does not, so CI was failing six assertions while every local run was
green. The system-wide install is still wanted (fontconfig can select named instances of a
*variable* font, which `fontsdir` cannot, and `drawtext` goes through fontconfig), but it is no
longer load-bearing for the vendored faces. If you add a probe for a resource this repository
ships, check the shipped copy first.

**A listed encoder is not a usable one.** `ffmpeg -encoders` reports what was compiled in. This
ffmpeg lists `h264_v4l2m2m` and fails on the first frame. Availability is a real one-frame encode.

**Degradations are markers on the clip record, not log lines.** `music_degraded:synthesised`,
`caption_script_unsupported:*`, `encoder_unavailable:*`, `sfx_missing:*`,
`caption_face_overlap_unavoidable`, `subtitle_translation:skipped_*`. The rule: an absent feature
with no explanation is indistinguishable from a broken one, and the clip record is the only thing a
caller sees.

### Small things that will bite

- **The transcript cache is keyed on file *content*, not path.** `transcript_cache.hash_source`
  digests the bytes, so two tests that write the same placeholder payload to different `tmp_path`
  files share one cache entry. A test asserting a cache *miss* then passes or fails depending on
  whether something earlier in the session stored one — it passed in isolation and failed in the
  full suite on U4's first run. Give each fixture unique bytes.
- `ClipCandidate` lives in `worker/selection.py`, not `worker/models.py`. It now carries `cuts`
  (U4), which every selection path leaves empty; only an explicit edit populates it.
- `Transcript` / `TranscriptSegment` / `Word` live in `worker/transcribe.py`.
- `captions.Cue` has `start`, `end`, `words` — **no `text` field**.
- `ProcessingOptions` has no `to_dict()`; use `dataclasses.asdict`.
- The setting is `font_assets_dir`, not `fonts_dir`.
- `assets/fonts.json`'s `weight` is on **fontconfig's** scale (regular 80, bold 200, black 210), not
  OS/2's (400/700/900). `captions._fc_weight` converts.
- ASS colours are byte-reversed `&HAABBGGRR`. See `worker/branding.py`.
- `sendcmd` addresses filters **by name**, so multiple `crop` filters need instance names (`crop@t0`).
- ffmpeg's `movie=filename=…` source filter avoids adding an input — compositor input indices are
  load-bearing and counted from the argv.
- Presets are frozen and shared across every clip in a job. Use `dataclasses.replace`, never mutate.

## 6. How to work here

Read the README's **Testing** section, including `scripts/mutate.py`. The short version: a passing
suite proves the tests agree with the code, not that they would disagree with the *wrong* code — and
almost nothing in this project fails loudly. Ranking changes produce plausible orderings; a caption
in the wrong font still encodes.

Mutation testing is what has been catching those. Recent escapes were, every time, a real defect
rather than a missing assertion: a leaked file descriptor on each unreadable font file, an overlay
box that was only even-sided at one frame width, a compositor wiring that could be replaced with an
empty list while every unit test still passed, and two cases of one fact being stated in two places
so that changing either had no effect.

Add a spec per batch under `tests/mutations/`. `tests/mutations/example.json` is the template and
doubles as a self-test for the tool. A spec's `command` can be anything, so the frontend gets one
too — `["npm", "--prefix", "frontend", "run", "test:run"]`; see
`tests/mutations/u4_transcript_trim_frontend.json`.

**A mutation run is only as trustworthy as the baseline underneath it.** `CAUGHT` means "some test
failed", and it cannot tell *why*. U4's first run reported 22 of 22 caught; one order-dependent
test in the batch was failing for its own reasons, so every mutation inherited a free `CAUGHT`.
With that test fixed, two mutations escaped — one a test asserting against the very constant the
mutation changed (vacuous by construction), one a genuine second source of truth. **Run the target
tests green on unmutated code first**, and treat a suspiciously perfect first result as a reason to
look harder rather than a result.
