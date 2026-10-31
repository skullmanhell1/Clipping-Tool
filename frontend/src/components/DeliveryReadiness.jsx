const STATUS_STYLES = {
  ready: {
    card: "border-emerald-400/30 bg-emerald-400/[0.05]",
    badge: "bg-emerald-500/15 text-emerald-200",
    label: "Variant ready",
  },
  fallback: {
    card: "border-amber-400/30 bg-amber-400/[0.05]",
    badge: "bg-amber-500/15 text-amber-200",
    label: "Canonical fallback",
  },
  blocked: {
    card: "border-rose-400/30 bg-rose-400/[0.05]",
    badge: "bg-rose-500/15 text-rose-200",
    label: "Blocked",
  },
};

const geometry = (destination) =>
  destination.width && destination.height
    ? `${destination.width}×${destination.height}`
    : "canonical geometry";

export default function DeliveryReadiness({
  readiness = null,
  loading = false,
  error = "",
  busyPlatform = "",
  onRender = null,
  onRetry = null,
}) {
  if (!readiness && !loading && !error) return null;
  const destinations = readiness?.destinations || [];
  const summary = readiness?.summary || { ready: 0, fallback: 0, blocked: 0 };

  return (
    <section
      data-testid="delivery-readiness"
      className="rounded-xl border border-slate-800 bg-slate-950/50 p-3"
      aria-busy={loading}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-200">
            Delivery readiness
          </h4>
          <p className="mt-1 text-[10px] leading-4 text-slate-500">
            Checks the actual file, route, account, and duration before this clip is queued.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {readiness ? (
            <span className="text-[10px] text-slate-400">
              {summary.ready} ready · {summary.fallback} fallback · {summary.blocked} blocked
            </span>
          ) : null}
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              disabled={loading}
              className="rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:border-slate-500 disabled:opacity-40"
            >
              {loading ? "Checking…" : "Recheck"}
            </button>
          ) : null}
        </div>
      </div>

      {error ? (
        <p role="alert" className="mt-2 text-[10px] text-rose-300">
          {error}
        </p>
      ) : null}

      <div className="mt-3 grid gap-2 md:grid-cols-3">
        {destinations.map((destination) => {
          const style = STATUS_STYLES[destination.status] || STATUS_STYLES.blocked;
          const busy = busyPlatform === destination.requested_platform;
          return (
            <div
              key={destination.requested_platform}
              className={`rounded-lg border p-2.5 ${style.card}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-xs font-semibold text-slate-100">
                    {destination.label}
                  </div>
                  <div className="mt-0.5 truncate text-[10px] text-slate-400">
                    {destination.filename || "No file selected"}
                  </div>
                </div>
                <span
                  className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-semibold ${style.badge}`}
                >
                  {style.label}
                </span>
              </div>

              <div className="mt-2 text-[10px] text-slate-500">
                {geometry(destination)}
                {destination.max_duration_s
                  ? ` · limit ${Math.round(destination.max_duration_s)}s`
                  : ""}
              </div>

              {destination.blockers?.length ? (
                <ul className="mt-2 space-y-1 text-[10px] leading-4 text-rose-200">
                  {destination.blockers.map((blocker) => (
                    <li key={blocker}>{blocker}</li>
                  ))}
                </ul>
              ) : null}
              {!destination.blockers?.length && destination.warnings?.length ? (
                <ul className="mt-2 space-y-1 text-[10px] leading-4 text-amber-200/90">
                  {destination.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              ) : null}

              {destination.action === "render_variant" && onRender ? (
                <button
                  type="button"
                  onClick={() => onRender(destination)}
                  disabled={Boolean(busyPlatform)}
                  className="mt-2 rounded border border-amber-300/30 px-2 py-1 text-[10px] font-semibold text-amber-100 hover:bg-amber-400/10 disabled:opacity-40"
                >
                  {busy ? "Rendering…" : `Render ${destination.label} variant`}
                </button>
              ) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}
