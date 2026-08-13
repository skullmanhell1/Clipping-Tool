# Improvement Plan

A complete, evidence-based audit of this tool against the commercial short-form clipping
market, with every improvement worth making, the resources needed to make them, and a
priority order.

Every claim about our own code below was verified by reading it, and the current values are
quoted exactly. Nothing here is aspirational description — where something does not exist,
it says so.

**Status: written 2026-07-29 against `VERSION` 0.10.0, when none of it was done. 142 of the 154
items are now implemented.** `SESSION_HANDOFF.md` §3 lists the 12 that remain and why each was
left; `CHANGELOG.md` is the record of what landed.

Do not recount by grepping for item IDs without reading the traps `SESSION_HANDOFF.md` §"Start
here" documents — `P0`–`P3` are phase rows rather than items, excluding everything starting with
`P` also drops `PB1`–`PB9`, and an item can be satisfied without carrying its own ID (`PB3` is
`publishers/preflight.py`, labelled `O10`). **The "current values" quoted throughout this
document are as of 0.10.0 and most are now stale** — verify against the code before acting on one.

---

## How to read this

Items are numbered `C1`, `S4`, … by area so they can be referenced in commits and issues.
Each carries a priority and an effort estimate:

| Priority | Meaning |
| --- | --- |
| **P0** | The output looks or sounds wrong today. Fix before showing anyone. |
| **P1** | Materially closes a gap against commercial tools. |
| **P2** | Real improvement, not urgent. |
| **P3** | Nice to have. |

Effort: **S** ≈ under a day, **M** ≈ a few days, **L** ≈ a week or more, **XL** ≈ a project.

---

## 0. The diagnosis, in one paragraph

**The rendering capability is well ahead of the assets and defaults feeding it.** Three
independent instances of the same failure: captions ask for a font that is not installed,
emoji ask for images that are not shipped, and music asks for tracks that do not exist —
each silently degrading to something plain. On top of that, every feature that makes
short-form video look modern is **default-off**. A first-time user gets a static centre
crop with plain white captions and nothing else, which is why the honest reaction to
running it is "fine, but plain". Very little of this is missing capability. Most of it is
starvation of a capable pipeline.

The one genuinely missing capability is **clip selection intelligence**, and that is also
the thing the commercial tools actually sell.

---

## 1. Captions and typography

The most visible layer, and where the cheapest wins are.

**What we emit today** (`worker/captions.py`, `worker/effects/caption_presets.py`):

| Parameter | Current value |
| --- | --- |
| Font | `Arial` (every preset's default) |
| Size | `84` (`minimal` 76, `hormozi` 96) |
| Bold | `-1` always, i.e. synthesised |
| Primary / highlight | `&H00FFFFFF` white / `&H0000E5FF` amber |
| Karaoke secondary | `&H0000FF00` — **green** |
| Outline / shadow | `4 / 2` for `karaoke_fill`, else `2 / 1` |
| Margins | `MarginL/R` hard-coded `80 / 80` |
| Positions | 3 only: `bottom=(align 2, MarginV 220)`, `center=(5, 0)`, `top=(8, 200)` |
| Cue grouping | `words_to_cues(max_words=5, max_gap=0.6, max_duration=3.0)` |
| Line wrapping | none — no character limit, no measured wrap |
| Animations | `karaoke_fill` (`\kf`), `pop` (`\fscx60→100` over 120 ms), `typewriter` (alpha fade 30 ms), `none` |
| Highlight | `\c<colour>` + `\fscx118\fscy118` |
| Presets | `karaoke`, `boxed`, `minimal`, `pop`, `typewriter`, `hormozi` |
| Inline emoji map | 35 hard-coded pairs |

### The font bug — fix this first

`_FALLBACK_FONT = "Arial"`, and the fallback path is `resolved_font = _FALLBACK_FONT`. So
when the preset font is unavailable the code "substitutes" **the same unavailable font**.
Verified on this machine:

```
font_available('Arial')          = False
font_available('Impact')         = False
font_available('Montserrat')     = False
font_available('Liberation Sans')= False
fc-match Arial                   → NotoSans-Regular.ttf
```

The Dockerfile installs only `fonts-liberation`. So on every host, libass silently falls
back to a plain regular-weight sans and fakes the bold. That single defect explains most of
the "plain" impression.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **C1** | Fix the fallback to resolve to a real installed heavy face, ordered by preference, instead of the same missing font. Record the *actual* substitution in the marker. | **P0** | S |
| **C2** | Bundle fonts in-repo (`assets/fonts/`) and register them in the Dockerfile + `fontconfig`, so appearance never depends on the host. See §2. | **P0** | S |
| **C3** | Use real Black/ExtraBold weights rather than `Bold=-1` synthesis. Add a `font_weight` field to `CaptionPreset`. | **P0** | S |
| **C4** | Replace the green karaoke secondary (`&H0000FF00`). Green reads as dated; current idiom is white→yellow or white→brand colour. | **P0** | S |
| **C5** | Reduce `max_words` to 2–4 and raise size accordingly. Five words at size 84 gives long thin lines; short-form captions are near-full-width and read in one saccade. | **P0** | S |
| **C6** | Implement real line wrapping with a character-per-line budget and measured text width, instead of relying on `WrapStyle: 2`. | **P1** | M |
| **C7** | Add an `uppercase` preset flag. Only the hook is uppercased today (`captions.py:488`). | **P0** | S |
| **C8** | Thicken outline (6–10 at 1080×1920) and add a real offset drop shadow; expose both per preset. | **P0** | S |
| **C9** | Per-word background pill / highlight box behind the active word — a mainstream look we have no equivalent of. Requires `\3c`+`\bord` tricks or a drawn box layer. | **P1** | M |
| **C10** | Word-level punch scale on the active word for all presets, not just `pop`. | **P1** | S |
| **C11** | Fix the keyword-highlight rule. `plan_keywords` treats `probability >= 0.9` as "important", and on clean audio nearly every word clears 0.9 — so highlighting fires on almost everything, which is the same as highlighting nothing. Use relative salience (top-N per cue) instead. | **P0** | S |
| **C12** | Safe-area insets so captions never sit under platform UI. Hard-coded `MarginV` of 220/200 is not TikTok-aware. The kinetic engine has safe-area logic; standard captions do not. | **P1** | S |
| **C13** | More positions than 3, plus a numeric vertical offset. | **P2** | S |
| **C14** | Expand the preset library to 12–20 recognisable styles (see §2 for the looks to target). We ship 6, two of which are "plain". | **P1** | M |
| **C15** | Per-preset letter-spacing (`Spacing`) and `ScaleX/Y`; both are hard-coded to `0`/`100`. | **P2** | S |
| **C16** | Multi-line vertical stacking with a controlled max-lines, like the kinetic engine's `max_lines`. | **P2** | M |
| **C17** | Text stroke gradient / dual outline for the "3D" look several tools offer. | **P3** | M |
| **C18** | Caption preview endpoint: render a 2-second sample of a preset over a still, so a user can pick a style without a full render. | **P1** | M |
| **C19** | Emoji inline *and* caption-adjacent placement, driven by the highlighted keyword rather than a time budget (see §3). | **P1** | M |
| **C20** | Auto-contrast: sample the frame behind the caption and pick outline/box colour for legibility. | **P2** | M |
| **C21** | RTL and CJK handling — `WrapStyle: 2` plus a Latin font will fail on Arabic/Hebrew/Chinese. Noto covers CJK; nothing selects it per language. | **P2** | M |
| **C22** | Profanity masking option for captions. | **P3** | S |

**Context on why this matters:** analysis published by OpusClip reports captions appear in
roughly 80% of short-form clips and that animated captions overwhelmingly outnumber static
ones ([OpusClip research](https://www.opus.pro/research/best-caption-strategy-entertainment-comedy)).
Their guidance attributes retention gains to reduced cognitive load and visual anchoring
([OpusClip](https://www.opus.pro/blog/best-caption-presets-styles-boost-retention)).
Submagic's own font guide names Montserrat as a common choice for short-video captions
([Submagic](https://www.submagic.co/blog/best-font-for-subtitle)).
*(Sources rephrased for compliance with licensing restrictions.)*

---

## 2. Assets to acquire

We currently ship **no fonts, no emoji, no music, and no b-roll**. Three of the four
directories contain only a `.gitkeep`.

### 2.1 Fonts

All of these are SIL Open Font License, so they can be bundled and redistributed with the
app (the OFL permits bundling and redistribution provided reserved names are respected —
[FontSquirrel summary](https://www.fontsquirrel.com/fonts/bebas-neue)). Available from
[Google Fonts](https://fonts.google.com/), which states its catalogue is open source and
free for commercial use ([Kinsta overview](https://kinsta.com/blog/best-google-fonts/)).

| Font | Why | Use for |
| --- | --- | --- |
| **Anton** | Very heavy condensed display, single weight | Hormozi-style, hooks |
| **Archivo Black** | Heavy grotesque, wide | Bold statement captions |
| **Bebas Neue** | Tall condensed all-caps | Titles, thumbnails |
| **Montserrat** (ExtraBold/Black) | The default modern caption face | General captions |
| **Oswald** | Condensed, narrow, good for long lines | Dense text |
| **Poppins** (Bold/Black) | Geometric, friendly | Lifestyle content |
| **Inter** (Black) | Screen-optimised, huge weight range | UI-adjacent, clean |
| **Roboto Condensed** (Bold) | Safe, condensed | Fallback |
| **Noto Sans** + **Noto Sans CJK/Arabic** | Broad script coverage | Non-Latin fallback (C21) |
| **Luckiest Guy / Bangers** | Cartoon display | Comedy/gaming |

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **A1** | Vendor 8–12 OFL fonts into `assets/fonts/`, with their `OFL.txt` licence files. | **P0** | S |
| **A2** | Register them in the Dockerfile (copy into `/usr/share/fonts`, run `fc-cache -f`). | **P0** | S |
| **A3** | A `fonts.json` manifest mapping friendly name → file → weight, so presets reference a font we know we have. | **P0** | S |
| **A4** | Expose the font list via `/api/info` so the UI can offer a real picker. | **P1** | S |
| **A5** | Allow user font upload into `assets/fonts/` with `fc-cache` refresh. | **P2** | M |

### 2.2 Emoji

Current: Twemoji **72×72 PNG** fetched lazily from jsDelivr at render time, upscaled to
`scale=151:-1` — a **2.1× upscale**, which is visibly soft. `assets/emoji/` is empty and
the `.gitignore` claims they are "downloaded at build time", which **nothing does**.

Options:

| Set | Format | Count | Licence |
| --- | --- | --- | --- |
| **Twemoji** | SVG + PNG 72 | Unicode 14 coverage | CC-BY 4.0 (art) |
| **OpenMoji** | SVG, PNG 72 + **618** | ~7,100 ([Iconduck](https://www.iconduck.com/sets/openmoji-emoji-set)) | CC BY-SA 4.0 ([Emojipedia](https://www.emojipedia.org/openmoji)) |
| **Noto Emoji** | SVG + OpenType-SVG | Full | OFL / Apache |

OpenMoji ships production-ready 618×618 PNGs as well as SVG
([OpenMoji repo](https://github.com/RobertMueller2/openmoji)), which removes the upscaling
problem without needing a rasteriser.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **A6** | Switch the source to SVG (or OpenMoji 618 PNG) and rasterise at target size. Kills the 2.1× blur. | **P0** | S |
| **A7** | Vendor or pre-fetch the emoji set at **build** time so rendering never needs a live CDN — and correct the false `.gitignore` comment. | **P0** | S |
| **A8** | Fix `build_overlay`'s hard-coded 1080 reference (`scale=int(1080*size_frac)`): on 1:1 or 16:9 output the emoji is the wrong size. Scale relative to actual frame width. | **P0** | S |
| **A9** | Expand `KEYWORD_EMOJI` — currently **85 keywords → 53 unique glyphs**. Target 500+. | **P1** | M |
| **A10** | Add stemming/lemmatisation. Verified misses today: `winning`, `wins`, `won`, `fired` all fail while `win` hits. | **P0** | S |
| **A11** | Drive emoji from keyword salience rather than the `INTENSITY_SPACING` stopwatch (`standard` = one per 5 s). | **P1** | S |
| **A12** | Prevent immediate repeats of the same glyph, and cap per clip. | **P2** | S |
| **A13** | Offer several emoji styles (Noto / Twemoji / OpenMoji) as a user choice. | **P3** | S |

### 2.3 Music

**`worker/effects/audio.py` does not play music — it synthesises a tone.** Each "mood" is
two sine waves plus tremolo and a lowpass:

```
upbeat    root 293.66  fifth 440.00  tremolo 5.0  cutoff 3200
suspense  root 110.00  fifth 164.81  tremolo 0.8  cutoff 1400
```

A real track is used only if the user drops `assets/music/<mood>.mp3` in themselves, and
that directory is empty. So enabling music today adds a **drone**, not a bed.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **A14** | Ship a small library of real, licence-clean beds (CC0 / Pixabay-licensed) per mood. Pixabay's licence permits commercial use ([Pixabay FAQ](https://pixabay.com/en/service/faq/)). | **P0** | M |
| **A15** | Keep synthesis strictly as a labelled last-resort fallback, and mark it degraded. | **P0** | S |
| **A16** | Loop/trim beds to clip length with a musical fade rather than an abrupt cut. | **P1** | S |
| **A17** | Multiple tracks per mood with deterministic per-clip selection, so a batch is not monotonous. | **P2** | S |

### 2.4 B-roll

`broll.py` matches keywords by **case-insensitive filename-stem substring** against
`assets/broll`, which is empty, and `_default_external_downloader` is
*"intentionally not implemented … Returns None"*. So b-roll cannot function out of the box.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **A18** | Implement a real provider. [Pexels](https://pexels.com/api/documentation) and [Pixabay](https://pixabay.com/api/docs/) both offer free APIs with commercial-use licences and no attribution requirement for Pexels ([Pexels licence](https://www.pexels.com/license/)). | **P1** | M |
| **A19** | Replace filename-substring matching with tag/semantic matching (embeddings or provider search terms). | **P1** | M |
| **A20** | Cache downloads under `broll_cache_dir` with licence metadata retained, since assets with an empty `license` are currently dropped. | **P1** | S |
| **A21** | Ship ~30 generic starter clips so the feature demonstrates itself. | **P2** | M |
| **A22** | Ken-Burns motion on b-roll stills, and audio ducking under b-roll. | **P2** | M |

---

## 3. Clip selection — the real product gap

This is what commercial tools sell. OpusClip describes combining transcript signals
(hook strength, narrative arc, emotional intensity) with **audio dynamics — pitch range,
pace shifts, laughter** — and **visual cues such as gesture and facial expression**
([OpusClip](https://www.opus.pro/blog/viral-moment-detection-api)), tuned against a
large-scale clip analysis ([OpusClip research](https://www.opus.pro/research/how-to-make-viral-video)).

**What we use:** the LLM is shown *only* `[i] start-end: text` lines. The deterministic
fallback uses `silencedetect=noise=-30dB:d=0.4` plus length presets, and when it must cap
the count it keeps the longest segments — its own docstring calls this
*"a simple heuristic standing in for real scoring in later phases"* (`segmentation.py:192`).
Visual scoring is 12 keyframes at `width=160` reduced to mean luma and `|Δbrightness|`,
described in-source as a *"proxy that needs no vision model"*, blended 50/50.

**There are no audio features in selection at all.** Verified by grep: no pitch, no energy,
no speech rate, no laughter.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **S1** | **Build an evaluation harness first.** 20 of your own sources, hand-labelled with the moments you would actually post; score the selector against them (precision@k, overlap IoU). Without this every change below is unmeasurable. | **P0** | M |
| **S2** | Audio energy / RMS envelope per segment. Cheap: one `astats` or `ebur128` pass over already-extracted audio. | **P1** | M |
| **S3** | Pitch range and variance (excitement proxy) — `librosa` or `parselmouth`. | **P1** | M |
| **S4** | Speech rate (words/sec from existing word timings). Free — the data is already there. | **P1** | S |
| **S5** | Laughter / applause / cheering detection. A small audio classifier (YAMNet or similar) over the clip audio. | **P1** | L |
| **S6** | Hook scoring for the first 1.5–3 s specifically; retention is decided there and nothing models it. | **P1** | M |
| **S7** | Question/answer and list-structure detection ("three things", "here's why") — strong self-contained-clip signals. | **P2** | M |
| **S8** | Sentiment/emotional-intensity scoring per segment via the LLM, not just moment picking. | **P2** | M |
| **S9** | Scene-change awareness so clips don't start mid-shot. [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) is the standard tool. | **P1** | M |
| **S10** | Feed audio/visual features **into the LLM prompt**, not just text. The model currently cannot see that a moment was loud or animated. | **P1** | M |
| **S11** | Replace `keep the longest segments` with real scoring in the fallback path. | **P1** | M |
| **S12** | Semantic completeness check — does the clip stand alone without prior context? | **P2** | M |
| **S13** | Replace brightness/motion proxies with a real vision signal (face size, gesture energy, shot variety). | **P2** | L |
| **S14** | Sample more than 12 keyframes and above `width=160` for visual scoring. | **P2** | S |
| **S15** | De-duplicate overlapping candidates and enforce topic diversity across the returned set. | **P1** | M |
| **S16** | Record published-clip performance (views/retention) and use it to tune weights. This is the one advantage self-hosting has over a vendor — it can learn *your* audience. | **P1** | L |
| **S17** | Expose per-signal weights as config so the blend is tunable without code changes. | **P2** | S |
| **S18** | Calibrate the virality score against something. Right now it is an LLM's unanchored 0–100 opinion. Independent testing of Opus Clip found roughly 40% of generated clips discarded and the score sometimes misleading ([BIGVU](https://bigvu.tv/blog/opus-clip-tested-2026-where-ai-wins-40-percent-discard/)) — so even a tuned score is a shortlist, not a verdict. | **P2** | M |

---

## 4. Transcription

Current: `faster-whisper`, model **`base`**, `beam_size=5`, `word_timestamps=True`,
`vad_filter=True` with default parameters, device auto (cuda/float16 else cpu/int8).

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **T1** | Default to a larger model (`small`/`medium`). `base` is a noticeable accuracy step down, and captions are the most visible artefact in the product. Submagic-class tools are reported around 98% on clean audio ([review](https://autogpt.net/ai-tool/submagic-ai/)). | **P0** | S |
| **T2** | Forced alignment for word timestamps. Vanilla Whisper timestamps drift; WhisperX-style wav2vec2 alignment is reported around ±50 ms versus several hundred ms ([WhisperX docs](https://docs.clore.ai/guides/audio-and-voice/whisperx), [WhisperX](https://github.com/jeffh/whisperX)). Karaoke captions live or die on this. | **P1** | M |
| **T3** | Hallucination/repetition filtering. Whisper invents text over music and silence; nothing filters it. | **P1** | M |
| **T4** | `initial_prompt` / custom vocabulary for names, jargon, brands. | **P1** | S |
| **T5** | Expose VAD parameters; they are currently fixed at library defaults. | **P2** | S |
| **T6** | Real diarisation via `pyannote` as the optional backend the code already anticipates — the current offline path is explicitly *"attribution by proxy"*. | **P2** | L |
| **T7** | Confidence-driven caption behaviour: dim or flag low-probability words instead of asserting them. | **P2** | M |
| **T8** | Cache transcripts by source hash so re-runs skip ASR entirely. | **P1** | S |
| **T9** | Per-segment language detection for code-switching content. | **P3** | M |
| **T10** | Translated-subtitle output as a separate track, not just `task=translate` replacing the text. | **P2** | M |

---

## 5. Reframe and visual

Current: **Haar cascade** `haarcascade_frontalface_default.xml` (the docstring's MediaPipe
claim is not what runs), sampled at `reframe_sample_fps=5.0` capped at
`reframe_sample_cap=120` frames, smoothed with `EMA alpha=0.35`, applied via `sendcmd` at
`command_fps=12.0`. Split-screen is limited to `split_screen_max_regions=2` and crops each
tile on that speaker's **mean** centre — static, with `intensity` ignored.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **V1** | **Reframe is off by default**, so the default output is a centre crop that decapitates any off-centre speaker. Turn it on. | **P0** | S |
| **V2** | Replace Haar with a modern detector (MediaPipe Face Detection / BlazeFace, or YOLO-face). Haar is from 2001, misses profiles and non-frontal faces, and false-positives on texture. | **P1** | M |
| **V3** | Active-speaker detection so multi-person footage follows whoever is talking, rather than the largest face. This is the single biggest reframe quality gap; open models exist (TalkNet, Light-ASD). | **P1** | L |
| **V4** | Reset tracking on shot changes — currently the EMA smooths *across* a cut, so the crop drifts through the new shot. Pair with **S9**. | **P1** | M |
| **V5** | Per-time tracking inside split-screen tiles instead of a static mean centre. | **P2** | M |
| **V6** | More than 2 split-screen regions (`split_screen_max_regions=2`). | **P3** | M |
| **V7** | Subject/body detection so a person turned away is still framed. | **P2** | M |
| **V8** | Raise `command_fps` above 12 or interpolate; fast movement will visibly step. | **P2** | S |
| **V9** | **No clip-to-clip transitions exist anywhere.** `transitions` only does an intro punch-in. Add cross-dissolve, whip-pan, zoom-cut. | **P1** | M |
| **V10** | Filler-word removal produces **hard cuts with no audio crossfade or j-cut** — audible clicks and unnatural rhythm. The stem engine can repair seams; wire it up by default or add a short crossfade. | **P0** | M |
| **V11** | `crop_blur` letterbox background is a fixed `boxblur=40:1` + `eq=brightness=-0.1`. Offer gradient/colour/mirrored backgrounds. | **P2** | S |
| **V12** | Hook title is off by default and uses a single hard-coded amber (`&H0000E5FF`) at size 110. Make it styled and on by default. | **P1** | S |
| **V13** | Progress bar is a plain `drawbox` in one cyan (`0x22D3EE@0.9`, height 12). Offer styles/positions. | **P3** | S |
| **V14** | End-card / CTA / subscribe animation — absent entirely. | **P2** | M |
| **V15** | Face-aware caption placement so text never covers the speaker's mouth. | **P2** | M |
| **V16** | Auto-crop to remove existing letterboxing in the source before reframing. | **P2** | M |
| **V17** | Thumbnail selection is `at=min(1.0, duration/2)` — an arbitrary frame. Pick the best frame (face present, eyes open, sharp). | **P2** | M |
| **V18** | Colour presets are 5 fixed `eq`/`curves` strings; add LUT support. | **P3** | M |
| **V19** | Zoom is a linear ken-burns `(1+0.12*on/total)`; add easing and beat-synced punches. | **P3** | M |

---

## 6. Audio

Verified absent across the whole repo: **no `loudnorm`, no `dynaudnorm`, no
`sidechaincompress`, no LUFS target, no de-noise, no de-esser.** Music is mixed at
`volume=0.12` then `amix=inputs=2:duration=first:normalize=0`.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **AU1** | **Loudness normalisation to a platform target.** Reported targets: YouTube ≈ −14 LUFS, Instagram/TikTok ≈ −10 to −12 LUFS, with true peak ≤ −1 dBTP ([OpusClip](https://www.opus.pro/blog/best-loudness-normalizers), [process.audio](https://process.audio/blog/lufs-targets-streaming-platforms-loudness-metering)). Quiet clips get turned up by the platform, amplifying noise floor. Use two-pass `loudnorm`. | **P0** | M |
| **AU2** | **Duck music under speech** with `sidechaincompress`. A flat `volume=0.12` bed either buries speech or is inaudible. | **P0** | M |
| **AU3** | True-peak limiting before output. | **P1** | S |
| **AU4** | Speech de-noise (`afftdn`/`arnndn`) as an option — `arnndn` is available in ffmpeg and was one of the filters hidden by the capability-probe bug. | **P1** | M |
| **AU5** | De-reverb / de-esser for poor source audio. | **P2** | M |
| **AU6** | Real source separation by default where possible — the shipped ffmpeg backend is self-described as *"a mid-channel / speech-band approximation, not source separation"*. Requires `demucs` (see `requirements-ml.txt`). | **P2** | M |
| **AU7** | Silence trimming at clip head/tail so clips don't open on dead air. | **P1** | S |
| **AU8** | Normalise sample rate and channel count on output (`-ar 48000 -ac 2`); neither is set anywhere. | **P1** | S |
| **AU9** | Sound-effect stings on transitions/emoji (whoosh, pop). | **P3** | M |

---

## 7. Output encoding and platform compliance

Every pass uses the same ladder: `-c:v libx264 -preset veryfast -crf 20`, `-c:a aac -b:a 128k`,
`-movflags +faststart`. **Not set anywhere:** `-pix_fmt yuv420p`, `-profile:v`/`-level`,
frame-rate normalisation, `maxrate`/`bufsize`, `-ar`/`-ac`, any hardware encoder, any
per-platform profile. Resolution comes only from
`ASPECT_PRESETS = {9:16:(1080,1920), 1:1:(1080,1080), 16:9:(1920,1080), 4:5:(1080,1350)}`.

Consensus target for short-form is 1080×1920 / 9:16, H.264 + AAC, ~30 fps
([Sovran](https://sovran.ai/blog/social-media-video-specs),
[unifab](https://unifab.ai/resource/instagram-video)).

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **O1** | Set `-pix_fmt yuv420p` explicitly. Without it a 4:2:2/10-bit source can yield a file some platforms and browsers refuse to decode. | **P0** | S |
| **O2** | Set `-profile:v high -level 4.0` for broad device compatibility. | **P0** | S |
| **O3** | Normalise frame rate (`-r 30`), and handle variable-frame-rate sources, which otherwise desync captions. | **P0** | S |
| **O4** | Add `-maxrate`/`-bufsize` VBV caps so a busy clip cannot balloon past platform size limits. | **P1** | S |
| **O5** | Expose CRF/preset/resolution in config — all are hard-coded in six places. | **P1** | S |
| **O6** | **Reduce the pass count.** Minimum 3 full re-encodes per clip (cut → geometry → composite) plus thumbnail, plus one per media-replacing engine, each at `crf 20`. That is generation loss and wasted time; the cut could be stream-copied and geometry folded into the composite. | **P1** | L |
| **O7** | Per-platform output profiles (resolution/bitrate/duration) rather than one file for all destinations. | **P1** | M |
| **O8** | Optional hardware encoding (NVENC/QSV/VideoToolbox). | **P2** | M |
| **O9** | 720p and 4K options; only the four fixed presets exist. | **P2** | S |
| **O10** | Validate output against platform constraints **before** publishing. Verified: no publisher checks aspect, duration, resolution, file size, codec or fps — the only pre-flight is `video_path.exists()`. | **P0** | M |
| **O11** | Sidecar SRT/VTT export for platforms that accept uploaded captions. | **P2** | S |
| **O12** | Burned-in vs soft-caption choice. | **P3** | M |

---

## 8. Publishing

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **PB1** | **Verify all five publishers against live accounts.** None has ever run against a real platform, including the approve/retry path. This is the largest untested surface in the repo. | **P0** | M |
| **PB2** | Wire `/api/publish-attempts/{id}/approve` and `/retry` into the UI. Verified: **zero references** in `frontend/src/`. The endpoints exist but are unreachable from the dashboard. | **P0** | S |
| **PB3** | Pre-flight media validation per platform (see **O10**). | **P0** | M |
| **PB4** | Token refresh and expiry handling; nothing refreshes OAuth tokens. | **P1** | M |
| **PB5** | Retry with exponential backoff for transient failures, separate from human review. | **P1** | M |
| **PB6** | Per-platform caption/hashtag tailoring on publish — metadata limits exist (`tiktok 80/300/8`, `x 70/260/4`, …) but text is truncated rather than regenerated to fit. | **P2** | M |
| **PB7** | Scheduling UI with a calendar, and best-time-to-post suggestions. | **P2** | M |
| **PB8** | Fetch post-publish metrics (views/likes) — the input for **S16**. | **P1** | M |
| **PB9** | More destinations: LinkedIn, Facebook Reels, Snapchat, Threads. | **P3** | L |

---

## 9. Product and UX

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **U1** | **Change the defaults.** A default run enables only captions, 9:16, `ai` strategy and metadata. Off: reframe, zoom, transitions, fades, hook title, progress bar, music, filler removal, b-roll, kinetic typography, speaker reframe, visual selection, emoji. The tool ships looking worse than it is capable of. | **P0** | S |
| **U2** | Ship 3–4 opinionated **profiles** ("Podcast", "Gaming", "Talking head", "Educational") that set a whole coherent bundle. | **P0** | M |
| **U3** | Preview player with scrubbing before publishing. | **P1** | M |
| **U4** | Transcript-based trimming — click words to cut. This is the feature Descript-class tools are chosen for. | **P1** | L |
| **U5** | Caption style picker with live preview (pairs with **C18**). | **P1** | M |
| **U6** | Brand kit: persist font, colours, logo, CTA per profile. | **P1** | M |
| **U7** | Per-clip regenerate that re-renders only what changed instead of the whole clip. | **P2** | M |
| **U8** | Progress detail per stage — currently a single coarse percentage. | **P2** | S |
| **U9** | Batch review UI: approve/reject many clips quickly. | **P2** | M |
| **U10** | Better empty/error states; failures surface as a bare string. | **P2** | S |
| **U11** | Keyboard shortcuts for review. | **P3** | S |
| **U12** | Multi-user auth and per-user storage; single-tenant today. | **P2** | L |
| **U13** | Replace the placeholder landing page (`api/main.py:1237`). | **P3** | S |

---

## 10. Infrastructure and performance

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **I1** | **Concurrency.** One worker thread; renders take minutes; a backlog serialises. `redis`/`rq` are declared dependencies that nothing imports and `worker/tasks.py` is imported by nothing — either wire it up or drop it. | **P1** | L |
| **I2** | GPU support for Whisper and any ML path. | **P1** | M |
| **I3** | Cache intermediates (transcript, keyframes, stems) keyed by source hash — pairs with **T8**. | **P1** | M |
| **I4** | Job cancellation; there is no way to stop a running render. | **P1** | M |
| **I5** | Resume partially-completed jobs after the interrupted-job fix (they are marked failed wholesale). | **P2** | M |
| **I6** | Structured logging with a job id, and metrics per stage. | **P2** | M |
| **I7** | Docker image size and build time — `INSTALL_ML` pulls torch; consider a separate image. | **P3** | M |
| **I8** | Frontend test coverage beyond `api.js` and `Dropdown`. | **P2** | M |
| **I9** | Adopt `black`, plus ruff `UP` (~450 findings) and `B` (~30). | **P2** | M |
| **I10** | Clear the 11 dev-only npm advisories (needs a breaking `vite@8`). | **P3** | S |
| **I11** | Resolve the two `react-hooks/exhaustive-deps` warnings properly — both are polling effects where the naive fix causes a re-subscribe loop. | **P2** | S |
| **I12** | Verify a full Docker build end to end; it has never been run. | **P1** | S |
| **I13** | Verify URL ingest (`yt-dlp`) actually downloads; only local files have been exercised. | **P1** | S |

---

## 11. Measurement

The theme that connects the worst defects found so far — a capability probe that hid 124
ffmpeg filters, two parity guards that never ran in CI, a caption font that was never the
one requested — is that **nothing measured the real output**.

| # | Item | Pri | Effort |
| --- | --- | --- | --- |
| **M1** | Golden-output rendering tests: render fixed sources and compare frame hashes / SSIM against approved references, so appearance regressions are caught. | **P1** | M |
| **M2** | A visual smoke reel: one command producing a clip exercising every effect, for eyeball review each release. | **P1** | S |
| **M3** | Caption accuracy benchmark (WER) across Whisper model sizes on your own footage. | **P1** | M |
| **M4** | Selection benchmark — see **S1**. | **P0** | M |
| **M5** | Render-time benchmark per stage, to find where the minutes go. | **P2** | S |
| **M6** | Loudness assertion on output (measure LUFS post-render, fail outside tolerance). | **P1** | S |
| **M7** | Extend the real-binary testing idea to fonts and emoji: assert the *resolved* font is the requested one, which would have caught the fallback bug immediately. | **P0** | S |

---

## 12. Suggested order

**Phase 1 — "stop looking plain" (a few days, highest visible payoff)**
C1–C5, C7, C8, C11 · A1–A3, A6–A8, A10, A14, A15 · V1, V12 · U1, U2 · T1 · O1–O3 · M7

Everything here is a bug, a missing asset, or a default. Almost no new capability. This is
the phase that changes your reaction to running the tool.

**Phase 2 — "sound and ship correctly"**
AU1, AU2, AU7, AU8 · O10, O4 · PB2, PB3, PB1 · V10 · M2, M6 · I12, I13

**Phase 3 — "actually pick good moments"**
S1 (first) · S2, S4, S6, S9, S10, S15 · T2, T3, T8 · M3, M4

**Phase 4 — "compete on polish"**
C6, C9, C14, C18 · V2, V3, V4, V9 · A18–A20 · U3–U6

**Phase 5 — "scale"**
I1, I2, I3, I4 · S5, S16 · PB8 · U12

---

## Appendix: honest limits of this document

* Competitor capabilities are taken from vendor material and third-party reviews, not from
  running the products side by side with ours on the same footage. A real bake-off would be
  worth doing before betting much on any single item.
* Effort estimates are judgement, not measurement.
* The priority of everything in §3 depends on **S1** existing first. Without a benchmark
  those items cannot be validated, only implemented.

*External sources were rephrased for compliance with licensing restrictions; links are
inline throughout.*


---

## Appendix B: backlog items added after the v0.11.0 render-path audit

Registered here so the plan stays the single index of work. **This appendix is additive only.**
The body of this document above is still quoted against **v0.10.0** and is materially stale —
the Arial fallback, the green karaoke secondary, absent `loudnorm`, missing `pix_fmt`, `base`
Whisper, and the longest-segment production fallback are all already fixed. Correcting the body
is owned by `clip-quality-uplift` task 13; do not treat the sections above as current.

These items came from reading the render path directly rather than from competitor comparison,
which is why none of them appear in §§1–12. Each is specified in full under `.kiro/specs/`.

### Status of this appendix — read this before the tables

**The tables below describe the tree as it was at the audit. Most of them have since been built, and
the `Note` column still reads as though they have not.** Rows are kept rather than deleted, per the
"mark superseded diagnoses" rule, but the notes are now history and not diagnoses.

Implemented, with where the code lives:

| ID | Where |
| --- | --- |
| `M9`–`M12` | `evaluation/fidelity.py`, `evaluation/caption_timing.py`, `evaluation/sync.py`, `evaluation/preference.py` |
| `O13`–`O15` | `worker/colour.py` |
| `O16`–`O20` | `worker/video_encoders.py`, `worker/ffmpeg_utils.py`, `worker/frame_rate.py`, `worker/output_profiles.py` |
| `V20` | `worker/deinterlace.py` |
| `V22` | `worker/headroom.py` |
| `V24` | `worker/content_class.py` |
| `C23` | `worker/word_spans.py` |
| `C24`, `C25` | `worker/cue_constraints.py` |
| `AU11` | `worker/effects/audio.py` |

`V21` | `worker/stabilise.py`, wired into the geometry pass. Applies only on the `apply_reframe`
branch, which is the one that can hold its crop inside the rectangle `vidstab` leaves valid; the other
branches decline with `stabilise_skipped:<branch>`.

**Implemented but unreachable — CLEARED.** This section listed features that were complete, tested and
called by nothing, which is worse than unbuilt because they read as done. All of them are now wired:

| ID | Module | Wired in |
| --- | --- | --- |
| `C23`/`C24`/`C25` | `worker/word_spans.py`, `worker/cue_constraints.py` | #127, at `build_ass` |
| `V21` | `worker/stabilise.py` | #127, into the geometry pass |
| `AU12` | `worker/turn_gain.py` | #128, onto the speech branch before `loudnorm` |
| `V15` | `worker/caption_placement.py` | #129, at `build_ass` **and** in the kinetic engine |
| `A15` | `worker/effects/sfx.py` | #130, into the audio mix |

`scripts/check_wired.py` now reports **0 unwired modules and 0 unread settings**, and both baselines in
that file are empty dicts. Of the fourteen `Settings` fields nothing read, four were plumbed
(`BACKGROUND_STYLE`, `BACKGROUND_COLOR`, `MUSIC_DEFAULT_VOLUME`, `FACE_DETECTOR_BACKEND`), `SFX_VOLUME`
became live with `A15`, and eight were **retired** because they described behaviour this project does
not have — a queue with no `import redis` anywhere, a bind the container's `CMD` fixes independently,
and OAuth1 credentials for a publisher that authenticates with a Bearer token (#131).

Both ratchet tests had to be rewritten in the process, and the reason is worth keeping: an empty
`pytest.mark.parametrize` produces a *skipped* test, and a skipped ratchet at the exact moment the debt
reaches zero is indistinguishable from one that has been switched off. One of them had also been
asserting that real debt existed, using its presence as a proxy for "the checker is not reading itself"
— a proxy that was only valid while the debt was.

`T11` is **refused by measurement** rather than outstanding — the cached energy envelope has no
word-rate information and R7.8 forbids the second audio pass that would provide it. See
`worker/word_spans.py`.

`V23` is **built** (#132), and its implementation records a finding that constrains anything similar:
the crop-size mechanism R2.2 asks for **crashes ffmpeg 7.0.2**
(`Assertion best_input >= 0 failed at src/fftools/ffmpeg_filter.c:1923`) because changing a crop's
output dimensions reconfigures the filter link mid-stream. Per-frame magnification must use `zoompan`
instead. A test asserts the crash from both sides, so a future ffmpeg that fixes it will fail that test.

Still genuinely open below: `S21`, `S22`, `S23`, `S24` — the whole of `clip-editorial-structure`.
`S21` (cold-open assembly) is the only one of the four that is *available*: `S22`–`S24` are §3
clip-selection quality work, which the working agreement forbids starting before the labelled
benchmark exists. The offline lexical primitives those two need are written and tested on
`feat/s22-s23-lexical-primitives` but **deliberately unmerged**, because `check_wired` correctly
refuses them — their only consumers are the blocked items, and landing them would re-create the dead
code this section just recorded clearing.

### Measurement — `render-quality-measurement`

| ID | Item | Note |
| --- | --- | --- |
| M9 | Full-reference render fidelity (SSIM/PSNR, VMAF where built) | **No `vmaf`, `psnr`, or `ssim` exists anywhere in the repo today.** `golden_render.py` answers "did it change?", not "is it better?" |
| M10 | Caption alignment error | `wer.py` measures words; nothing measures timing, which is the defect viewers see |
| M11 | A/V sync verification | Nothing measures sync. No defect alleged — but if it drifts, every burned caption drifts and no test would notice |
| M12 | Pairwise human preference harness | The only instrument for framing, grade, and pacing changes, which have no metric |

### Signal chain — `clip-signal-fidelity`

| ID | Item | Note |
| --- | --- | --- |
| O13 | HDR → SDR tone-mapping | **Currently wrong output, not a missing feature.** Zero hits for `tonemap`/`zscale`/`colorspace`. `probe()` already fetches `color_transfer` and never reads it |
| O14 | Colour metadata on delivered files | Nothing sets `-colorspace`/`-color_primaries`/`-color_trc`; players guess |
| O15 | Colour range resolution | Full-range phone footage crushes or lifts |
| O16 | Encoder preset quality | `x264_preset="veryfast"` (`config.py:354`), paid three times per clip |
| O17 | Scaling algorithm | No `-sws_flags` anywhere; every `scale=` is default bicubic |
| O18 | Conditional frame-rate normalisation | `-r 30` applied unconditionally; correct for VFR, adds 3:2 judder to CFR 24 fps |
| O19 | Keyframe interval | No `-g` set; x264's default 250 (~8 s) applies |
| O20 | Delivered audio bitrate | `-b:a 128k` fixed; thin under a music bed |
| V20 | Deinterlacing | No `yadif`/`bwdif`; combing gets cropped and scaled into a smear |
| V21 | Stabilisation | No `vidstab`; shaky source under a moving crop compounds |

### Presentation — `clip-presentation-polish`

| ID | Item | Note |
| --- | --- | --- |
| V22 | Headroom / eye-line composition | `reframe.py:487` centres the face vertically — the most recognisable auto-crop tell, and it parks the mouth where captions go |
| V23 | Subject-scale normalisation across shots | Subject size jumps between cuts |
| V24 | Screen-recording and graphics detection | No content-type notion; a 16:9 slide is cropped to an unreadable middle third |
| C24 | Minimum cue duration and reading-rate cap | `words_to_cues` has only ceilings, no floor; fast speech yields ~0.3 s cues |
| C25 | Linguistically-aware line breaking | Width-only breaking splits proper nouns and article-noun pairs |
| AU11 | Speech presence / clarity chain | Only spectral shaping in `audio.py` is a `lowpass`; `loudnorm` is level, this is spectrum |
| AU12 | Per-speaker level matching | Diarisation exists and is never used for gain; `loudnorm` normalises the clip so cannot fix balance within it |

### Editorial structure — `clip-editorial-structure`

| ID | Item | Note |
| --- | --- | --- |
| S21 | Cold-open / multi-segment assembly | Every clip is one contiguous range. `filler.apply_keep_intervals` already renders non-contiguous keeps in one pass |
| S22 | Topic-shift boundaries | Boundaries use silence, sentence, and luma cuts only — nothing knows what the clip is about |
| S23 | Semantic diversity in the delivered set | Dedup is lexical; weighted Jaccard is blind to paraphrase |
| S24 | Dangling-opener repair | `discourse.py` already **detects** it and only ever scores it; nothing extends the start to include the antecedent |

### Ordering

`M9`–`M12` first: they gate `O16`, `O17`, `O20`, `V21`–`V24`, `C24`, `C25`, `AU11`, `AU12`, and
`S21`, none of which can be judged without them. `O13`–`O15` are the exception and should jump
the queue — they fix output that is currently incorrect and need no preference judgement.
`S22`–`S24` additionally depend on **S1**, the labelled selection benchmark, for the same reason
everything in §3 does.

### Deferred, with the blocker named

Unchanged from the body above, restated because these are the items most often rediscovered:
**weights CI cannot have** — `S5`, `S13`, `T2`, `T6`, `V3`, `V7`, `AU6`; **credentials** —
`PB1`, `PB9`; **data** — `S16`; **infrastructure** — `I1`, `I2`. `V3` (active-speaker
detection) remains the largest single visual gap: on two-person footage we follow the largest,
most-diarisation-active face rather than the person speaking.

*Appendix B's tables were verified against v0.11.0; its "Status" section above was re-verified against
`1133a55` (V23 merged) by reading the tree and by running `scripts/check_wired.py`, not by trusting the
previous revision. The sections above Appendix B are still quoted against v0.10.0 and are not verified
— that is `clip-quality-uplift` task 13 and it remains open.*

*A note on how the "Status" section above was previously wrong, because the failure repeats: it listed
`V21`, `V24`, `AU11` and `AU12` as unbuilt after all four had merged, and it described features as
shipped that no code called. Both errors come from the same habit of updating the plan from the plan.
`scripts/check_wired.py` exists so the second class is now a failing gate rather than a prose error.*
