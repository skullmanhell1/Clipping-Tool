# Unified campaign presets

The Campaign panel is the source of truth for a campaign. It follows four explicit stages:

1. **Import** — submit one or more public/private Docs or Drive links, folders, pasted text, or local requirement files.
2. **Review** — edit the extracted policy and source library. Ambiguity is retained as a warning.
3. **Save & apply** — persist one versioned preset and apply its settings to the next clipping job.
4. **Compliance & posting** — run the active preset's compliance report and use the public scheduler.

The import surface also includes a creator-studio strategy blueprint. It keeps content type, clip goal,
target audience, hook approach, selection direction, retention ranking, target clip count, story-first
assembly, active-speaker framing, and b-roll intent beside the campaign rules. These fields are optional;
the importer does not invent them when a brief is silent.

Review also offers editable quick-start recipes for podcast growth, education authority, product launch,
and clean storytelling. A recipe only fills the draft; campaign locks still win, and the operator must
review and explicitly Save & apply.

The Review and Save & apply stages show a campaign-readiness preflight with separate states for identity,
output format, distribution, clip strategy, source approvals, and policy checks. Blocked items match the
required fields that prevent applying; source suggestions and optional strategy gaps remain visible as
review states rather than being silently promoted to blockers.

The campaign policy also follows clips into the scheduling workspace. A manual-only preset disables the
direct auto scheduler, disallowed platforms are unavailable, and the Campaign panel's daily scheduler
 requires a passing compliance report, the configured number of approved clips, completed required
checklist items, and distinct peak slots before it can queue public posts.
Those checks are enforced by the API as well: direct scheduling calls must provide the active campaign's
allowed platforms and review-check acknowledgements, use approved clips, and supply valid distinct slots.
Compliance is evaluated for every requested platform before any attempt is queued.
Publish requests that omit a platform list resolve the saved campaign routes first, so the same policy
cannot be bypassed by relying on publisher defaults; aliases that resolve to the same publisher are
rejected rather than creating duplicate attempts.
The daily scheduler also rejects a clip/platform pair that is already live instead of reporting a new
slot while silently reusing the old attempt.
It also rejects destination-slot collisions already present in the publishing calendar.
Campaigns may configure a bounded `posting.daily_clip_target` from 1 to 10; the UI requests that many
approved clips and distinct peak slots, and the API enforces the same target.
Compliance preview can also run against a selected completed job and rendered clip, rather than only
against pasted draft copy.
The clip review workspace includes pending/approved/rejected filters, filter-scoped batch selection,
and keyboard review controls so large jobs can be processed without losing focus.
Each clip review card also supports choosing and saving a precise thumbnail frame for platform
previews instead of relying only on the automatic thumbnail.
Review decisions can carry handoff notes, including one shared note for a filtered batch action.
Clip selection now repairs nearby mid-thought openings and unfinished endings when the requested
length budget allows. Caption cues use measured font widths and platform-aware safe areas, while
quality cards expose flow, impact, clarity, and relevance signals for review.
Visual selection normalizes motion and brightness against the source baseline, avoiding a bias toward
bright but visually static footage.
The compliance preview reads the current editable draft, including local source decisions, so review
changes are checked before Save & apply rather than being silently replaced by an older saved version.

Server-side job creation also consumes the strategy fields, so API, upload, and watch-folder submissions
receive the same selection direction as the browser flow. Supported duration and clip-count phrases are
normalized before segmentation. Local uploads may enter a campaign with a source library; the existing
source confirmation is collected at publish time rather than rejecting the upload before the creator can
review it.

## Local requirement files

The Campaign panel accepts multiple local `.txt`, `.md`, `.markdown`, `.csv`, `.json`, `.yaml`, and
`.yml` requirement files by drag-and-drop or file picker. Files are read in the browser, limited to 2 MB
each and 10 files per intake. Raw file objects stay in the browser; when Analyze is clicked, extracted
text is sent to the normal review-only brief importer and is not saved as a preset. Text files are
combined into one reviewable brief. A structured campaign JSON file is loaded into the same Review step
without saving it; **Save & apply** is still explicit. PDF and DOCX are intentionally not treated as plain text because
silently extracting binary data would create unsafe, misleading campaign rules; export them to text or
use the authorized Drive connector instead.

The strategy blueprint is inspired by the useful workflow patterns common to modern clip studios:
brief/transcript-first intake, guided highlight selection, active-speaker framing, caption/brand controls,
multi-platform output planning, and human review before publishing. It does not copy provider branding or
claim a deterministic "virality" score.

## Schema

The JSON store is versioned at `schema_version: 2`. The main blocks are:

- `length` — explicit clip duration range.
- `aspect_resolution` — allowed aspect ratios and output resolutions.
- `settings` — ProcessingOptions-shaped defaults, optional `locked_fields`, and strategy keys such as
  `content_type`, `clip_goal`, `target_audience`, `hook_style`, `selection_prompt`, `retention_mode`,
  `num_clips`, `strategy`, `vibe`, `caption_preset`, `caption_position`, `broll_intensity`,
  `asset_sourcing_mode`, `reframe_layout`, and `reframe_intensity`.
- `edit_policy` — enforced editor locks and `enforced_settings` for campaign-specific tools.
- `quality_policy` — whether quality delivery is allowed.
- `captions_language`, `copy_rules`, `watermark_end_cards` — caption, spelling, CTA, brand, and card rules.
- `audio_rules`, `meme_policy` — allowed/prohibited audio and meme policy.
- `posting` — allowed platforms, manual-only mode, public visibility, peak slots, and affiliate requirements.
- `compliance` — required copy, prohibited terms/topics, and human review checks.
- `source_library` — individual sources with `approved`, `suggested`, or `banned` status.
- `source_links` — original brief/Drive links used for re-sync; these are not automatically approved footage.
- `warnings`, `dismissed_warnings`, `last_synced_at`, `changed` — review and synchronization state.

## Source safety

Links extracted from a brief are `suggested` unless the brief explicitly says they are approved or
the operator accepts them in the source library. YouTube channels are discovery references; they do
not approve every video on the channel. A URL submission is rejected when it is banned, suggested,
or absent from the active preset's approved source library. Local files require the source review
check when a campaign has a source allow-list.

## Migration

`CampaignProfileStore` migrates old profile records on load without deleting them. The configured
application store receives a one-time migrated `BOXABL Casita` preset from the previous source list
and review checklist. The migration marker is stored beside the profiles, so deleting the preset
does not cause it to be recreated. Explicit temporary stores do not receive the product seed.

The migrated BOXABL record is ordinary saved preset data. New clients must not special-case its
name or id; rendering and publishing consume the same generic preset fields as every other campaign.

The older `/api/campaign-profiles/*` endpoints remain compatibility adapters. New clients should use:

- `GET /api/campaign-presets`
- `POST /api/campaign-presets/import`
- `POST /api/campaign-presets` with `select: true` to save and apply
- `POST /api/campaign-presets/{id}/select`
- `PATCH /api/campaign-presets/{id}` to rename
- `POST /api/campaign-presets/{id}/duplicate`
- `DELETE /api/campaign-presets/{id}`
- `POST /api/campaign-presets/{id}/sync`
- `GET /api/campaign-presets/{id}/export`
- `POST /api/campaign-presets/import-json` (pass `save: false` for review-only JSON intake)

The review-only parser/connector endpoints remain available for automation-focused clients:

- `POST /api/campaign-presets/parse` for the deterministic parser
- `POST /api/campaign-presets/from-drive` for the Drive connector

Compliance evaluation is currently exposed at `POST /api/campaign-profiles/compliance` because it
accepts the compatibility profile payload and can evaluate one or more saved preset ids. Treat this
route as a compatibility surface while using the `/api/campaign-presets` routes for preset storage,
selection, import, export, and synchronization.

OAuth credentials remain server-side and use the existing minimum read-only Drive scope. No token is
serialized into a preset, source record, browser response, or Git-tracked file.
