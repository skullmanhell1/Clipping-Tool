# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
