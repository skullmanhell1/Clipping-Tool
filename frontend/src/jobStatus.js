/**
 * Job status vocabulary, mirroring `worker/models.py`.
 *
 * These lists exist in two languages because the frontend has to classify a status without
 * asking the backend. That duplication is the hazard, not the lists: `["queued", "processing"]`
 * was previously written inline in `App.jsx` and again in `api/main.py`, and adding `cancelling`
 * to the enum without finding both would have undercounted silently — the app would drop to its
 * slow idle interval exactly while a user waited for a cancel to take effect.
 *
 * So `tests/test_job_status_vocabulary.py` reads this file and asserts it against
 * `worker.models`, which is the same cross-language pin `tests/test_stems_api.py` applies to the
 * settings schema. A status added on one side and not the other fails that test rather than
 * quietly changing behaviour.
 */

/** Still holds, or is still waiting for, the worker. */
export const ACTIVE_JOB_STATUSES = ["queued", "processing", "cancelling"];

/** Never leaves this state. */
export const TERMINAL_JOB_STATUSES = ["completed", "failed", "cancelled"];

/** A cancel request is meaningful. Excludes `cancelling` — a second cancel is a no-op. */
export const CANCELLABLE_JOB_STATUSES = ["queued", "processing"];
