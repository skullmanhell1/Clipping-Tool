/**
 * U9: batch review controls for one job's clips.
 *
 * A job produces up to ten clips and every one had to be judged individually, with nowhere to
 * record the verdict — so a review pass over twenty clips left no trace and had to be redone from
 * the top after any interruption.
 *
 * "Select pending" is the button that makes this useful rather than merely present: the second
 * pass over a job is always about the clips you have not decided on yet, and selecting those by
 * hand is the work the batch action was supposed to remove.
 */

const chip =
  "rounded border border-slate-700 px-2 py-0.5 text-[10px] text-slate-300 hover:border-brand-accent disabled:opacity-40";

export default function ReviewBar({
  counts = { approved: 0, rejected: 0, pending: 0 },
  total = 0,
  selectedCount = 0,
  busy = false,
  error = "",
  onSelectAll,
  onSelectNone,
  onSelectPending,
  onApprove,
  onReject,
  onReset,
}) {
  const hasSelection = selectedCount > 0;

  return (
    <div className="mb-3 rounded-xl border border-slate-800 bg-slate-950/60 p-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-[11px] text-slate-400" data-testid="review-tally">
          <span className="text-emerald-400">{counts.approved || 0} approved</span>
          {" · "}
          <span className="text-rose-400">{counts.rejected || 0} rejected</span>
          {" · "}
          <span className="text-slate-400">{counts.pending || 0} to review</span>
          {" · "}
          {total} total
        </span>

        <span className="flex items-center gap-1">
          <button type="button" onClick={onSelectPending} className={chip} disabled={busy}>
            Select pending
          </button>
          <button type="button" onClick={onSelectAll} className={chip} disabled={busy}>
            All
          </button>
          <button
            type="button"
            onClick={onSelectNone}
            className={chip}
            disabled={busy || !hasSelection}
          >
            None
          </button>
        </span>

        <span className="ml-auto flex items-center gap-1">
          <span className="text-[10px] text-slate-500">
            {hasSelection ? `${selectedCount} selected` : "nothing selected"}
          </span>
          <button
            type="button"
            onClick={onApprove}
            disabled={busy || !hasSelection}
            // Named "selected" to distinguish it from the per-clip button, which is a different
            // action on a different scope: one clip versus everything ticked.
            aria-label="Approve selected clips"
            className="rounded border border-emerald-500/50 px-2 py-0.5 text-[10px] font-semibold text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-40"
          >
            ✓ Approve selected
          </button>
          <button
            type="button"
            onClick={onReject}
            disabled={busy || !hasSelection}
            aria-label="Reject selected clips"
            className="rounded border border-rose-500/50 px-2 py-0.5 text-[10px] font-semibold text-rose-300 hover:bg-rose-500/10 disabled:opacity-40"
          >
            ✕ Reject selected
          </button>
          <button
            type="button"
            onClick={onReset}
            disabled={busy || !hasSelection}
            className={chip}
            aria-label="Reset selected clips"
            title="Clear the verdict on the selected clips"
          >
            Reset
          </button>
        </span>
      </div>

      <p className="mt-1 text-[10px] text-slate-600">
        Keys: <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>a</kbd> approve · <kbd>x</kbd> reject ·{" "}
        <kbd>s</kbd> select · <kbd>space</kbd> play · <kbd>,</kbd>/<kbd>.</kbd> frame · <kbd>←</kbd>
        /<kbd>→</kbd> second
      </p>
      {error ? <p className="mt-1 text-[10px] text-rose-400">{error}</p> : null}
    </div>
  );
}
