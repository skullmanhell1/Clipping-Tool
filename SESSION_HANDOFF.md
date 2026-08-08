# Session Handoff

Rewritten after the Phase 1–4 improvement pass. The previous version described the five completed
specs and said "no PRs are open", which is no longer true of either half — it has been replaced
rather than amended, because a handoff document that is wrong is worse than none.

## Start here

**1. Check that `main` has the work before building on it.** See
[§1](#1-where-the-work-is). One PR carries the whole Phase 1–4 pass onto `main`; until it
lands, branching off `main` means rebuilding things that already exist.

```bash
git fetch origin
git cat-file -e origin/main:worker/script_support.py && echo "main is current" \
  || echo "main is BEHIND - see section 1"
```

**2. Then read `docs/IMPROVEMENT_PLAN.md`.** It is the prioritised backlog — 154 numbered items with
a priority and effort estimate each, every current value quoted from the code. **142 are now
implemented.** The 12 that remain are listed in [§3](#3-what-is-actually-left), and all but one are
blocked on something other than effort.

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

**Resolved: all of this is on `main`.** `main` is at `5ed1b24` (PR #111) and the probe in "Start here"
passes. Landed since this table was written: #83 face detection, #86, #92, #94, #106 (the API and UI
silently dropped or contradicted 24 options), #107/#108 (the five new specs, documentation only), #109
(**I9** — ruff `UP`/`B`, `ruff format` enforced, `black` removed), #110 (**O13**–**O15** HDR
tone-mapping), #111 (**M9**–**M12** render-fidelity measurement, which is what introduced the VMAF
ffmpeg requirement in §2), #114 CodeQL, #115, and #10. So two Appendix B claims are **no longer true**:
`vmaf`/`ssim`/`psnr` and `tonemap`/`zscale` now exist in the tree.

Version `0.11.0`, and note `CHANGELOG.md` still carries a large `[Unreleased]` section above it — an
entire release of work with `VERSION` unbumped. Naming that release is a human decision and has not
been made.

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

Measured on `5ed1b24`. Figures in older revisions of this table, and the `1994`/`98` pair in
`.kiro/specs/face-detection-upgrade/CLOSE_OUT.md`, are historical snapshots — take a fresh
measurement rather than trusting any of them, including this one.

| Gate | Expected |
| --- | --- |
| `pytest` | **2292 passed, 0 failed, 0 skipped, 0 warnings** |
| `npm run test:run` | **141 passed** (11 files) |
| `ruff check .` | clean |
| `ruff format --check .` | clean — 207 files (I9; blocking in CI) |
| `mypy .` | clean — 106 source files |
| `python scripts/fetch_emoji.py --check` | `all 326 noto emoji vendored` |
| `python scripts/fetch_models.py --check` | `all 1 detector model(s) verified` |
| `scripts/smoke_reel.py` | renders; 15 effects incl. `music_degraded:synthesised` |
| `scripts/docker_smoke.sh` | builds and serves; prints `I12 OK` |
| `npm audit --audit-level=critical` | exits 0 |

**Warnings are errors** and **a skipped test fails CI** — both deliberate, both explained in the
README's Testing section. The full backend suite takes about **five minutes** (290 s measured).

**`VMAF_FFMPEG_BINARY` must point at a libvmaf-capable ffmpeg or the M9 fidelity tests fail**, several
steps from the cause. The distro/static ffmpeg on `PATH` does not have the filter; CI pins a separate
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

12 items. Only one is a matter of effort.

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

**`I9` is done** — `UP` and `B` are enabled, `ruff format` is enforced in CI, and `black` has been
removed rather than left listed and unrun. Two consequences outlive the sweep. First, **`RUF100` is
now on, and it is load-bearing rather than tidiness**: `ruff format` rewraps lines, and a `# noqa`
does not travel with the code it was written for — four in `publishers/` were carried off their
violation by the formatter in this very change. A suppression stranded on a line with no violation
is invisible without `RUF100`, and hides a real finding the day that code changes. Second, **prose
mentioning `noqa` is parsed as a directive**; a comment opening `# noqa on the message...` is read
as a *blanket* suppression of the whole line. Two were found and reworded. If you write about
suppressions in a comment, keep the token out of a parseable position.

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
