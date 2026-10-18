# Short-form clipping benchmark

Research date: 2026-10-16

This note records the external patterns used for the retention-quality pass. It
is a design reference, not a promise that any individual edit will go viral.

## What established tools expose

### VEED Clips

VEED's AI Clip Generator describes a workflow that lets the creator set topic,
length, format and caption style, then review the result in a transcript editor.
It calls out silence and filler removal, dynamic captions, B-roll, branding and
four review dimensions: **Flow, Impact, Clarity and Relevance**.

Source: <https://www.veed.io/tools/auto-video-editor/ai-clip-generator>

### OpusClip

OpusClip describes cross-genre selection using visual, audio and sentiment
cues, followed by AI relayout, dynamic captions, smooth transitions and a
strong call to action. It also exposes natural-language prompts instead of
forcing creators to accept an opaque selection.

Source: <https://www.opus.pro/>

### Platform guidance

- YouTube classifies square or vertical uploads up to three minutes as Shorts.
  A Short over one minute with an active Content ID claim can be blocked
  globally, so music and B-roll provenance must remain visible to the editor.
- YouTube recommends making the first lines of a description describe the
  video and using one or two main keywords in the title and description.
- TikTok's Creative Center presents trend and creative-strategy research as a
  first-party planning surface; trend-specific choices should remain optional
  rather than baked into every clip.
- TikTok's Content Posting API exposes `max_video_post_duration_sec` in creator
  info; the current official example reports 300 seconds. Until the publisher
  queries that account-specific value, preflight uses 300 seconds as a
  conservative ceiling.

Sources:

- <https://support.google.com/youtube/answer/15424877>
- <https://support.google.com/youtube/answer/12948449>
- <https://ads.tiktok.com/business/creativecenter/quicktok/>
- <https://developers.tiktok.com/docs/en/content-posting-api-get-started>

## Product decisions derived from the research

1. **Hook plus payoff:** score the opening hook, but also prefer a window that
   lands a complete thought instead of ending on a conjunction or an
   unfinished clause.
2. **Measured review axes:** expose Flow, Impact, Clarity and Relevance on the
   review card. Relevance uses only creator-supplied topic words; unknown
   relevance stays neutral and generic excitement cannot manufacture a topic
   match.
3. **Pacing with restraint:** preserve existing silence/filler/cut safeguards;
   only apply retention pacing cuts in aggressive mode and protect reaction and
   hook beats.
4. **Sound-off readiness:** keep captions, safe-area placement and transcript
   editing first-class. Caption emphasis must not depend on an unrelated AI
   keyword toggle.
5. **Creator control:** prompts, audience reactions, B-roll, music, trends and
   platform-specific choices remain explicit options. Automated editing should
   leave inspectable reasons and provenance markers.
6. **Language-safe matching:** topic relevance is lexical and conservative, but
   it normalizes accents and supports Unicode terms so a creator asking for
   `café`, `AI`, or non-Latin keywords does not silently get a neutral score.

## Quality guardrails

- Do not claim a deterministic score predicts virality.
- Missing audio, pitch, visual or reaction evidence is neutral, not a zero.
- Never invent quotes, facts, reactions or a payoff that is not in the
  transcript.
- Keep one coherent thought, a visible beginning and a defensible ending.
- Review the first three seconds, the final sentence, captions and platform
  overlays before publishing.
