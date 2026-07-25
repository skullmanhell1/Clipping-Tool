# Implementation Plan — Tier 1 Creator Output Upgrade

These are incremental, test-first coding steps. Execute them **one task at a time**,
in order — each task builds on the previous ones so there is never orphaned code.
The plan intentionally lands the shared data-model/options changes first (they
unblock the API + UI), then builds each feature's **pure planning/generation
functions with their unit/property tests before** wiring them into the ffmpeg
single-pass compositor and pipeline. Every new capability defaults OFF, so at any
point an "all-new-options-off" run reproduces v0.6.0 behaviour.

Tasks marked with `*` are optional test sub-tasks (unit / property / integration
tests). Property tests use `hypothesis` with `@settings(max_examples=100)` and are
tagged `# Feature: tier1-creator-output-upgrade, Property N: <text>`, one property
per test, in the exact files named in the design's Testing Strategy. ffmpeg
integration tests reuse the existing helpers (`make_video`, `requires_ffmpeg`,
`probe_size`, `probe_duration`, `FakeWord`) and mock the LLM / `AssetProvider` /
downloader / keyframe sampler.

## Tasks

- [x] 1. Data-model, options, and config foundations
  - [x] 1.1 Extend `ProcessingOptions` and add `effective_options` in `worker/models.py`
    - Append the new fields with safe defaults: `caption_preset="karaoke"`, `caption_animation=""`, `caption_keyword_highlight=False`, `caption_keyword_ai=False`, `caption_emoji=False`, `broll=False`, `broll_intensity="standard"`, `asset_sourcing_mode="off"`, `broll_provider=""`, `selection_prompt=""`, `visual_selection=False`, `permissibility_mode=False`; keep all existing fields/defaults unchanged.
    - Extend `from_dict` bool coercion (`_as_bool`) for the new boolean flags and validate enum-like strings (`caption_preset`, `caption_animation`, `broll_intensity`, `asset_sourcing_mode`) against known values, falling back to the documented default on unknown/malformed values without raising.
    - Add `effective_options(o)` that, under `permissibility_mode`, sets `music=""` and forces `asset_sourcing_mode="local_only"`, and downgrades `local_then_external`→`local_only` when no external key is configured.
    - _Requirements: 16.1, 16.2, 16.4, 16.5, 19.1, 19.3, 22.1, 22.4, 8.4, 8.6_

  - [x] 1.2 Extend `ClipResult` and effects markers in `worker/models.py`
    - Add `broll_assets: list[dict]` (each `{provider, source_id, license, attribution, keyword, path}`) populated only for composited assets.
    - Document/define the new `effects_applied` string markers used by later tasks (`caption_preset:<name>`, `caption_preset_substituted`, `font_substituted:<name>`, `keyword_highlight`, `caption_emoji`, `broll:<keyword>`, `broll_source:local_only`, `broll_asset_failed`, `broll_license_unknown`, `broll_degraded`, `visual_selection`, `visual_degraded`).
    - _Requirements: 12.1, 12.2, 18.2, 20.1_

  - [x] 1.3 Add b-roll and visual-selection settings in `config.py`
    - Add `broll_dir`, `broll_cache_dir`, `broll_provider`, `broll_provider_api_key`, `broll_provider_base_url`, `broll_allow_download=False`, and `keyframe_sample_limit=12`.
    - Extend `ensure_local_dirs()` to create `broll_dir` and `broll_cache_dir`.
    - _Requirements: 8.1, 8.3, 12.4, 15.1, 17.4, 20.2_

  - [x]* 1.4 Property test: new option fields round-trip → `tests/test_options_roundtrip.py`
    - **Property 25: New option fields round-trip** — for any options dict, `from_dict(to_dict(...))` preserves every new field without loss.
    - _Requirements: 16.4_ · _Properties: P25_

  - [x]* 1.5 Property test: malformed/unknown values apply defaults → `tests/test_options_roundtrip.py`
    - **Property 26: Malformed or unknown option values apply documented defaults** — `from_dict` applies the documented default and does not raise.
    - _Requirements: 16.5, 22.4_ · _Properties: P26_

  - [x]* 1.6 Unit tests: defaults OFF and existing fields untouched → `tests/test_options_roundtrip.py`
    - Assert `broll`, `visual_selection`, `permissibility_mode`, external downloading, and any added audio default to disabled; assert every pre-existing field keeps its v0.6.0 default.
    - _Requirements: 16.2, 22.1_

- [x] 2. Caption preset model and keyword planner (`worker/effects/caption_presets.py`)
  - [x] 2.1 Implement the serializable preset model, registry, and resolution
    - Add `CaptionColors`, `CaptionPreset` (with `to_dict`/`from_dict`), and the `BUILTIN_PRESETS` registry expressing the three legacy templates (`karaoke`/`boxed`/`minimal`) plus new animated presets (`pop`, `typewriter`, `hormozi`).
    - Add `resolve_preset(name)` (unknown → `(karaoke, True)`) and `load_preset(data)` (name or serialized dict; malformed → karaoke fallback).
    - _Requirements: 1.1, 1.2, 1.5, 5.1, 6.1, 6.2, 6.4_

  - [x]* 2.2 Property test: built-in presets are complete → `tests/test_caption_presets.py`
    - **Property 1: Built-in presets are complete** — every registry preset defines a valid animation, font, colours, and default position.
    - _Requirements: 1.2_ · _Properties: P1_

  - [x]* 2.3 Property test: unknown/malformed presets fall back to karaoke → `tests/test_caption_presets.py`
    - **Property 2: Unknown or malformed presets fall back to karaoke** — resolution returns `karaoke` and reports a substitution.
    - _Requirements: 1.5, 6.4_ · _Properties: P2_

  - [x]* 2.4 Property test: caption preset round-trip → `tests/test_caption_presets.py`
    - **Property 3: Caption preset round-trip** — `from_dict(to_dict(p))` produces an equivalent definition.
    - _Requirements: 6.2_ · _Properties: P3_

  - [x] 2.5 Implement the pure keyword-highlight planner
    - Add `DEFAULT_STOPWORDS` and `plan_keywords(words, *, use_ai=False, client=None) -> set[int]` returning highlighted word indices from the deterministic rule set (non-stopword length threshold, ALL-CAPS, numerals/currency, high Whisper probability), merging injected-LLM selections when `use_ai` and a client are available, and returning the deterministic set only when the LLM is missing/fails.
    - _Requirements: 3.2, 3.3, 3.4, 21.1_

  - [x]* 2.6 Property test: deterministic planning + AI-unavailable equivalence → `tests/test_caption_presets.py`
    - **Property 7: Deterministic keyword planning and AI-unavailable equivalence** — deterministic result is stable and makes no LLM call; `use_ai` with no client equals the deterministic result.
    - _Requirements: 3.2, 3.4_ · _Properties: P7_

  - [x]* 2.7 Property test: AI-assisted highlighting extends the deterministic set → `tests/test_caption_presets.py`
    - **Property 8: AI-assisted highlighting extends the deterministic set** — with `use_ai` + `MockLLMClient`, the highlighted set contains the deterministic set as a subset.
    - _Requirements: 3.3_ · _Properties: P8_

  - [x]* 2.8 Unit test: highlighting disabled skips all LLM work → `tests/test_captions_templates.py`
    - Assert (via a mock LLM call-count spy) that a highlight-disabled preset makes zero LLM calls.
    - _Requirements: 3.6_

- [x] 3. Caption ASS generation extension (`worker/captions.py`)
  - [x] 3.1 Implement the pure `build_word_span` helper
    - Emit libass ASS tag spans only (no `drawtext`): `pop`/`scale` via `{\fscx..\fscy..\t(...)}`, `typewriter` via per-word `\alpha` reveal, `karaoke_fill` via `{\kfNN}`; wrap highlighted words in a distinct colour+scale span; insert inline emoji when `preset.emoji_inline` (dropping unrenderable glyphs, keeping surrounding words, locally-available glyphs only under permissibility); add a `font_available(name)` check for fallback.
    - _Requirements: 2.2, 2.3, 3.1, 4.1, 4.3, 4.4, 5.3_

  - [x] 3.2 Extend `build_ass` to consume presets, keyword indices, and position override
    - Add `preset`, `keyword_indices`, and `position` params; extend `_caption_style` to take colours/border/font/size from the preset; clamp all per-word timing to `[0, end-start]`; emit zero events (never raise) for an empty timeline; record `font_substituted:<name>` / `caption_preset_substituted` notes; preserve legacy template behaviour when no preset is supplied.
    - _Requirements: 1.1, 1.3, 2.1, 2.4, 2.5, 5.2, 5.4_

  - [x]* 3.3 Property test: per-word animation timed and bounded → `tests/test_caption_presets.py`
    - **Property 4: Per-word animation is timed to the word and bounded** — animation anchored to each word's `start`, all timestamps within `[0, D]`.
    - _Requirements: 2.1, 2.5, 21.5_ · _Properties: P4_

  - [x]* 3.4 Property test: captions use libass ASS tags only → `tests/test_caption_presets.py`
    - **Property 5: Captions use libass ASS tags only** — generated output contains no `drawtext` filter.
    - _Requirements: 2.3_ · _Properties: P5_

  - [x]* 3.5 Property test: keyword highlighting distinct and timing-preserving → `tests/test_caption_presets.py`
    - **Property 6: Keyword highlighting is visually distinct and timing-preserving** — highlighted spans carry a distinct colour/scale while spoken `start`/`end` are unchanged.
    - _Requirements: 3.1, 3.5_ · _Properties: P6_

  - [x]* 3.6 Property test: preset styling applied, position override wins → `tests/test_caption_presets.py`
    - **Property 9: Preset styling applied, position override wins** — style reflects preset font/colours/position when unoverridden; override changes alignment.
    - _Requirements: 5.1, 5.2_ · _Properties: P9_

  - [x]* 3.7 Property test: in-caption emoji respect permissibility → `tests/test_caption_presets.py`
    - **Property 11: In-caption emoji respect permissibility** — under permissibility only locally-available glyphs appear and no external download is attempted.
    - _Requirements: 4.4_ · _Properties: P11_

  - [x]* 3.8 Unit tests: legacy behaviour, emoji independence, font substitution → `tests/test_captions_templates.py`
    - Assert the three legacy templates render unchanged, in-caption emoji is independent of the overlay `emoji` effect, and a missing preset font surfaces a `font_substituted:<name>` marker while still rendering.
    - _Requirements: 1.1, 4.1, 4.2, 5.3_

  - [x]* 3.9 ffmpeg integration test: every preset/position yields a parseable ASS render → `tests/test_captions_templates.py`
    - **Property 10: Every preset/position combination yields a parseable ASS file** — using `make_video`+`requires_ffmpeg`, render a 2–3s clip for each built-in preset × position and assert output exists and `probe_size` matches target.
    - _Requirements: 1.3, 5.4_ · _Properties: P10_

- [x] 4. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 5. B-roll planning, providers, and engine (`worker/effects/broll.py`)
  - [x] 5.1 Implement data types and the pure `plan_broll_cues`
    - Add `AssetRef`, `BrollCue`, and `BROLL_INTENSITY`; implement `plan_broll_cues(words, duration, *, intensity, hold, min_gap, keyword_fn=None)` selecting key phrases into ≤N cues, each bounded to `[0, duration]`, spaced by `min_gap`, capped by intensity count/total-seconds, returning `[]` when `off`; consume the (already rebased) timeline so no cue lands in a removed interval.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 11.1, 11.2, 11.3_

  - [x] 5.2 Implement providers and `resolve_asset`
    - Add the `AssetProvider` protocol, `LocalProvider` (from `settings.broll_dir`, no network, records source path), and `ExternalProvider` (BYOK key+base_url, injectable `downloader`, records provider/source_id/license/attribution); implement `resolve_asset(keyword, mode, local, external)` with `off`/`local_only`/`local_then_external` semantics and unknown-license rejection.
    - _Requirements: 8.2, 8.3, 8.4, 8.5, 12.1, 12.2, 20.3, 21.1_

  - [x] 5.3 Implement `Broll_Engine` orchestration
    - Add `Broll_Engine(options, *, local=None, external=None)` with `plan(words, duration)` (effective-mode resolution: permissibility → local_only, missing key → local_only, off → none) and `resolve(cues)` (drop cues with no asset, failed download/decode, or unknown license; return only resolvable cues).
    - _Requirements: 8.4, 8.5, 8.6, 9.1, 9.2, 19.1, 20.3_

  - [x]* 5.4 Property test: b-roll cues well-formed and bounded → `tests/test_broll_plan.py`
    - **Property 12: B-roll cues are well-formed and bounded** — ≤1 cue per phrase, timed to source phrase, bounded to `[0, D]`, zero cues when disabled.
    - _Requirements: 7.1, 7.2, 7.3, 7.5, 21.5_ · _Properties: P12_

  - [x]* 5.5 Property test: intensity caps count and total on-screen time → `tests/test_broll_plan.py`
    - **Property 13: B-roll intensity caps count and total on-screen time** — planned cue count and summed duration stay within the intensity caps.
    - _Requirements: 7.4_ · _Properties: P13_

  - [x]* 5.6 Property test: no cue lands in a removed interval → `tests/test_broll_plan.py`
    - **Property 14: No b-roll cue lands in a removed interval** — for a rebased timeline, no cue intersects a removed interval and every cue stays within final duration.
    - _Requirements: 11.2, 11.3_ · _Properties: P14_

  - [x]* 5.7 Property test: asset-sourcing mode semantics → `tests/test_broll_plan.py`
    - **Property 15: Asset-sourcing mode semantics** — `off` calls no provider; `local_only` calls only local (never external/downloader); `local_then_external` calls external only on local miss with a key; missing key behaves as `local_only`. Use a `SpyAssetProvider` + call-recording downloader.
    - _Requirements: 8.2, 8.3, 8.4, 8.5_ · _Properties: P15_

  - [x]* 5.8 Property test: permissibility forces local-only and mutes audio → `tests/test_broll_plan.py`
    - **Property 16: Permissibility forces local-only and mutes added audio** — with permissibility enabled, `effective_options` sets `local_only` and disables audio; no music input or external download occurs.
    - _Requirements: 8.6, 19.1, 19.3_ · _Properties: P16_

  - [x]* 5.9 Property test: unusable cues dropped, others retained → `tests/test_broll_plan.py`
    - **Property 17: Unusable cues are dropped, others retained** — cues with unresolved/failed/unknown-license assets are dropped, all others kept.
    - _Requirements: 9.1, 9.2, 20.3_ · _Properties: P17_

  - [x]* 5.10 Unit tests: mode dispatch, defaults, DI, license/attribution → `tests/test_broll_plan.py`
    - Assert mode dispatch examples, b-roll defaults OFF, `broll_provider` DI wiring, unknown-license drop, and that composited external attribution is serialized onto `ClipResult.broll_assets`.
    - _Requirements: 8.1, 12.3, 12.4, 16.2, 20.2_

- [x] 6. B-roll single-pass compositor integration
  - [x] 6.1 Implement the pure `build_broll_overlay` builder in `worker/effects/broll.py`
    - Return `(input_args, filtergraph, applied_notes)` for resolved cues: images via `-loop 1 -t <dur> -i`, videos via `-i` trimmed/scaled to the window; scale to ~0.5 frame width; bound each overlay with `enable='between(t,start,end)'`; give zero-length windows a 1-frame minimum; assign input indices from `input_offset` without collision; return `([],"",[])` when there are no resolvable cues.
    - _Requirements: 10.3, 10.4, 10.5, 9.4, 12.1, 12.2_

  - [x]* 6.2 Property test: overlays bounded, uniquely indexed, layered below captions → `tests/test_broll_overlay.py`
    - **Property 20: B-roll overlays are bounded, uniquely indexed, and layered below captions** — each overlay uses `enable=between`, all input indices are distinct/contiguous, and b-roll labels precede the subtitles filter.
    - _Requirements: 10.2, 10.3, 10.4_ · _Properties: P20_

  - [x]* 6.3 Property test: only composited cues recorded with correct provenance → `tests/test_broll_overlay.py`
    - **Property 19: Only composited cues are recorded with correct provenance** — `effects_applied` b-roll markers match composited cues exactly; external assets record provider/source_id/license/attribution, local assets record source path.
    - _Requirements: 9.4, 12.1, 12.2_ · _Properties: P19_

  - [x] 6.4 Wire b-roll into `worker/effects/compositor.py` `render_clip`
    - Add a `broll_resolver=None` DI hook; insert b-roll inputs/overlays into the single `-filter_complex` with explicit input-index accounting (`base → music → b-roll → emoji`) and layer order (look chain → b-roll → subtitles → emoji → progress); catch filtergraph build errors to render b-roll-disabled (record `broll_degraded`); preserve the "return `None` when nothing changed" no-op contract.
    - _Requirements: 10.1, 10.2, 10.3, 10.6, 9.3, 17.1, 17.2, 17.3_

  - [x]* 6.5 ffmpeg integration test: single pass with b-roll + captions + emoji → `tests/test_effects_compositor.py`
    - Render a tiny clip with a mock `AssetProvider` (generated PNG/clip) + captions + emoji; spy on `_run` to assert a **single** ffmpeg invocation and assert distinct input indices in the command.
    - _Requirements: 10.1, 10.3, 17.1_

  - [x]* 6.6 ffmpeg integration test: zero resolvable cues equals b-roll disabled → `tests/test_effects_compositor.py`
    - **Property 18: Zero resolvable cues renders identically to b-roll disabled** — b-roll enabled with no resolvable assets produces the same modified/copied streams as b-roll disabled.
    - _Requirements: 9.3_ · _Properties: P18_

  - [x]* 6.7 ffmpeg integration test: stream-copy and no-op contract → `tests/test_effects_compositor.py`
    - Assert `-c:a copy` when only video changes, and `render_clip` returns `None` when every effect/feature is off.
    - _Requirements: 17.2, 17.3_

- [x] 7. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 8. Visual / prompt clip finding (`worker/visual_selection.py`)
  - [x] 8.1 Implement keyframe sampling and cue derivation
    - Add `Keyframe`, `sample_keyframes(source, total_duration, *, limit, sampler=None)` sampling ≤`limit` evenly-spaced frames **once** per source (via `fu.generate_thumbnail`, injectable sampler), and `derive_visual_cues(frames)` computing cheap brightness/motion proxies (CPU-only, no vision model).
    - _Requirements: 14.1, 15.1, 17.4, 21.1_

  - [x] 8.2 Implement `merge_scores` and the `select_moments_visual` entry point
    - Add `merge_scores(transcript_candidates, visual_frames, *, weight=0.5)` producing one ranking in `ClipCandidate` shape, and `select_moments_visual(...)` that: delegates to `sel.select_moments` when disabled; biases toward the `Selection_Prompt` when an LLM is available; falls back to transcript-only when sampling fails / provider unconfigured / no LLM; ranks on visual cues when no audio (still using embedded transcript text); snaps candidates via `sel.snap_to_sentences`; honours topic/vibe/clip_length/num_clips/range; may return `[]` on catastrophic failure.
    - _Requirements: 13.2, 13.3, 13.4, 14.2, 14.3, 14.4, 14.5, 15.2, 15.3, 15.4, 15.5_

  - [x]* 8.3 Property test: visual merge ranked, shape-preserving, snapped → `tests/test_visual_selection.py`
    - **Property 21: Visual merge yields ranked, shape-preserving, snapped candidates** — merged result ordered by combined score, each item keeps `ClipCandidate` shape, starts/ends snapped to boundaries.
    - _Requirements: 14.2, 14.4, 14.5_ · _Properties: P21_

  - [x]* 8.4 Property test: keyframe sampling is bounded → `tests/test_visual_selection.py`
    - **Property 22: Keyframe sampling is bounded** — sampled keyframe count never exceeds the configured limit.
    - _Requirements: 15.1_ · _Properties: P22_

  - [x]* 8.5 Property test: degrade to transcript-only and pass-through when disabled → `tests/test_visual_selection.py`
    - **Property 23: Visual selection degrades to transcript-only and is a pass-through when disabled** — disabled equals `select_moments`; sampling-fail / no-provider / prompt-without-LLM yield the transcript-only outcome.
    - _Requirements: 13.3, 15.2, 15.3, 15.4_ · _Properties: P23_

  - [x]* 8.6 Unit / edge tests: prompt wiring, caps, once-per-source, edge cases → `tests/test_visual_selection.py`
    - Assert `selection_prompt` field is honoured and included in the mock LLM request, `num_clips` cap holds, the sampler is called at most once (spy), no-audio ranking works, and catastrophic failure returns `[]`.
    - _Requirements: 13.1, 13.2, 13.4, 14.3, 15.5, 17.4_

- [x] 9. Pipeline wiring (`worker/pipeline.py`)
  - [x] 9.1 Swap selection to `select_moments_visual` and thread resolvers
    - Replace the `sel.select_moments(...)` call with `visual_selection.select_moments_visual(...)` (which delegates back to `select_moments` when disabled/degraded); apply `effective_options` centrally; pass the rebased `Word_Timeline` and a `broll_resolver` (built from `Broll_Engine`) into `compositor.render_clip`; record degradation markers.
    - _Requirements: 13.2, 15.4, 11.1, 18.1, 18.2, 19.1_

  - [x]* 9.2 Property test: all new features off reproduces v0.6.0 → `tests/test_pipeline_degradation.py`
    - **Property 24: All new features off reproduces v0.6.0 behaviour** — with every new option default/off, output and `effects_applied` match pre-feature behaviour, `render_clip` returns `None` when no legacy effect is on, and unmodified streams are stream-copied.
    - _Requirements: 16.3, 17.2, 17.3, 22.2_ · _Properties: P24_

  - [x]* 9.3 Property test: missing dependencies still produce clips and record degradation → `tests/test_pipeline_degradation.py`
    - **Property 27: Missing dependencies still produce clips and record degradation** — any combination of missing LLM/key/asset/sampler/font/filtergraph still produces clips and records the degradation.
    - _Requirements: 18.1, 18.2, 18.4_ · _Properties: P27_

  - [x]* 9.4 Property test: no external network when external features disabled → `tests/test_pipeline_degradation.py`
    - **Property 28: No external network when external features are disabled** — with all external-download/provider features disabled, no downloader/network call occurs (assert via injected recording downloader).
    - _Requirements: 18.3_ · _Properties: P28_

- [x] 10. API surface (`api/main.py`)
  - [x] 10.1 Extend `/api/info` with the new option lists
    - Add `caption_presets`, `caption_animations`, `asset_sourcing_modes`, `broll_intensities`, `broll_providers`, and `broll_available` to the `effects` block while retaining all existing lists.
    - _Requirements: 1.4, 8.7, 22.3_

  - [x] 10.2 Extend `OptionsModel` and `POST /api/upload` Form fields
    - Add the new fields to `OptionsModel` with identical defaults and matching `Form(...)` parameters (`caption_preset`, `caption_animation`, `caption_keyword_highlight`, `caption_keyword_ai`, `caption_emoji`, `broll`, `broll_intensity`, `asset_sourcing_mode`, `broll_provider`, `selection_prompt`, `visual_selection`, `permissibility_mode`), threaded into `ProcessingOptions.from_dict`; leave existing fields untouched.
    - _Requirements: 16.1, 22.1_

  - [x]* 10.3 Unit tests: `/api/info` superset and option passthrough → `tests/test_api.py`
    - Assert `/api/info` advertises the new preset + sourcing-mode lists in addition to all existing values, and that upload Form fields reach `from_dict`.
    - _Requirements: 1.4, 8.7, 22.3_

- [x] 11. Frontend wiring
  - [x] 11.1 Extend `frontend/src/App.jsx` defaults and `toOptions`
    - Add the new keys to `DEFAULT_SETTINGS` (all defaulting OFF / `karaoke`) and map them into the request body in `toOptions`.
    - _Requirements: 16.1, 16.4, 6.3_

  - [x] 11.2 Add controls to `frontend/src/components/SettingsPanel.jsx`
    - Add a caption-preset dropdown + keyword-highlight / AI-highlight / in-caption-emoji toggles; a b-roll section (enable, intensity, asset-sourcing-mode, provider); a selection-prompt textarea + visual-selection toggle; and a permissibility-mode toggle, populated from `/api/info`.
    - _Requirements: 16.1, 6.3_

- [x] 12. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [x] 13. Version, changelog, and README for the 0.7.0 release
  - [x] 13.1 Bump `VERSION` to `0.7.0` and document the release
    - Update `VERSION`; add a `## [0.7.0]` "Added — Tier 1 Creator Output Upgrade" section to `CHANGELOG.md` (animated caption presets, b-roll auto-insertion, prompt/visual clip finding, permissibility mode); update `README.md` feature/options docs consistent with prior phases.
    - _Requirements: 22.1, 22.3_

## Notes

- Tasks marked with `*` are optional test sub-tasks (unit / property / integration) and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each task references the specific requirement numbers it satisfies, and property-test tasks cite the design property (P1–P28) they implement.
- Ordering is test-first and dependency-safe: shared data-model/options/config land first (unblocking API + UI), then pure planning/generation functions and their tests precede the ffmpeg single-pass wiring, and the pipeline swap comes after all underlying modules exist — so no code is orphaned and an "all-off" run always reproduces v0.6.0.
- Property tests use `hypothesis` (`@settings(max_examples=100)`), one property per test, tagged `# Feature: tier1-creator-output-upgrade, Property N: ...`, in the exact files named in the design's Testing Strategy.
- ffmpeg integration tests reuse `make_video`, `requires_ffmpeg`, `probe_size`, `probe_duration`, and `FakeWord`, and mock the LLM (`MockLLMClient`), `AssetProvider`, downloader, and keyframe sampler so the suite stays fast and offline.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "1.3", "2.1"] },
    { "id": 1,  "tasks": ["1.2", "2.5", "5.1", "8.1"] },
    { "id": 2,  "tasks": ["3.1", "5.2", "8.2", "1.4"] },
    { "id": 3,  "tasks": ["3.2", "5.3", "1.5", "2.2", "8.3"] },
    { "id": 4,  "tasks": ["6.1", "1.6", "2.3", "8.4", "5.4", "10.1"] },
    { "id": 5,  "tasks": ["6.4", "2.4", "8.5", "5.5", "10.2", "6.2"] },
    { "id": 6,  "tasks": ["9.1", "2.6", "8.6", "5.6", "6.3", "10.3"] },
    { "id": 7,  "tasks": ["6.5", "2.7", "5.7", "9.2", "11.1"] },
    { "id": 8,  "tasks": ["6.6", "2.8", "5.8", "9.3", "11.2", "3.3"] },
    { "id": 9,  "tasks": ["6.7", "3.8", "5.9", "9.4", "3.4"] },
    { "id": 10, "tasks": ["3.9", "5.10", "3.5"] },
    { "id": 11, "tasks": ["3.6"] },
    { "id": 12, "tasks": ["3.7"] },
    { "id": 13, "tasks": ["13.1"] }
  ]
}
```
