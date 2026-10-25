# Asset provenance and safe sourcing

This project can render user-supplied overlays, music, b-roll, brand media, and
sound effects. It does not grant rights to third-party media. Do not bundle or
download ripped Vine/Bruh clips, TV/movie audio, broadcast excerpts, reaction
memes, or other assets whose license and permission are unknown.

## What “meme mode” means here

The storytelling planner can suggest a reaction or meme concept from transcript
words. A suggestion is editorial metadata only:

- it has a bounded clip-relative time window;
- it describes a concept and an operator prompt;
- it marks whether an asset is required; and
- it does not fetch, embed, or assert ownership of an image, screenshot, movie
  still, GIF, or sound.

Use text, shapes, captions, emoji, or a synthetic sound when that communicates
the beat without a third-party work. If a reaction template is important, bring
one that you created, commissioned, or licensed for the intended distribution.
The safest default for a campaign is no external meme media until a human has
reviewed the exact file and its license.

## Local-first asset workflow

1. **Source one asset at a time.** Preview it in the library, read the item-level
   license, and confirm that the license permits the intended commercial use,
   modification, redistribution, and platform publishing.
2. **Keep the original evidence.** Save the source page URL, creator name,
   license name and URL, any required attribution text, and the date you
   retrieved it. For a commissioned or user-created asset, record the owner and
   written permission reference instead.
3. **Copy only reviewed files** into `assets/broll`, `assets/music`, or
   `assets/sfx`. Do not use a crawler, bulk downloader, playlist ripper, or
   automated mass-download workflow.
4. **Hash the exact file** (for example, SHA-256) and add one entry to
   `assets/asset-manifest.json`, copied from
   [`../assets/asset-manifest.example.json`](../assets/asset-manifest.example.json).
5. **Use local-only mode** while reviewing: set `asset_sourcing_mode=local_only`
   in the job, leave `BROLL_ALLOW_DOWNLOAD=false`, and enable
   `permissibility_mode` when added audio and external downloads must be blocked.
6. **Review the rendered clip and sidecar.** The pipeline records b-roll
   provenance only for assets actually composited. A missing, failed, or
   unknown-license asset is skipped rather than made part of the output.

The manifest is a human-maintained evidence file; the current app does not
parse it as an enforcement database. The renderer's own b-roll provenance is
written into the clip record/sidecar, so keep the manifest and its source
evidence alongside the media for auditability.

## License choices

Prefer public-domain or CC0 assets when practical. Other licenses can be
compatible, but read the exact terms:

- **CC0 / public domain:** usually the simplest path, but trademark, privacy,
  publicity, and recognizable-person issues can still apply.
- **CC BY:** generally permits commercial use and adaptation with attribution;
  preserve the required credit in the manifest and campaign records.
- **CC BY-SA:** adaptation may require sharing under the same or a compatible
  license; confirm that this fits a proprietary or paid campaign.
- **CC BY-NC:** noncommercial only; do not use it for monetized, sponsored, or
  client campaigns without separate permission.
- **CC BY-ND:** no derivatives; compositing, cropping, captioning, or editing
  may not be allowed. Obtain permission or choose another asset.

Re-check a library's current terms and the individual item's license. A site
label such as “free,” “royalty-free,” or “meme” is not a license by itself.

## Reputable starting points

Use the library's normal search and download controls, not automated harvesting.
Always verify the individual item:

- [Openverse](https://openverse.org/) — filter for an explicit license and keep
  the item page/creator attribution.
- [Wikimedia Commons](https://commons.wikimedia.org/) — inspect the file page's
  license, author, source, and any model/trademark concerns.
- [Freesound](https://freesound.org/) — useful for SFX, but licenses vary by
  upload; select CC0 or a compatible attribution license and retain the item
  page and creator credit.
- [NASA Image and Video Library](https://images.nasa.gov/) — many government
  materials are public domain, but check item notes, third-party material,
  logos, people, and endorsement restrictions.

Do not assume that a platform preview, repost, “sound effect pack,” reaction
compilation, or public search result is cleared for reuse. Do not use a movie,
TV, Vine, or creator clip merely because it is short or widely reposted.

Use stable, descriptive filenames and keep `tags.json` for local b-roll
matching. A tags entry can be a list or a space-separated string:

```json
{
  "licensed-currency-graphic.mp4": ["money", "revenue", "price"],
  "creator-overwhelmed-reaction.png": "chaos overwhelmed reaction"
}
```

The SFX resolver prefers `<name>.wav`/audio files in `SFX_DIR` (or the meme SFX
library). Short generated tones are available for `pop`, `click`, `whoosh`,
`swipe`, `beep`, `bass_drop`, `ding`, `dramatic_sting`, `airhorn`, `crickets`, `vine_boom`, and
`record_scratch`; these are original signal recipes, not recordings of branded
catchphrases. A matching local file is preferred and can replace any generated
effect; an unknown effect with no local file remains a no-op and records
`sfx_missing:<name>`. Music uses a supplied track before the synthetic drone;
the drone is marked `music_degraded:synthesised` and should not be described as
a licensed song.

## Manifest template fields

Each asset entry should answer: what is it, where did it come from, who made it,
what license applies, what credit is required, and which exact bytes were
reviewed? The starter template includes:

| Field | Meaning |
| --- | --- |
| `id` | Stable internal identifier. |
| `path` | Repository-relative or deployment-relative media path. |
| `kind` | `meme`, `broll`, `music`, `sfx`, `logo`, or another local category. |
| `source_url` | Original item or permission reference; never a search-result URL alone. |
| `source_title`, `creator` | Human-readable identification. |
| `license`, `license_url` | Exact license name and terms page. |
| `attribution` | Required credit, or an explicit empty string for CC0/owned media. |
| `commercial_use`, `derivatives`, `redistribution` | Operator's reviewed yes/no notes. |
| `retrieved_at` | ISO date of download or permission capture. |
| `sha256` | Digest of the exact local file. |
| `notes` | Model-release, trademark, privacy, expiry, or campaign restrictions. |

Do not fill a field with `unknown` and then treat the asset as cleared. If the
license cannot be identified, leave the media out of the render; this is the
same conservative behavior used by external b-roll resolution.

## Campaign and review implications

Campaign briefs can suggest meme concepts and proof cards, but a brief is data,
not permission to use referenced media. A Google Drive import does not make its
linked images, audio, or screenshots cleared for publishing. Confirm the source
owner's permission separately, record it in the manifest, and re-review clips
after a brief or asset changes. For sponsored campaigns, keep the manifest,
license evidence, attribution text, and final clip sidecar together with the
campaign approval record.
