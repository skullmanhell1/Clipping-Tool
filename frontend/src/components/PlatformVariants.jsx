import { useMemo, useState } from "react";
import { api } from "../api.js";

/**
 * Turn one selected moment into destination-specific files.
 *
 * The platform catalogue comes from `/api/info`, which already owns the aspect and dimensions
 * for each destination. Rendering is deliberately one destination per click: a variant is a
 * full encode, and starting three at once would hide which one is taking the time or failed.
 */
export default function PlatformVariants({
  jobId,
  clip,
  settings = {},
  outputProfiles = [],
  onUpdated,
}) {
  const [busyPlatform, setBusyPlatform] = useState("");
  const [error, setError] = useState("");

  const profiles = useMemo(
    () => (Array.isArray(outputProfiles) ? outputProfiles.filter((profile) => profile?.name) : []),
    [outputProfiles],
  );
  const variants = useMemo(
    () => new Map((Array.isArray(clip?.variants) ? clip.variants : []).map((item) => [item.platform, item])),
    [clip?.variants],
  );

  if (!profiles.length) return null;

  const render = async (platform) => {
    if (busyPlatform) return;
    setBusyPlatform(platform);
    setError("");
    try {
      const updated = await api.renderVariant(jobId, clip.id, platform, settings, []);
      onUpdated?.(updated);
    } catch (renderError) {
      setError(renderError.message || "Could not render this platform variant.");
    } finally {
      setBusyPlatform("");
    }
  };

  return (
    <section
      data-testid="platform-variants"
      className="rounded-lg border border-cyan-400/20 bg-cyan-400/[0.035] p-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-cyan-100">
            Platform variants
          </h4>
          <p className="mt-1 text-[10px] leading-4 text-slate-500">
            One encode per destination. The original clip and its publish links stay untouched.
          </p>
        </div>
        <span className="rounded-full border border-cyan-300/20 bg-cyan-400/10 px-2 py-0.5 text-[10px] text-cyan-100">
          {variants.size} ready
        </span>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
        {profiles.map((profile) => {
          const variant = variants.get(profile.name);
          const busy = busyPlatform === profile.name;
          return (
            <div
              key={profile.name}
              className="rounded-lg border border-slate-800 bg-slate-950/50 p-2.5"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-xs font-medium text-slate-200">
                    {profile.label || profile.name}
                  </div>
                  <div className="mt-0.5 text-[10px] text-slate-500">
                    {profile.recommended_aspect || profile.aspect || ""}
                    {profile.width && profile.height ? ` · ${profile.width}×${profile.height}` : ""}
                  </div>
                </div>
                {variant ? (
                  <span
                    className="shrink-0 rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-semibold text-emerald-200"
                    title="Publishing to this destination uses this file automatically."
                  >
                    Ready
                  </span>
                ) : null}
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => render(profile.name)}
                  disabled={Boolean(busyPlatform)}
                  className="rounded border border-cyan-300/30 px-2 py-1 text-[10px] font-semibold text-cyan-100 hover:bg-cyan-400/10 disabled:opacity-40"
                >
                  {busy ? "Rendering…" : variant ? "Re-render" : "Render"}
                </button>
                {variant ? (
                  <>
                    <a
                      href={api.videoDownloadUrl(jobId, variant.filename)}
                      className="text-[10px] text-slate-300 underline hover:text-white"
                    >
                      Video
                    </a>
                    <a
                      href={api.downloadUrl(jobId, variant.filename)}
                      className="text-[10px] text-slate-300 underline hover:text-white"
                    >
                      Package
                    </a>
                  </>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>

      {busyPlatform ? (
        <p role="status" className="mt-2 text-[10px] text-cyan-200">
          Rendering {profiles.find((profile) => profile.name === busyPlatform)?.label || busyPlatform}.
          This is a full encode, so it can take a minute.
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-2 text-[10px] text-rose-300">
          {error}
        </p>
      ) : null}
    </section>
  );
}
