# Requirements Document

## Introduction

This spec defines **Campaign Briefs** — an incremental enhancement to the AI Video
Clipper (self-hosted, CPU-first, currently **v0.8.0**).

Paid clipping campaigns on Whop communities come with a written **brief**: the
free-form clipping requirements a whop owner publishes for clippers to follow. A
typical brief reads:

> "30–60s vertical clips, captions required, no background music, tag @brandname,
> include the link in the description, post to TikTok and YouTube Shorts, max 3
> clips/day, don't use profanity, hook in the first 3 seconds."

Today a clipper reads that by hand, translates it into settings in the Settings
panel, and eyeballs each finished clip for compliance. This feature closes that
loop with three cooperating capabilities:

1. **Brief ingestion** — capture the brief text from one of three sources: pasted
   text (the primary path, always available), an optional URL fetch, or an
   optional read through the existing `@whop/sdk` Node bridge. Briefs are stored,
   named, and selectable; one may be marked active.
2. **Brief parsing** — convert free-form brief text into a structured
   `Parsed_Requirements` record of discrete `Requirement_Rule`s using the existing
   pluggable LLM client (`worker/llm_client.py`), with a **deterministic
   non-LLM extractor** as a fallback so the feature works with no LLM key and no
   network.
3. **Settings mapping and compliance checking** — turn the parsed rules into a
   **proposed** settings profile (reusing the existing `profiles.py` store and the
   profiles UI) that the user reviews before it applies, and evaluate every
   produced clip against the brief's checkable rules, reporting per-rule
   pass/fail/unknown with a reason, surfaced per clip in the UI and recorded in
   the publish history.

The feature MUST preserve the product's established design values, which are
treated as hard constraints throughout this document:

- **Toggleable and default OFF/absent** — with no brief configured and the
  feature off, behaviour is exactly v0.8.0: same options, same clips, same
  `effects_applied`, same publish flow.
- **Graceful degradation is mandatory** — no LLM key → deterministic extraction;
  no network or no Whop permission → paste-only; unparseable brief → surface what
  *was* understood and mark the rest advisory. A brief never fails a job and
  never blocks clip creation.
- **BYOK / self-hosted / offline-friendly** — no mandatory external network call.
  The Whop read path reuses the existing `settings.whop_api_key` plus the
  `publisher_bridge/whop.mjs` Node bridge pattern (as in `publishers/whop.py`),
  and is optional and dependency-injected.
- **`permissibility_mode` is honoured** — when enabled, no external fetch of any
  kind (no URL fetch, no Whop API read): paste-only. A brief can never re-enable
  music, external downloading, or external sourcing against permissibility mode.
- **Reuse, don't duplicate** — build on `profiles.py` + `ProfilesBar.jsx`,
  `worker/llm_client.py` (`MockLLMClient`, `set_llm_client`), `publishers/manager.py`
  + `publishers/history.py` (SQLite) for publish-time behaviour and recording, and
  `api/main.py` + `frontend/src/` for surface. `ProcessingOptions` in
  `worker/models.py` is the settings vocabulary a brief maps onto.
- **Untrusted input** — brief text is third-party content fed to an LLM and mapped
  onto settings. It is treated as data, never as instructions to the Clipper.

**Three open decisions** are called out in the "Open Decisions" section at the end
of this document. Each has a proposed default that is already encoded in the
acceptance criteria below and needs user confirmation before design.

## Glossary

- **Clipper**: The overall AI Video Clipper application (self-hosted, ffmpeg-based, CPU-first).
- **Pipeline**: The per-source flow in `worker/pipeline.py` (probe → transcribe → selection → per clip: cut → filler removal → geometry → compositor → thumbnail).
- **Brief**: A stored record of the clipping requirements text published for a paid clipping campaign, plus its provenance metadata (`id`, `name`, `text`, `Brief_Source`, `fetched_at`, `is_active`).
- **Brief_Text**: The raw, unmodified free-form text of a Brief as ingested.
- **Brief_Source**: The origin of a Brief_Text: `paste` (primary), `url` (optional), or `whop` (optional, via the Node bridge).
- **Brief_Ingestor**: The subsystem that obtains Brief_Text from a Brief_Source and stores a Brief.
- **Brief_Fetcher**: The injectable HTTP fetch dependency used by the `url` Brief_Source.
- **Whop_Brief_Reader**: The injectable dependency that reads campaign/brief content through the existing `@whop/sdk` Node bridge (`publisher_bridge/whop.mjs`) using `settings.whop_api_key`.
- **Brief_Parser**: The subsystem that converts Brief_Text into Parsed_Requirements. Uses the LLM_Client when available, otherwise the Deterministic_Extractor.
- **LLM_Client**: The existing pluggable client from `worker/llm_client.py` (`get_llm_client`, `set_llm_client`, `MockLLMClient`, `llm_available`).
- **Deterministic_Extractor**: The offline, non-LLM, rule-based (regex/keyword) extractor that produces Parsed_Requirements without any model call.
- **Parsed_Requirements**: The structured, serialisable result of parsing one Brief: an ordered list of Requirement_Rules plus parse provenance (`parser` used, `unparsed_text` remainder, `warnings`).
- **Requirement_Rule**: One discrete requirement extracted from a Brief: `{rule_id, kind, operator, value, source_text, checkable, confidence, advisory}`.
- **Rule_Kind**: The classified type of a Requirement_Rule, drawn from a closed set (e.g. `duration_min`, `duration_max`, `aspect`, `captions_required`, `music_prohibited`, `platforms`, `required_mention`, `required_hashtag`, `required_link`, `clips_per_day_max`, `hook_within_seconds`, `prohibited_content`, `other`).
- **Checkable_Rule**: A Requirement_Rule whose satisfaction the Compliance_Checker can determine mechanically from clip data (e.g. `duration_max`, `aspect`, `captions_required`).
- **Advisory_Rule**: A Requirement_Rule that is retained and displayed for the user but is not mechanically evaluated (e.g. `prohibited_content`, `other`), always reported as `unknown`.
- **Mappable_Setting**: A `ProcessingOptions` / profile settings field that appears on the Mapping_Allowlist and may therefore be pre-filled from a Brief.
- **Mapping_Allowlist**: The closed, code-defined set of Mappable_Settings a Brief is permitted to influence.
- **Brief_Profile**: A **proposed** settings profile derived from Parsed_Requirements, expressed in the existing `profiles.py` `Profile` shape (`settings` + `publishing` blobs), presented for user review before being applied.
- **Profile_Store**: The existing saved-settings store (`profiles.py` `ProfileStore`, persisted at `settings.profiles_path`).
- **Processing_Options**: The user options record (`worker/models.py` `ProcessingOptions`, mirrored by `OptionsModel`, the `/api/upload` Form fields, `App.jsx` defaults/`toOptions`, and `SettingsPanel.jsx`).
- **Compliance_Checker**: The subsystem that evaluates a produced clip against the Checkable_Rules of a Brief.
- **Compliance_Rule_Result**: The outcome for one Requirement_Rule against one clip: `{rule_id, kind, status, reason, observed, expected}` where `status` is `pass`, `fail`, or `unknown`.
- **Compliance_Report**: The full set of Compliance_Rule_Results for one clip against one Brief, plus a rolled-up `overall` status and the Brief identifier.
- **Compliance_Status**: The rolled-up status of a Compliance_Report: `pass` (no `fail`), `fail` (at least one `fail`), or `unknown` (no `fail` and at least one `unknown`, with no evaluated `pass`).
- **Publish_Manager**: The existing scheduler/throttled publish worker (`publishers/manager.py`).
- **History_Store**: The existing SQLite history store (`publishers/history.py` `HistoryStore`).
- **Info_Endpoint**: The `/api/info` endpoint advertising available option values and capabilities to the UI.
- **Effects_Applied**: The free-form `ClipResult.effects_applied` string markers recording which optional capabilities ran and how they degraded.
- **Degraded_Mode**: Operation when an optional dependency (LLM, network, Whop bridge) is unavailable; the feature falls back along its degradation chain and the Pipeline still produces clips.
- **Permissibility_Mode**: The existing `permissibility_mode` setting that forces local-only sourcing, disables added music, and blocks external downloads (see `effective_options` in `worker/models.py`).
- **BYOK**: "Bring your own key" — the self-hosted model in which the operator supplies any external provider credentials.

## Requirements

---

## Group A — Brief Ingestion

### Requirement 1: Paste ingestion (primary path)

**User Story:** As a clipper working a paid campaign, I want to paste the whop's brief text straight into the tool, so that the requirements are captured without depending on any network or API.

#### Acceptance Criteria

1. THE Brief_Ingestor SHALL accept Brief_Text supplied directly as text and SHALL store it as a Brief with Brief_Source `paste`.
2. THE Brief_Ingestor SHALL preserve the ingested Brief_Text unmodified in the stored Brief.
3. THE Brief_Ingestor SHALL ingest a `paste` Brief without any network access and without any configured credential.
4. WHEN a Brief is stored, THE Brief_Ingestor SHALL record its `id`, `name`, `Brief_Source`, and ingestion timestamp.
5. IF supplied Brief_Text is empty or consists only of whitespace, THEN THE Brief_Ingestor SHALL reject the ingestion, SHALL return a reason, and SHALL leave stored Briefs unchanged.
6. THE Brief_Ingestor SHALL cap stored Brief_Text at a configurable maximum character length and SHALL truncate longer text at that limit while recording that truncation occurred.

### Requirement 2: Optional URL ingestion

**User Story:** As a clipper, I want to point the tool at the URL where the brief is published, so that I do not have to copy text by hand.

#### Acceptance Criteria

1. WHERE URL ingestion is enabled, THE Brief_Ingestor SHALL accept a URL and SHALL obtain Brief_Text through the injected Brief_Fetcher, storing the result with Brief_Source `url`.
2. THE Brief_Ingestor SHALL extract human-readable text from a fetched HTML document before storing it as Brief_Text.
3. THE Brief_Ingestor SHALL record the source URL and the fetch timestamp on a Brief ingested with Brief_Source `url`.
4. THE Brief_Fetcher SHALL apply a configurable request timeout and a configurable maximum response size.
5. IF the Brief_Fetcher returns an error, times out, exceeds the maximum response size, or yields no readable text, THEN THE Brief_Ingestor SHALL report the failure with a reason and SHALL direct the user to the `paste` path, and SHALL leave stored Briefs unchanged.
6. THE Clipper SHALL default URL ingestion to disabled.

### Requirement 3: Optional Whop read (unverified availability)

**User Story:** As an operator who already configured a Whop API key for publishing, I want the tool to try reading the campaign brief from Whop directly, so that ingestion is one click when it is possible at all.

#### Acceptance Criteria

1. WHERE Whop ingestion is enabled AND `settings.whop_api_key` is configured AND the Node bridge script is present, THE Brief_Ingestor SHALL request Brief_Text through the injected Whop_Brief_Reader and SHALL store the result with Brief_Source `whop`.
2. THE Whop_Brief_Reader SHALL reuse the existing Node bridge invocation pattern of `publishers/whop.py`, passing `settings.whop_api_key` through the subprocess environment rather than through arguments.
3. THE Whop_Brief_Reader SHALL expose a capability/status report stating whether Whop brief reading is configured and available, mirroring the existing `PublisherStatus` reporting pattern.
4. IF the Whop_Brief_Reader reports the capability unavailable, returns an authorisation or permission error, returns no brief content, or fails for any reason, THEN THE Brief_Ingestor SHALL report the failure with a reason, SHALL direct the user to the `paste` path, and SHALL leave stored Briefs unchanged.
5. THE Brief_Ingestor SHALL apply a bounded timeout to every Whop_Brief_Reader invocation.
6. THE Clipper SHALL default Whop ingestion to disabled.
7. THE Clipper SHALL keep every capability of this feature available when the Whop read path is unavailable, using the `paste` path.

### Requirement 4: Brief storage, selection, and source precedence

**User Story:** As a clipper running more than one campaign, I want several briefs stored and selectable, so that each job is checked against the right campaign's requirements.

#### Acceptance Criteria

1. THE Clipper SHALL store multiple Briefs, each with a unique `id` and a user-editable `name`.
2. THE Clipper SHALL allow at most one stored Brief to be marked active at any time.
3. WHEN a Brief is marked active, THE Clipper SHALL clear the active mark from every other stored Brief.
4. THE Clipper SHALL allow a job to reference a specific Brief `id`, and WHERE a job references no Brief `id`, THE Clipper SHALL use the active Brief.
5. WHERE a job references no Brief `id` AND no Brief is active, THE Pipeline SHALL run with no brief applied.
6. WHEN a single ingestion request supplies more than one Brief_Source, THE Brief_Ingestor SHALL apply the precedence `paste` > `url` > `whop` and SHALL record which Brief_Source was used.
7. THE Clipper SHALL support renaming and deleting a stored Brief.
8. WHEN the active Brief is deleted, THE Clipper SHALL leave no Brief active and SHALL continue to process jobs with no brief applied.
9. THE Clipper SHALL persist stored Briefs across process restarts.

---

## Group B — Brief Parsing

### Requirement 5: LLM-assisted parsing

**User Story:** As a clipper, I want the brief's prose turned into a structured list of requirements, so that the tool can act on it.

#### Acceptance Criteria

1. WHERE the LLM_Client is available, THE Brief_Parser SHALL parse Brief_Text into Parsed_Requirements using the LLM_Client's JSON completion interface.
2. THE Brief_Parser SHALL accept a dependency-injected LLM_Client so that tests can supply a `MockLLMClient`.
3. THE Brief_Parser SHALL classify every produced Requirement_Rule into a Rule_Kind from the closed Rule_Kind set, assigning `other` when no specific kind applies.
4. FOR every produced Requirement_Rule, THE Brief_Parser SHALL record the `source_text` span of the Brief_Text the rule was derived from.
5. FOR every produced Requirement_Rule, THE Brief_Parser SHALL record whether the rule is a Checkable_Rule or an Advisory_Rule.
6. THE Brief_Parser SHALL record which parser produced the result (`llm` or `deterministic`) in the Parsed_Requirements provenance.
7. IF the LLM_Client raises an error, returns unparseable output, or returns rules that fail validation against the Parsed_Requirements schema, THEN THE Brief_Parser SHALL fall back to the Deterministic_Extractor and SHALL record the degradation.
8. THE Brief_Parser SHALL discard any LLM-produced rule whose Rule_Kind is outside the closed Rule_Kind set and SHALL retain the remaining valid rules.

### Requirement 6: Deterministic non-LLM extraction

**User Story:** As a self-hosted operator with no LLM key, I want the brief still understood, so that the feature is useful entirely offline.

#### Acceptance Criteria

1. WHEN no LLM_Client is available, THE Brief_Parser SHALL produce Parsed_Requirements using only the Deterministic_Extractor.
2. THE Deterministic_Extractor SHALL extract duration bounds from Brief_Text expressed as ranges and limits (for example "30–60s", "under 60 seconds", "at least 45s", "1–3 min").
3. THE Deterministic_Extractor SHALL extract aspect-ratio requirements from Brief_Text, including the keyword forms "vertical", "square", and "horizontal" and the ratio forms accepted by Processing_Options.
4. THE Deterministic_Extractor SHALL extract a `captions_required` rule from caption keywords (for example "captions required", "subtitles", "hard-coded captions").
5. THE Deterministic_Extractor SHALL extract a `music_prohibited` rule from music-prohibition keywords (for example "no background music", "no music").
6. THE Deterministic_Extractor SHALL extract platform requirements from platform names matching the Clipper's known publisher and platform vocabulary.
7. THE Deterministic_Extractor SHALL extract required mentions from `@handle` tokens and required hashtags from `#tag` tokens.
8. THE Deterministic_Extractor SHALL extract a `clips_per_day_max` rule from per-day clip-count limits (for example "max 3 clips/day", "up to 5 clips per day").
9. THE Deterministic_Extractor SHALL extract a `required_link` rule from link-requirement phrasing referring to the description or caption.
10. THE Deterministic_Extractor SHALL extract a `hook_within_seconds` rule from hook-timing phrasing (for example "hook in the first 3 seconds").
11. THE Deterministic_Extractor SHALL operate without any network access and without any LLM call.
12. THE Deterministic_Extractor SHALL produce the same Parsed_Requirements for the same Brief_Text on every invocation.
13. WHEN the Deterministic_Extractor recognises no rule in the Brief_Text, THE Brief_Parser SHALL produce Parsed_Requirements containing zero Requirement_Rules and SHALL retain the full Brief_Text as `unparsed_text`.

### Requirement 7: Parsed requirements model and round-trip

**User Story:** As an operator, I want parsed brief data to serialise and reload exactly, so that stored briefs, job records, and compliance reports reproduce the same rules.

#### Acceptance Criteria

1. THE Clipper SHALL represent Parsed_Requirements and each Requirement_Rule as serialisable records with stable field names.
2. FOR every Parsed_Requirements value, serialising the value and then parsing the serialised form SHALL produce an equivalent Parsed_Requirements value (round-trip property).
3. THE Clipper SHALL assign each Requirement_Rule a `rule_id` that is unique within its Parsed_Requirements.
4. THE Clipper SHALL preserve the order of Requirement_Rules through serialisation and parsing.
5. IF a serialised Requirement_Rule record is malformed or carries an unknown Rule_Kind, THEN THE Clipper SHALL discard that record and SHALL retain the remaining valid records.
6. THE Clipper SHALL parse a Parsed_Requirements record written by an earlier version without raising, applying documented defaults for absent fields.
7. FOR every Requirement_Rule with numeric bounds, THE Clipper SHALL ensure a minimum bound is less than or equal to its corresponding maximum bound, and IF the bounds conflict, THEN THE Brief_Parser SHALL mark the affected rules as Advisory_Rules and SHALL record a warning.

### Requirement 8: Ambiguity, partial understanding, and never failing

**User Story:** As a clipper with an unusually worded brief, I want to see exactly what the tool understood and what it did not, so that I can trust it and fill the gaps myself.

#### Acceptance Criteria

1. THE Brief_Parser SHALL retain every portion of Brief_Text it produced no Requirement_Rule from as `unparsed_text` in the Parsed_Requirements.
2. THE Clipper SHALL present the Requirement_Rules that were understood alongside the `unparsed_text` and any parse warnings.
3. WHEN a Requirement_Rule cannot be classified as a Checkable_Rule, THE Brief_Parser SHALL mark the rule as an Advisory_Rule.
4. FOR every produced Requirement_Rule, THE Brief_Parser SHALL record a confidence value.
5. WHERE a Requirement_Rule's confidence is below a configurable threshold, THE Clipper SHALL mark the rule as an Advisory_Rule and SHALL exclude it from settings mapping.
6. IF parsing fails entirely for any reason, THEN THE Pipeline SHALL continue processing the job with no brief applied and SHALL record the degradation in Effects_Applied.
7. THE Brief_Parser SHALL complete parsing of a Brief without raising to the Pipeline for every Brief_Text input, including empty, non-textual, and adversarial input.

---

## Group C — Mapping a Brief onto Settings

### Requirement 9: Proposed brief profile with user review

**User Story:** As a clipper, I want the brief to pre-fill my clip settings for review, so that configuring a campaign is fast but still mine to confirm.

#### Acceptance Criteria

1. WHERE brief-to-settings mapping is requested, THE Clipper SHALL derive a Brief_Profile from the Parsed_Requirements in the existing Profile_Store `Profile` shape (`settings` and `publishing` blobs).
2. THE Clipper SHALL present the Brief_Profile as a proposal that the user reviews and explicitly applies.
3. THE Clipper SHALL NOT alter any stored Profile or any in-flight Processing_Options value until the user applies the Brief_Profile.
4. FOR every proposed settings change, THE Clipper SHALL present the current value, the proposed value, and the `source_text` of the Requirement_Rule that motivated the change.
5. THE Clipper SHALL allow the user to edit or reject any individual proposed settings change before applying the Brief_Profile.
6. WHEN the user applies a Brief_Profile, THE Clipper SHALL persist it through the existing Profile_Store save path.
7. WHEN a Brief_Profile is applied, THE Clipper SHALL record the Brief `id` it was derived from on the saved Profile.
8. WHEN a Brief_Profile proposes no settings change, THE Clipper SHALL report that no change was proposed and SHALL leave stored Profiles unchanged.

### Requirement 10: Mapping allowlist

**User Story:** As an operator, I want a brief able to touch only a fixed, safe set of settings, so that third-party text can never reconfigure my installation.

#### Acceptance Criteria

1. THE Clipper SHALL define a closed, code-defined Mapping_Allowlist of Mappable_Settings that a Brief may influence.
2. THE Mapping_Allowlist SHALL include the clip-length/duration settings, the aspect setting, the captions setting, the music setting, the target-platform settings, and the required mention/hashtag metadata settings of Processing_Options.
3. THE Mapping_Allowlist SHALL exclude `permissibility_mode`, `asset_sourcing_mode`, `broll_provider`, every publisher credential, every publisher target identifier, `publish_mode`, `publish_to` account/target routing, `schedule_at`, and every storage, retention, and runtime-configuration setting.
4. FOR every proposed settings change, THE Clipper SHALL verify the target field is on the Mapping_Allowlist before including the change in a Brief_Profile.
5. IF a Requirement_Rule would map to a field outside the Mapping_Allowlist, THEN THE Clipper SHALL discard that mapping, SHALL retain the rule as an Advisory_Rule, and SHALL record the rejection.
6. THE Clipper SHALL validate every mapped value against the known value set of its target Processing_Options field, and IF a mapped value is invalid, THEN THE Clipper SHALL discard that mapping and SHALL retain the rule as an Advisory_Rule.
7. THE Clipper SHALL map a `platforms` Requirement_Rule only onto platforms the Clipper already supports, and SHALL retain unsupported platform names as Advisory_Rules.

### Requirement 11: Composition with existing cross-cutting rules

**User Story:** As an operator with permissibility mode on, I want brief-derived settings to compose correctly with the rules I already set, so that a brief can only ever be more restrictive, never less.

#### Acceptance Criteria

1. WHERE Permissibility_Mode is enabled, THE Clipper SHALL apply the existing `effective_options` normalisation after any Brief_Profile is applied, so that added music stays disabled and asset sourcing stays local-only.
2. THE Clipper SHALL treat a `music_prohibited` Requirement_Rule as setting music off, and SHALL NOT allow any Requirement_Rule to enable music.
3. WHERE Permissibility_Mode is enabled AND a Requirement_Rule would enable added music, THE Clipper SHALL discard that mapping and SHALL retain the rule as an Advisory_Rule.
4. THE Clipper SHALL NOT allow any Requirement_Rule to disable Permissibility_Mode, enable external downloading, or widen asset sourcing.
5. WHEN a brief-derived duration bound conflicts with an existing user-selected clip-length setting, THE Clipper SHALL present both values in the Brief_Profile review and SHALL apply the brief value only if the user accepts that change.
6. WHEN a Brief_Profile is applied, THE Clipper SHALL leave every Processing_Options field outside the Mapping_Allowlist at its pre-existing value.

---

## Group D — Compliance Checking

### Requirement 12: Per-rule compliance evaluation

**User Story:** As a clipper, I want each finished clip checked against the brief rule by rule, so that I know what to fix before I submit.

#### Acceptance Criteria

1. WHERE compliance checking is enabled AND a Brief applies to the job, THE Compliance_Checker SHALL produce a Compliance_Report for every produced clip.
2. FOR every Requirement_Rule in the applicable Parsed_Requirements, THE Compliance_Report SHALL contain exactly one Compliance_Rule_Result.
3. THE Compliance_Checker SHALL assign each Compliance_Rule_Result a status of `pass`, `fail`, or `unknown`.
4. FOR every Compliance_Rule_Result, THE Compliance_Checker SHALL record a human-readable reason, the observed value, and the expected value.
5. THE Compliance_Checker SHALL evaluate `duration_min` and `duration_max` rules against the produced clip's duration.
6. THE Compliance_Checker SHALL evaluate an `aspect` rule against the produced clip's rendered aspect ratio.
7. THE Compliance_Checker SHALL evaluate a `captions_required` rule against whether captions were rendered onto the clip.
8. THE Compliance_Checker SHALL evaluate a `music_prohibited` rule against whether added music was mixed into the clip.
9. THE Compliance_Checker SHALL evaluate `required_mention`, `required_hashtag`, and `required_link` rules against the clip's title, description, hashtags, and mentions metadata.
10. THE Compliance_Checker SHALL evaluate a `platforms` rule against the clip's configured publish targets.
11. FOR every Advisory_Rule, THE Compliance_Checker SHALL report status `unknown` with a reason stating the rule requires human judgement.
12. IF the data needed to evaluate a Checkable_Rule is unavailable, THEN THE Compliance_Checker SHALL report status `unknown` with a reason naming the missing data.
13. THE Compliance_Checker SHALL derive the Compliance_Report's rolled-up Compliance_Status as `fail` when any Compliance_Rule_Result is `fail`, otherwise `pass` when at least one result is `pass` and none is `fail`, otherwise `unknown`.
14. THE Compliance_Checker SHALL evaluate rules without any network access.
15. WHEN a Brief's Parsed_Requirements contains zero Requirement_Rules, THE Compliance_Checker SHALL produce a Compliance_Report with zero Compliance_Rule_Results and Compliance_Status `unknown`.

### Requirement 13: Compliance never blocks clip creation

**User Story:** As a clipper, I want clips produced regardless of compliance outcome, so that a brief rule I disagree with never costs me the render.

#### Acceptance Criteria

1. THE Pipeline SHALL produce every selected clip regardless of its Compliance_Report outcome.
2. IF the Compliance_Checker fails for any reason, THEN THE Pipeline SHALL complete the job, SHALL omit the Compliance_Report for the affected clip, and SHALL record the degradation in Effects_Applied.
3. WHEN compliance checking runs for a clip, THE Clipper SHALL record a marker identifying the applied Brief and the rolled-up Compliance_Status in Effects_Applied.
4. THE Compliance_Checker SHALL add no ffmpeg pass to the Pipeline.
5. WHEN compliance checking is disabled or no Brief applies, THE Pipeline SHALL perform no compliance evaluation for that job.

### Requirement 14: Per-day clip-count cap

**User Story:** As a clipper on a campaign with a daily submission cap, I want to be told when I have hit the cap, so that I do not waste submissions.

#### Acceptance Criteria

1. WHERE a `clips_per_day_max` Requirement_Rule applies, THE Compliance_Checker SHALL count the clips already published for that Brief within the applicable day window using the History_Store.
2. THE Compliance_Checker SHALL define the day window as a fixed 24-hour boundary in a configurable time zone, defaulting to UTC.
3. WHEN the counted clips for the day window are fewer than the `clips_per_day_max` value, THE Compliance_Checker SHALL report the rule as `pass` including the counted value and the cap.
4. WHEN the counted clips for the day window are greater than or equal to the `clips_per_day_max` value, THE Compliance_Checker SHALL report the rule as `fail` including the counted value and the cap.
5. THE Compliance_Checker SHALL count only publish attempts recorded as successfully published against that Brief when evaluating the cap.
6. IF the History_Store cannot be read, THEN THE Compliance_Checker SHALL report the `clips_per_day_max` rule as `unknown` with a reason and SHALL NOT fail the job.

### Requirement 15: Publish gating policy

**User Story:** As a clipper, I want to be warned about brief violations before publishing, and optionally to have publishing blocked, so that I control how strict the tool is.

#### Acceptance Criteria

1. WHEN a publish is requested for a clip whose Compliance_Status is `fail`, THE Clipper SHALL report the failing Compliance_Rule_Results with the publish response.
2. THE Clipper SHALL default publish gating to warn-only, allowing the publish to proceed after reporting the failing results.
3. THE Processing_Options SHALL expose a "block publishing on brief violation" toggle, and THE Clipper SHALL default that toggle to disabled.
4. WHERE the block-publishing toggle is enabled AND a clip's Compliance_Status is `fail`, THE Publish_Manager SHALL decline to submit that clip's publish attempt and SHALL report the failing Compliance_Rule_Results as the reason.
5. WHERE the block-publishing toggle is enabled AND a clip's Compliance_Status is `unknown`, THE Publish_Manager SHALL allow the publish to proceed and SHALL report the unknown results.
6. WHERE the block-publishing toggle is enabled, THE Clipper SHALL allow the user to override the block for a specific clip through an explicit acknowledgement, and SHALL record that override.
7. WHEN compliance checking is disabled or no Brief applies, THE Publish_Manager SHALL behave exactly as in v0.8.0.

### Requirement 16: Recording compliance results

**User Story:** As an operator, I want compliance outcomes recorded alongside publish history, so that I can audit what was submitted against which brief.

#### Acceptance Criteria

1. WHEN a Compliance_Report is produced for a clip, THE Clipper SHALL persist the report through the History_Store, associated with the job identifier, the clip identifier, and the Brief identifier.
2. THE History_Store SHALL persist the rolled-up Compliance_Status and the serialised Compliance_Rule_Results for each recorded Compliance_Report.
3. THE Clipper SHALL extend the History_Store schema additively, so that an existing history database continues to be readable after the upgrade.
4. WHEN a publish attempt is created for a clip with a Compliance_Report, THE Clipper SHALL record the Brief identifier and the Compliance_Status on that attempt.
5. WHEN a publish is permitted despite a `fail` Compliance_Status, THE Clipper SHALL record that the violation was accepted.
6. THE `GET /api/history` response SHALL include the recorded Compliance_Status for each clip that has one.
7. IF persisting a Compliance_Report fails, THEN THE Clipper SHALL continue the job and the publish flow and SHALL record the degradation.

---

## Group E — Cross-Cutting: Security, Surface, Compatibility, Testability

### Requirement 17: Permissibility mode behaviour

**User Story:** As an operator with a permissibility preference, I want brief handling to stay fully local, so that no external call is made on my behalf.

#### Acceptance Criteria

1. WHERE Permissibility_Mode is enabled, THE Brief_Ingestor SHALL accept only Brief_Source `paste`.
2. WHERE Permissibility_Mode is enabled, THE Brief_Ingestor SHALL NOT invoke the Brief_Fetcher and SHALL NOT invoke the Whop_Brief_Reader.
3. WHEN a `url` or `whop` ingestion is requested WHILE Permissibility_Mode is enabled, THE Brief_Ingestor SHALL decline the request, SHALL state that Permissibility_Mode restricts ingestion to pasted text, and SHALL leave stored Briefs unchanged.
4. WHERE Permissibility_Mode is enabled, THE Brief_Parser SHALL use the Deterministic_Extractor and SHALL NOT invoke the LLM_Client.
5. THE Clipper SHALL produce Parsed_Requirements, Brief_Profiles, and Compliance_Reports under Permissibility_Mode without any external network access.

### Requirement 18: Untrusted input and prompt-injection resistance

**User Story:** As an operator, I want brief text treated as untrusted data, so that a hostile brief cannot reconfigure, exfiltrate from, or act on my installation.

#### Acceptance Criteria

1. THE Brief_Parser SHALL pass Brief_Text to the LLM_Client as delimited data within a fixed system prompt that instructs the model to extract requirements only.
2. THE Brief_Parser SHALL accept only LLM output that validates against the Parsed_Requirements schema and SHALL discard every other field the model returns.
3. THE Clipper SHALL apply settings changes derived from a Brief only through the Mapping_Allowlist.
4. THE Clipper SHALL ignore any instruction contained in Brief_Text that requests enabling external downloading, widening asset sourcing, or disabling Permissibility_Mode.
5. THE Clipper SHALL ignore any instruction contained in Brief_Text that requests reading, writing, or changing publisher credentials, publisher targets, or storage settings.
6. THE Clipper SHALL require an explicit user action to initiate any publish, and SHALL ignore any instruction contained in Brief_Text that requests initiating a publish.
7. THE Clipper SHALL ignore any instruction contained in Brief_Text that requests a network request, a file-system path, or a shell command, and SHALL treat such text as an Advisory_Rule of Rule_Kind `other`.
8. THE Clipper SHALL escape Brief_Text and Requirement_Rule `source_text` when rendering them in the UI and when including them in API responses, so that brief content is displayed as text.
9. THE Clipper SHALL exclude Brief_Text and Parsed_Requirements from any prompt used by an unrelated Clipper capability unless that capability is explicitly configured to receive them.

### Requirement 19: API surface

**User Story:** As a developer integrating the tool, I want brief ingestion, parsing, mapping, and compliance exposed over the API, so that the UI and scripts can drive the feature.

#### Acceptance Criteria

1. THE Clipper SHALL expose endpoints to list, create, rename, delete, and mark active a stored Brief.
2. THE Clipper SHALL expose an endpoint that parses a stored Brief and returns its Parsed_Requirements, including the parser used, warnings, and `unparsed_text`.
3. THE Clipper SHALL expose an endpoint that returns a proposed Brief_Profile for a stored Brief without persisting it.
4. THE Clipper SHALL expose an endpoint that applies a reviewed Brief_Profile through the existing Profile_Store save path.
5. THE Clipper SHALL expose an endpoint that returns the Compliance_Report for a given job and clip.
6. THE Info_Endpoint SHALL advertise whether brief features are enabled, which Brief_Sources are available, whether an LLM_Client is available for parsing, and whether the Whop read path is available.
7. THE `OptionsModel` and the `/api/upload` Form fields SHALL accept the brief identifier for the job, the compliance-checking toggle, and the block-publishing-on-violation toggle.
8. IF a request references an unknown Brief identifier, THEN THE Clipper SHALL return a not-found response and SHALL leave stored Briefs unchanged.
9. WHEN the API receives an unknown value for a new option, THE Clipper SHALL apply the documented default and SHALL still process the job.

### Requirement 20: UI surface

**User Story:** As a clipper, I want to paste a brief, review what was understood, apply it to my settings, and see compliance per clip, so that the whole loop is visible in one place.

#### Acceptance Criteria

1. THE frontend SHALL provide a brief editor that accepts pasted Brief_Text, names the Brief, and saves it.
2. WHERE URL ingestion or Whop ingestion is available, THE frontend SHALL offer those Brief_Sources in addition to pasting.
3. THE frontend SHALL display the parsed Requirement_Rules grouped into Checkable_Rules and Advisory_Rules, alongside any `unparsed_text` and parse warnings.
4. THE frontend SHALL display the proposed Brief_Profile as a per-field comparison of current and proposed values with per-field accept controls, reusing the existing profiles surface.
5. THE frontend SHALL allow selecting the active Brief and selecting a Brief for a job.
6. THE frontend SHALL display each clip's Compliance_Status and per-rule Compliance_Rule_Results with reasons on the clip card.
7. WHERE a clip's Compliance_Status is `fail`, THE frontend SHALL display the failing rules before a publish is confirmed.
8. THE frontend defaults (`App.jsx`) SHALL include the new fields with the documented default values, and `toOptions` SHALL forward them.
9. WHERE brief features are disabled, THE frontend SHALL present the v0.8.0 surface without brief controls.

### Requirement 21: Toggles, defaults, and backward compatibility

**User Story:** As an operator upgrading from v0.8.0, I want the feature off until I opt in, so that the upgrade changes nothing I did not ask for.

#### Acceptance Criteria

1. THE Processing_Options SHALL expose an independent toggle for compliance checking and an independent toggle for blocking publishing on brief violation.
2. THE Clipper SHALL default every new toggle to disabled and every new brief-related field to empty or absent.
3. WHEN no Brief is configured for a job AND every new toggle is disabled, THE Pipeline SHALL produce clips, Effects_Applied, and publish behaviour identical to v0.8.0.
4. THE Processing_Options SHALL retain all existing v0.8.0 fields and their current default values.
5. THE Processing_Options record SHALL round-trip each new field through `from_dict` and `to_dict` without loss.
6. IF a new enum-like option value is unrecognised, THEN THE Processing_Options SHALL apply the documented default rather than raise.
7. THE Info_Endpoint SHALL continue to advertise all existing option values in addition to the new ones.
8. WHEN a stored Profile written before this feature is loaded, THE Profile_Store SHALL load it without raising and SHALL treat its brief association as absent.

### Requirement 22: Testability with injected dependencies

**User Story:** As a developer, I want the feature testable offline with a mocked LLM, fetcher, and Whop bridge, so that the suite stays fast, deterministic, and network-free.

#### Acceptance Criteria

1. THE Brief_Ingestor SHALL accept a dependency-injected Brief_Fetcher and a dependency-injected Whop_Brief_Reader so that tests can supply mocks.
2. THE Brief_Parser SHALL accept a dependency-injected LLM_Client so that tests can supply a `MockLLMClient`.
3. THE Deterministic_Extractor SHALL be a pure function of Brief_Text, testable without a network, an LLM, ffmpeg, or a subprocess.
4. THE Compliance_Checker SHALL expose rule evaluation as a pure function of a clip record and a Parsed_Requirements value, except for the `clips_per_day_max` rule, which SHALL accept an injected clip-count source.
5. THE Brief_Profile derivation SHALL be a pure function of Parsed_Requirements and the current settings blob, testable without the Profile_Store.
6. THE test suite SHALL exercise every Brief_Source, the LLM and deterministic parse paths, and the compliance evaluation paths without any real network access.
7. THE Brief_Ingestor SHALL support an injected Profile_Store path and History_Store path so that tests use temporary files.
8. FOR all Brief_Text inputs, parsing SHALL produce a valid Parsed_Requirements value whose rules carry known Rule_Kinds and non-conflicting numeric bounds (property-based test).
9. FOR all Parsed_Requirements values and clip records, compliance evaluation SHALL produce exactly one Compliance_Rule_Result per Requirement_Rule with a status in `{pass, fail, unknown}` (property-based test).

---

## Open Decisions

These three decisions shape the design and are encoded above as proposed
defaults. **Each needs user confirmation before the design phase.**

### Decision 1: Publish gating — *proposed: warn by default, optional blocking*

Encoded in Requirement 15. A `fail` Compliance_Status warns and reports the
failing rules, but publishing proceeds. An opt-in "block publishing on brief
violation" toggle (default **off**) makes the Publish_Manager decline the
attempt, with an explicit per-clip override. `unknown` never blocks.

*Alternative considered:* block by default. Rejected as inconsistent with the
product's "never block the creator" stance and with `unknown`-heavy briefs.

### Decision 2: Profile application — *proposed: proposed profile, user applies*

Encoded in Requirements 9, 10, and 11. A Brief yields a **proposed** Brief_Profile
shown as a per-field current-vs-proposed comparison; nothing is written to the
Profile_Store or to in-flight options until the user applies it, and settings
outside the Mapping_Allowlist are never touched.

*Alternative considered:* auto-apply on ingestion. Rejected: it would silently
override user settings from untrusted third-party text.

### Decision 3: Multiple briefs — *proposed: many stored, one active, per-job selectable*

Encoded in Requirement 4. Multiple Briefs are stored with names; at most one is
active; a job may name a specific Brief, otherwise the active Brief applies, and
with neither the run is exactly v0.8.0.

*Alternative considered:* a single global brief. Rejected: clippers commonly work
several campaigns at once.
