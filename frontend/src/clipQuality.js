// Shared review signals. Kept out of `ClipCard` so the review queue can rank and filter
// clips with exactly the numbers the card displays, rather than growing a second scoring
// implementation that drifts from the one users see.

export const TOP_PICK_SCORE = 70;
export const ATTENTION_SCORE = 50;
export const WEAK_HOOK_SCORE = 45;
export const UNCLEAR_THOUGHT_SCORE = 50;

export const REVIEW_SIGNAL_LABELS = {
  hook: ["Hook", "Opening strength"],
  flow: ["Flow", "Pacing and continuity"],
  impact: ["Impact", "Delivery and emotional strength"],
  clarity: ["Clarity", "Complete, easy-to-follow thought"],
  relevance: ["Relevance", "Match to your topic"],
};

export const RETENTION_REASON_LABELS = {
  front_loaded_hook: "Strong opening",
  energy_accents: "Energy accents",
  tone_variation: "Tone variation",
  audience_reactions: "Audience reactions",
  dead_air_pacing: "Tight pacing",
  clear_payoff: "Clear payoff",
};

// Normalise a ratio or a percentage into a display percentage. Missing evidence is omitted
// rather than rendered as zero: a neutral/unknown signal is not the same as a bad one.
export function signalPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  const ratio = number > 1 ? number / 100 : number;
  return Math.max(0, Math.min(100, Math.round(ratio * 100)));
}

export function signalTone(percent) {
  if (percent >= 75) return "bg-emerald-400";
  if (percent >= 50) return "bg-cyan-400";
  if (percent >= 30) return "bg-amber-400";
  return "bg-rose-400";
}

export function reviewSignals(clip) {
  const retention = clip?.retention_plan && typeof clip.retention_plan === "object"
    ? clip.retention_plan
    : {};
  const features = clip?.engagement_features && typeof clip.engagement_features === "object"
    ? clip.engagement_features
    : {};
  const quality = {
    ...(features.quality_scores && typeof features.quality_scores === "object"
      ? features.quality_scores
      : {}),
    ...(retention.quality_scores && typeof retention.quality_scores === "object"
      ? retention.quality_scores
      : {}),
  };
  const retentionActive = retention.mode && retention.mode !== "off";
  const values = {
    // A retention plan that was not run reports hook_score 0. The selection features still
    // carry the measured hook, so prefer them when retention is off.
    hook: retentionActive
      ? retention.hook_score ?? features.hook_score
      : features.hook_score ?? retention.hook_score,
    flow: quality.flow,
    impact: quality.impact,
    clarity: quality.clarity,
    relevance: quality.relevance,
  };
  return Object.entries(values)
    .map(([key, value]) => {
      const percent = signalPercent(value);
      if (percent === null) return null;
      const [label, hint] = REVIEW_SIGNAL_LABELS[key] || [key, ""];
      return { key, label, hint, percent, tone: signalTone(percent) };
    })
    .filter(Boolean);
}

export function retentionReasons(clip) {
  const reasons = clip?.retention_plan?.reasons;
  if (!Array.isArray(reasons)) return [];
  return reasons
    .map((reason) => RETENTION_REASON_LABELS[reason] || String(reason || "").replaceAll("_", " "))
    .filter(Boolean)
    .slice(0, 6);
}

function finiteScore(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.round(number) : null;
}

/**
 * The review-facing quality of one clip.
 *
 * `overall` prefers the backend's measured engagement score and falls back to the mean of the
 * quality axes. It is deliberately absent when the backend measured neither: inventing a neutral
 * 50 would let unscored legacy clips outrank measured weak ones in the review queue.
 */
export function clipQuality(clip) {
  const signals = reviewSignals(clip);
  const reasons = retentionReasons(clip);
  const engagement = finiteScore(clip?.engagement_score);
  const average = signals.length
    ? Math.round(signals.reduce((total, signal) => total + signal.percent, 0) / signals.length)
    : null;
  const overall = engagement ?? average;
  const byKey = Object.fromEntries(signals.map((signal) => [signal.key, signal.percent]));
  const attention = [];

  if (overall !== null && overall < ATTENTION_SCORE) attention.push("low overall signal");
  if (byKey.hook !== undefined && byKey.hook < WEAK_HOOK_SCORE) attention.push("weak opening");
  if (byKey.clarity !== undefined && byKey.clarity < UNCLEAR_THOUGHT_SCORE) {
    attention.push("unclear thought");
  }
  if (byKey.impact !== undefined && byKey.impact < WEAK_HOOK_SCORE) attention.push("low impact");

  return {
    signals,
    reasons,
    engagement,
    average,
    overall,
    attention,
    needsAttention: attention.length > 0,
    // A clip with a weak axis is not a top pick even when its other axes pull the average up.
    // The badge is a shortlist claim, so it has to survive the attention checks too.
    topPick: overall !== null && overall >= TOP_PICK_SCORE && attention.length === 0,
  };
}
