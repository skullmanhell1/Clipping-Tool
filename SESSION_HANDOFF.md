# Session Handoff

Amended after the dead-code clearing pass (PRs #127-#132). The numbers in §2 and the "what is left"
list in §3 were both wrong again — for the third consecutive revision — so they have been re-measured
rather than read forward. A handoff document that is wrong is worse than none.

**The headline change: §0's failure mode is cleared.** Every feature that shipped implemented, tested
and never called is now wired, and `scripts/check_wired.py` reports **0 unwired modules and 0 unread
settings**. Both baselines in that file are empty dicts. §0 stays because the *lesson* is permanent and
the gate that enforces it is the most valuable thing in this repository — not because there is
outstanding dead code.

## Start here

**1. Read [§0](#0-the-failure-mode-this-project-keeps-producing) first.** Three caption features
shipped implemented, tested and never called. It is the most expensive recurring mistake here and it
is invisible to a green suite.

**2. Check that `main` has the work before building on it.** See [§1](#1-where-the-work-is).

```bash
git fetch origin
git cat-file -e origin/main:worker/word_spans.py && echo "main has the caption timing modules" \
  || echo "main is BEHIND - see section 1"
```

**3. Then read `docs/IMPROVEMENT_PLAN.md`.** It is the prioritised backlog — 154 numbered items in
the body plus Appendix B's newer IDs, every current value quoted from the code. The body is
substantially complete; what remains is listed in [§3](#3-what-is-actually-left), and most of it is
blocked on something other than effort.

> Recount traps, all of which have produced wrong figures at least once. `P0`–`P3` are *phase* rows,
> not items, and must be excluded — but excluding everything starting with `P` also drops `PB1`–`PB9`.
> An item can be implemented without carrying its own ID: `PB3` is satisfied by
> `publishers/preflight.py`, labelled `O10`. And an ID present in the tree is not necessarily
> *reachable* — see §0. Grep both over- and under-counts; check before believing it.

## 0. The failure mode this project keeps producing

**A module can be complete, correct, fully tested, and never called. The suite will not tell you.**

> **Status: cleared, and now gated.** Everything named below is wired, and
> `python scripts/check_wired.py --check` reports 0 unwired modules and 0 unread settings. This section
> stays because the lesson is permanent and because the gate is the most valuable artefact in the
> repository — not because there is outstanding dead code. Wiring the last of it also surfaced two
> *tests* that were measuring the wrong thing, which is the second-order version of the same defect:
> an empty `pytest.mark.parametrize` produces a **skipped** test, and a skipped ratchet at the moment
> the debt reaches zero is indistinguishable from one that has been switched off.

Found in the caption timing code: `worker/word_spans.py` (C23), `cue_constraints.apply_constraints`
(C24) and `cue_constraints.choose_break` (C25) had **no importer outside their own test modules**,
and `min_cue_seconds` / `max_reading_rate` / `caption_linguistic_breaks` were read by nothing. Three
features, ~600 lines, full property-test coverage, and no effect on a single rendered frame. Every
test passed the whole time, because a unit test of a pure function cannot tell whether anything calls
it.

Two real defects were hiding in that dead code and surfaced within minutes of wiring it up: an
unguarded `dataclasses.replace` that raised `TypeError` from inside the renderer on the duck-typed
word objects the caption paths actually use, and an R8.5 clamp that was unreachable because the
compliance fast path never checked the cue boundary. Both would have shipped as "tested".

The check is one command, and it is worth running on anything you add:

```bash
# Any worker module whose only importer is its own test is not wired in.
for f in worker/*.py worker/**/*.py; do
  m=$(basename "$f" .py)
  [ "$m" = "__init__" ] && continue
  hits=$(grep -rl "import $m\b\|from worker.$m\b\|from .$m\b" --include=*.py . \
         | grep -v "^./tests/\|^./.venv\|^$f" | wc -l)
  [ "$hits" -eq 0 ] && echo "UNWIRED: $f"
done
```

A related trap in the same family: `tests/test_config_documentation.py` proves a setting is
*documented*, not that it is *read*. A `Settings` field with no `getattr`/attribute access outside
`config.py` is a knob connected to nothing, and it will pass every gate in the project.

There is now a check for both, run in CI and as a test:

```bash
python scripts/check_wired.py --all
```

It found four more dead modules after the caption three, and **two of them had been merged that
morning**:

| Module | Item | Resolution |
| --- | --- | --- |
| ~~`worker/stabilise.py`~~ | V21 stabilisation | Wired into the geometry pass (#127). |
| ~~`worker/turn_gain.py`~~ | AU12 per-speaker level | Wired onto the speech branch before `loudnorm` (#128). The turn computation turned out to be trapped inside the `speaker_reframe` branch, so on the configuration AU12 is *for* the clip-relative turns were never derived at all. |
| ~~`worker/effects/sfx.py`~~ | A15 sound effects | Wired into the audio mix (#130). `SFX_MODE=transitions` still makes no sound on a stock install — its trigger maps to `whoosh`, which is deliberately not synthesised, and `assets/sfx/` ships empty — but it now *says so* with `sfx_missing:whoosh`. |
| ~~`worker/caption_placement.py`~~ | V15 captions off the mouth | Wired at `build_ass` **and** separately in the kinetic engine, which supersedes the compositor's captions entirely (#129). |

The fourteen `Settings` fields nothing read are also resolved (#131): **four plumbed**
(`BACKGROUND_STYLE`, `BACKGROUND_COLOR`, `MUSIC_DEFAULT_VOLUME`, `FACE_DETECTOR_BACKEND`),
`SFX_VOLUME` became live with A15, and **eight retired** — `API_HOST`, `API_PORT`, `REDIS_URL`,
`RQ_QUEUE_NAME`, `USE_INPROCESS_FALLBACK`, `PUBLIC_BASE_URL`, `X_API_KEY`, `X_API_SECRET`.

Retirement was the right answer for those eight because they described behaviour this project does not
have: there is no `import redis` or `import rq` anywhere in the tree, the API bind is fixed
independently by the container's `CMD`, `EXPOSE` and healthcheck URL, and `X_API_KEY`/`X_API_SECRET`
are OAuth1 consumer credentials for a publisher that authenticates with a Bearer token — plumbing
those means *implementing OAuth1 signing*, which is a feature, not a wiring fix. `Settings` uses
`extra="ignore"`, so a stale key in someone's `.env` stays harmless; keeping the field would have been
the harmful choice, because it reads as supported.

The check is a **ratchet against a recorded baseline**, not a clean-tree assertion — the debt above is
listed in the script with a reason each, so new dead code fails immediately while the backlog is
cleared. `tests/test_check_wired.py` asserts every baseline entry is *still* dead, so wiring one up
forces its entry to be deleted and the list can only shrink. A baseline allowed to keep fixed entries
becomes a list of historical problems that reads as current, which is worse than no baseline.

**All four are now wired, and the ratchet's baselines are empty.** What remains valuable is the *gate*,
not the backlog it cleared: it is the only thing here that can tell a finished feature from a reachable
one, and a green suite cannot.

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

**All of it is on `main`.** `main` is at `d309f36` and the probe in "Start here" passes.

Six PRs landed after `8670063`, which is the revision the previous version of this table described:

| PR | Item |
| --- | --- |
| #121 | **AU11** speech presence chain, and a loudness defect it exposed |
| #123 | the retention sweeper deleting running jobs' output directories |
| #125 | **C23** word-span hygiene, and the measurement that rules **T11** out |
| #126 | **AU12** per-speaker level matching |
| #122 | **V24** screen-recording and graphics detection |
| #124 | **V21** optional stabilisation |

Three of those inserted a settings block at the same anchor in `config.py` and `.env.example`, so they
conflicted with each other pairwise. CI never saw it: **each PR is tested against its base, never
against the others.** They were merged through one integration branch with the full suite run on the
combination first. Expect this whenever several PRs are open at once here — the conflict is in the
settings block, every time.

**Two of the six shipped inert** (V21, AU12) and neither the suite nor CI could tell. See §0.

Version `0.11.0`, and note `CHANGELOG.md` still carries a large `[Unreleased]` section above it — an
entire release of work with `VERSION` unbumped. Naming that release is a human decision and has not
been made.

**CI has been red on every branch since before #121, for a reason that has nothing to do with the
code.** The annotation on every failed job is:

> The job was not started because recent account payments have failed or your spending limit needs to
> be increased. Please check the 'Billing & plans' section in your settings

Jobs complete in about three seconds having run zero steps, including `Frontend (node 20.19)` and
`Analyze (javascript-typescript)` on branches touching no JavaScript. That pattern — every job, every
branch, including ones with nothing to do — is how to recognise it, and it is not fixable from a pull
request. Until it is resolved, **every gate must be run locally** and the figures in §2 are the only
evidence available.

The original problem is kept below because its *shape* recurs whenever PRs are stacked, and this is
the reference for untangling it. The Phase 1–4 pass was built as a stack of PRs which were all merged
— but into **each other**, not into `main` — then consolidated onto one branch
(`integrate/main-sync`) to carry the lot across:

| Merged PR | Items | Backend tests |
| --- | --- | --- |
| #61 | T4, T5, T7, M3 | 1028 |
| #62 | V4, V9, V11, V13, V17, O5, O9, O11 | — |
| #63 | I4, I6, M5, U8, U10, U13, I11 | — |
| #64–#70 | the caption, asset, audio, publishing, review and infra batches | 1266 → 1686 |
| #71 | C19, S7, S8, S12, T9, T10 | 1751 |
| #72 | A5, A9, A13, A17, A19, A22 | 1808 |
| #73 | I7, I10, I12, I13 | 1827 |
| #74 | C21, V15, AU9, O8 | 1880 |
| #75 | the mutation harness and this document | — |

**Why one branch rather than a chain.** Each PR was based on the previous one, so merging them
bottom-up landed #72 in `feat/selection-transcript`, #73 in `feat/assets-expansion` and #74 in
`feat/infra-verification` — none of which is `main`. Only #71 reached
`integrate/phase4-base`. The consolidation merges both `integrate/phase4-base` *and* the chain tip,
so nothing is taken on trust: the only conflict was `CHANGELOG.md`, and the incoming version was
verified to be a strict superset of `main`'s (every `###` heading present, released history
byte-identical) before it was taken.

**#10** (`docs: campaign-briefs spec`) is an older, unrelated documentation PR and is not part of
this.

Note that the sandbox cannot `git push` or `git fetch` with ordinary credentials — only the GitHub
tooling authenticates. Retargeting or merging from the UI is a human step.

## 2. Test baselines

Do not let these go down. A drop means something stopped running, which is worse than a failure
because it looks like success.

Measured on `main` at `5e0b953` (v0.12.0). The `2631` figure in the previous revision was six PRs
stale, the `2457` before it was four, and the `1994`/`98` pair in
`.kiro/specs/face-detection-upgrade/CLOSE_OUT.md` is older still. **This table has been stale at every
single handoff.** Take a fresh measurement rather than trusting any of them, including this one.

| Gate | Expected |
| --- | --- |
| `pytest` | **2747 passed, 0 failed, 0 skipped, 0 warnings** (about 8.5 min) |
| `npm run test:run` | **141 passed** (11 files) |
| `ruff check .` | clean |
| `ruff format --check .` | clean — 237 files (I9; blocking in CI) |
| `mypy .` | clean — 117 source files (invoke as `mypy .`; bare `mypy` errors out) |
| `python scripts/check_wired.py --check` | **0 unwired modules, 0 unread settings** — both baselines empty |
| `python scripts/fetch_emoji.py --check` | `all 326 noto emoji vendored` |
| `python scripts/fetch_models.py --check` | `all 1 detector model(s) verified: blaze_face_short_range.tflite` |
| `scripts/smoke_reel.py` | renders; 15 effects incl. `music_degraded:synthesised` |
| `scripts/docker_smoke.sh` | builds and serves; prints `I12 OK` |
| `npm audit --audit-level=critical` | exits 0 |

**Node 20 or 22 is required for the frontend gates**, not whichever node is first on `PATH`. The
sandbox ships 18, 20, 22 and 24; vitest and vite require `^20.19.0 || >=22.12.0` and crash on 18 with
an error that does not name the version as the cause. Use
`export PATH=/root/.nvm/versions/node/v20.20.2/bin:$PATH`.

**Warnings are errors** and **a skipped test fails CI** — both deliberate, both explained in the
README's Testing section. The full backend suite takes about **five minutes** (290 s measured).

**`VMAF_FFMPEG_BINARY` must point at a libvmaf-capable ffmpeg or the M9 fidelity tests fail**, several
steps from the cause. Note the static build `scripts/setup_dev_env.sh` fetches (johnvansickle 7.0.2)
*does* carry `libvmaf` — verify with `ffmpeg -filters | grep libvmaf` before assuming you need a second
binary. CI pins a separate
BtbN build by URL *and* sha256 and asserts `-filters` lists `libvmaf` before running anything. Do the
same locally — see the "Fetch the VMAF-capable ffmpeg" step in `.github/workflows/ci.yml`. Pinning is
deliberate: `compare()` refuses to difference readings taken across builds, so a silently updated
libvmaf would invalidate every stored baseline with nothing to point at.

`npm audit` reports **0 vulnerabilities**. An earlier note here recorded 9 high advisories via a
`brace-expansion` DoS in eslint's `minimatch` chain as accepted, because neither available fix was
better than the finding. That advisory is gone from the tree; the two that replaced it — `js-yaml`
(via `eslint` → `@eslint/eslintrc`) and `nanoid` (via `postcss`) — did have clean fixes and are
pinned in `frontend/package.json` under `overrides`, each to a patched release inside the major its
dependent already requires. Verified not to be a functional bump: `npm ci` resolves the same tree and
the three build outputs are identical by sha256 with and without them. The gate stays at
`--audit-level=critical` because that history repeats — it is a floor, not a statement that `high` is
tolerable.

## 3. What is actually left

Re-derived from the tree at `1133a55`, not read forward. The previous two revisions of this section
were both wrong, in both directions.

### Nothing is left that is merely a matter of effort and unblocked

That is a real change from every previous handoff. The large block of "written but unreachable" work
is gone (§0), and `clip-presentation-polish` is complete. What remains is blocked on something other
than typing, and the blockers are named below rather than implied.

### Blocked on a human decision or an account

- **CI has run zero steps since at least 8 August.** Every workflow fails in about three seconds
  having executed no steps — verified with `gh api .../jobs`, which reports `steps=0` on every job,
  including JS jobs on branches containing no JS. This is a **GitHub Actions billing block**, not a
  code failure. It cannot be fixed from a pull request. Until it is cleared, every gate is local-only
  and the no-skips / warnings-are-errors discipline is unenforced by anything but discipline.
- ~~`VERSION` is still `0.11.0`~~ — **released as `0.12.0`** and tagged `v0.12.0` (#135). Minor rather
  than major because the eight retired environment variables never had an effect, and `Settings` uses
  `extra="ignore"` so a stale key in an existing `.env` stays harmless.
- **Preference trials.** `clip-presentation-polish` task 9 and `clip-editorial-structure` task 2.12
  both gate their defaults on a blind preference trial. `evaluation/preference.py` (M12) exists; the
  *judging* needs a person. Every feature in both specs therefore ships off.

### Blocked on data that does not exist

**`eval/labels/` holds one `.gitkeep`.** This is the single most load-bearing gap in the project.

`.kiro/steering/working-agreement.md` forbids starting clip-selection quality work before the
evaluation harness exists, and `clip-editorial-structure` R7.2 forbids flipping any of its defaults
before the labelled benchmark does. Between them that blocks **S22** (topic-shift boundaries),
**S23** (semantic diversity) and **S24** (dangling-opener repair) — the majority of the only spec
that is still at zero.

There is a second-order consequence worth understanding before someone tries to get ahead of it.
`clip-editorial-structure` task 1 (the offline lexical primitives S22 and S23 both stand on) is
**written and tested** on the branch `feat/s22-s23-lexical-primitives`, and it is deliberately
**unmerged**: `scripts/check_wired.py` correctly refuses it, because its only consumers are the
blocked items. Landing it alone would re-create exactly the dead code §0 is about, and neither escape
is legitimate — `ALLOWED` requires a reason about the module rather than "not called yet", and
`KNOWN_UNWIRED` is a ratchet that may only shrink. **It lands with its first consumer.** Do not merge
it to tidy up the branch list.

### Built since: S21 (cold-open assembly)

`worker/assembly.py` (#134). A clip can now open on its strongest sentence, rendered through the
existing keep-interval path so it costs no extra encode. **Off by default**, waiting on a preference
trial like everything else in its family.

Three things about it are worth knowing before touching that area:

- **`filler._merge` sorts by start, and must never be reached with an assembly.** It is correct for
  filler removal, where keeps are monotonic, and it silently converts an assembly back into the
  contiguous clip it was lifted from. There is a mutation for that.
- **`filler.rebase_words` cannot be used for an assembly**, and the reason is not the ordering — it
  accumulates offsets in list order and is already fine there. It is the `break`: with the lifted line
  *retained*, its source range is in the keep list **twice**, so it captions the first airing and
  leaves the second silent. `assembly.rebase_onto` emits one item per occurrence, and words, emoji and
  speaker turns each have their own builder.
- **`hook_score.text_signal` is sparse** — 0.0 for most sentences. On a flat clip every candidate ties
  at zero, so anything using it to rank sentences needs to distinguish "nothing stood out" from "the
  best one is already first", or it will make a false claim about the material.

**What remains of `clip-editorial-structure` is S22, S23 and S24, and all three are blocked** on the
labelled benchmark (above). With S21 done, the spec's only available work is finished.

### Still blocked, unchanged

- **Weights CI cannot have** — `S5`, `S13`, `T2`, `T6`, `V3`, `V7`, `AU6`, `I2`. Each has its seam and
  a labelled degraded fallback built. **`V3` (active-speaker detection) remains the largest single
  visual gap**: on two-person footage the crop follows the largest, most-diarisation-active face
  rather than the person actually speaking.
- **Credentials** — `PB1` (implemented, unexercisable), `PB9`.
- **Product decision** — `U12`, multi-user auth and per-user storage.
- **The `INSTALL_ML=true` Docker image has still never been built.** Only the default path is verified.

## 4. Environment

> **If a large block of tests suddenly fails on `cv2`/`mediapipe` `ImportError: libGL.so.1`, or the
> frontend gates report `npm: command not found`, the sandbox has been reset — not your change.**
> Re-run `bash scripts/setup_dev_env.sh` and put node back on `PATH`
> (`export PATH=/root/.nvm/versions/node/v22.23.2/bin:$PATH`). Confirm with
> `python -c "import cv2, mediapipe"` and `fc-list : family | grep -ci liberation` (expect 3). The
> `.venv` itself survives. This cost 34 spurious failures once; the give-away is that the failures
> cluster on vision and font paths rather than on whatever you just edited.

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
- **`git fetch` / `git push` work.** The previous note here said `git fetch origin` fails with
  `Missing header field, please provide AuthToken` and that only the GitHub tooling authenticates.
  That is no longer true — `git` and `gh` are both pre-configured. What still fails is `gh pr create`,
  `gh pr merge` and the other GraphQL-backed `gh pr` / `gh issue` subcommands; use the REST endpoints
  instead:

  ```bash
  gh api repos/{owner}/{repo}/pulls -f title=... -f head=... -f base=main -f body=...
  gh api -X PUT repos/{owner}/{repo}/pulls/{n}/merge -f merge_method=squash
  gh api -X PATCH repos/{owner}/{repo}/pulls/{n} -f base=main   # retarget
  ```

  `gh run view --log-failed` also fails (`none of the git remotes correspond to the GH_HOST`); read
  job annotations through `gh api repos/{owner}/{repo}/check-runs/{id}/annotations`, which is where
  the billing message in §1 was found.
- **Commit before mutation testing.** `git checkout <file>` restores from the *index*, so running it
  to undo a mutation destroys any uncommitted work in that file. It cost two rounds of re-doing the
  same edits here.

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

- **Background threads run against live jobs, and nothing in the suite exercises that.** The
  retention sweeper deleted running jobs' output directories. `cleanup_expired`'s empty-directory
  branch had no age check at all - unlike the file branch beside it - and `run_pipeline` creates
  `storage/clips/<job_id>/` before it encodes anything, so the directory is legitimately empty for as
  long as the first clip takes. A sweep landing in that window removed it from under ffmpeg. Found by
  booting the app and uploading one video, not by the suite: 2457 tests pass either way, because the
  sweeper and the pipeline are only ever tested apart. The API process also runs the render
  (`ThreadPoolExecutor(max_workers=1)`, no RQ worker), so *every* background thread here shares a
  filesystem with an active job. If you add one that deletes anything, assume a job is mid-write.
- **Mocking `subprocess.run` does not reach a `shutil.which` gate sitting in front of it.**
  `WhopPublisher.status` probes for the Node interpreter (I7) before `publish` shells out, so the whop
  upload tests faked the bridge's whole response and were then refused before it ran — they were
  really asserting on whether the *host* had `node` on `PATH`. Not a skip: the publisher returns a
  `FAILED` result, so the assertion fails outright. CI's runner ships Node, so it only showed up on a
  sandbox without it. `tests/test_publishers.py` now stubs the probe via `_pretend_node_is_installed`,
  and a fourth test covers the missing-runtime branch that the stub would otherwise leave uncovered.
  Availability probes in front of a subprocess are the normal shape here, so when you mock a
  subprocess, check what gates it.
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
