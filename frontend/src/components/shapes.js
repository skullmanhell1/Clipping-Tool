// The prop shapes for the API objects that cross more than one component boundary.
//
// A clip, a publish attempt and the publishing state are each passed through two or three
// components — `App` hands a job to `JobCard`, which hands each clip and the attempts filtered for
// it to `ClipCard` — so declaring them per consumer would mean the same object described in three
// places, drifting apart as fields are added. That is the duplication `SETTINGS_SCHEMA` was
// introduced to remove for settings; this is the same argument for the wire objects.
//
// The fields listed are the ones the components actually read, not everything the API sends.
// A shape is deliberately not `exact`: these arrive from the backend, and a component must not
// start warning because a newer server added a field it has no opinion about.
import PropTypes from "prop-types";

/** One rendered clip, as `/api/jobs` reports it and `ClipCard` edits it. */
export const CLIP_SHAPE = PropTypes.shape({
  // Required because it is the identity: the React key, the cache-busting `data-testid`, and the
  // path segment of every edit, review and re-render request made about this clip.
  id: PropTypes.string.isRequired,
  filename: PropTypes.string,
  start: PropTypes.number,
  end: PropTypes.number,
  duration: PropTypes.number,
  score: PropTypes.number,
  title: PropTypes.string,
  description: PropTypes.string,
  hashtags: PropTypes.arrayOf(PropTypes.string),
  hook_text: PropTypes.string,
  cta: PropTypes.string,
  thumbnail_text: PropTypes.string,
  video_url: PropTypes.string,
  thumbnail_url: PropTypes.string,
  reason: PropTypes.string,
  effects_applied: PropTypes.arrayOf(PropTypes.string),
  title_alternatives: PropTypes.arrayOf(PropTypes.string),
  mentions: PropTypes.arrayOf(PropTypes.string),
  // U9. Absent means `pending`, which every reader already spells as a fallback.
  review_state: PropTypes.string,
});

/** One attempt to publish one clip to one platform. */
export const PUBLISH_ATTEMPT_SHAPE = PropTypes.shape({
  id: PropTypes.string.isRequired,
  platform: PropTypes.string,
  state: PropTypes.string,
  campaign_id: PropTypes.string,
  account_id: PropTypes.string,
  message: PropTypes.string,
  error: PropTypes.string,
  url: PropTypes.string,
  created_at: PropTypes.number,
  scheduled_at: PropTypes.number,
  job_id: PropTypes.string,
  clip_id: PropTypes.string,
});

/**
 * The publishing state: where clips go and whether they go out unattended.
 *
 * `platforms` is required *within* the shape because every reader calls `.includes` on it without
 * guarding, so an object arriving without it is a crash rather than a degraded render.
 */
export const PUBLISHING_SHAPE = PropTypes.shape({
  platforms: PropTypes.arrayOf(PropTypes.string).isRequired,
  campaign_id: PropTypes.string,
  mode: PropTypes.string,
  schedule: PropTypes.string,
  account_id: PropTypes.string,
  target_type: PropTypes.string,
  target_id: PropTypes.string,
});

/**
 * `/api/publishers`: one probe result per platform, keyed by platform name.
 *
 * `objectOf` rather than a shape with five named platforms, because the key set is the server's to
 * decide — a platform added to the backend must appear in the UI without a frontend change, and
 * `PublishingPanel` has a test that renders an invented one.
 */
export const PUBLISHER_STATUSES_SHAPE = PropTypes.objectOf(
  PropTypes.shape({
    configured: PropTypes.bool,
    direct_publish: PropTypes.bool,
    message: PropTypes.string,
  })
);

/**
 * The options object `App.toOptions` produces: the settings in their wire form.
 *
 * Not enumerated, for the same reason `settings` is not — `SETTINGS_SCHEMA` in `App.jsx` is the
 * single declaration of that list, and restating its ~75 keys here would recreate the duplication
 * that schema removed. This is only ever forwarded to a re-render request, never read field by
 * field.
 */
export const WIRE_OPTIONS_SHAPE = PropTypes.object;
