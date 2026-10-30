# Campaign automation and retention workflow

The saved, versioned workflow is the unified Campaign panel described in
[`UNIFIED_CAMPAIGNS.md`](UNIFIED_CAMPAIGNS.md). This document covers the review-only parser and the
public scheduling connector that remain useful for automation-focused clients.

This release adds an opt-in automation layer for Whop clipping campaigns. It keeps the existing
CPU-first renderer and publishing adapters, but gives them a shared, reviewable policy object.

## Product flow

```text
Google Doc / pasted brief
        |
        v
campaign preset parser (deterministic, no network required)
        |
        +--> brand/caption/aspect/hashtags/CTA policy
        +--> visual storytelling plan
        +--> mechanical compliance report
        |
long-form source -> transcript -> selection -> render -> review
        |
        +--> proof cards, concept/meme suggestions, questions, pattern resets,
        |    curiosity opener, cliffhanger/Part 2 end-card
        |
        +--> campaign-aware publish preflight
                    |
                    +--> configured clips/day at distinct peak slots
                    +--> public-only direct publishing
```

The parser treats document text as data. It never executes document instructions, downloads an
asset without an operator-supplied path/provider, or makes a publish decision on its own.

## Google Doc / Drive campaign presets

The existing Drive brief sync endpoint remains the source connector:

- `POST /api/briefs/parse` accepts text from a connector and can sync it to a saved campaign.
- `POST /api/briefs/{campaign_id}/sync` applies a webhook/poll snapshot and preserves the last
  good brief when a read fails.
- `POST /api/campaign-presets/parse` returns a deterministic `CampaignPreset` for a pasted brief;
  `/api/campaign-presets/import` is the canonical endpoint for the saved unified preset workflow.

The preset supports:

- logo, font, primary/accent colours, caption preset and aspect ratios;
- required hashtags and a CTA;
- allowed destinations;
- an explicit daily target (normalised to at least two);
- public visibility enforcement;
- hook, cliffhanger and pattern-interrupter settings.

When a campaign-backed job is submitted, the preset is copied into the job options. This is
important: a later Google Doc edit cannot silently change an already-rendered clip. A new sync
marks affected clips for re-render/review, as the existing campaign brief service already does.

## Storytelling plan

`worker/storytelling.py` is a pure planning module. It accepts transcript words, optional entity
annotations, and optional scene metadata, then emits JSON-safe suggestions:

- entity proof cards for books, tweets, charts, films and people;
- concept illustrations and meme/reaction prompts, marked when licensed/operator media is needed;
- visual resets at a five-second cadence, bounded to the clip and nudged to nearby shot cuts;
- poll/question overlays from question-shaped transcript spans;
- curiosity-loop opener evidence; and
- a Part 2 / cliffhanger end-card opportunity when continuation language is detected.

The renderer stores this plan on `ClipResult.storytelling_plan` and shows its counts in the clip
card. It does not claim that a screenshot, meme or public-figure asset was sourced. The operator
must provide or license those assets before a future compositor step can burn them in.

## Two clips per day and public publishing

Use `POST /api/campaigns/{campaign_id}/daily-schedule` with:

```json
{
  "job_id": "completed-job-id",
  "clip_ids": ["clip-a", "clip-b"],
  "platforms": ["youtube", "tiktok", "instagram"],
  "day": "2026-10-19",
  "timezone": "Australia/Perth",
  "peak_hours": ["12:00", "19:00"],
  "visibility": "public"
}
```

The endpoint rejects anything other than two different clip ids, uses two distinct local peak
times converted to UTC, and checks every selected publisher for direct-public approval before it
creates queue entries. The scheduler stores `public_only` with each attempt. It refuses to call a
publisher that cannot publish directly and rejects an unexpected draft/private result rather than
silently treating it as success.

Platform approval is still a platform-side requirement. For example, a TikTok app that has not
been approved for Direct Post cannot be made public by application code; the endpoint reports that
status and leaves the clip unposted. This is intentional and protects the campaign's public-only
contract.

## Docker Desktop update procedure

After this pull request is merged, update a local checkout that Docker Desktop runs from:

```bash
git fetch origin
git checkout main
git pull --ff-only origin main
docker compose build --pull
docker compose up -d
docker compose logs -f --tail=100
```

If the checkout is pinned to a PR branch instead, replace the checkout/pull step with:

```bash
git fetch origin pull/<PR_NUMBER>/head:campaign-automation
git checkout campaign-automation
```

Do not delete the `storage/` directory or the `.env` file: campaign history, jobs and local
clips are persisted there. The image rebuild installs the same Python/Node dependencies as the
existing Docker workflow. Verify the update with:

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/api/info
```

Then open the dashboard, paste the campaign brief into the **Campaign workspace**, review the parsed
preset, render at least two clips, and use the daily schedule form only after the platform status
shows direct-public approval.
