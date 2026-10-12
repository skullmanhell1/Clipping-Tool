import { useState } from "react";

export const BOXABL_APPROVED_SOURCES = [
  { label: "Enes Yilmazer", videoId: "8JMrfSFQdpo" },
  { label: "Orkun Isitmak", videoId: "eS-fnedN2yI" },
  { label: "Faze Rug", videoId: "uu5aORByttg" },
  { label: "Graham Stephan", videoId: "0Cdqs2Q1Vis" },
  { label: "Airrack", videoId: "7rX5cPQrx_s" },
  { label: "808", videoId: "5Lo54Lx7MCU" },
  { label: "Ellie in Space", videoId: "KrahQlLAz44" },
  { label: "Irina Dee", videoId: "3plLxp_7Jlo" },
  { label: "BOXABL Casita Tour", videoId: "JbS_VhA0rRo" },
  { label: "BOXABL Factory Update", videoId: "IBV7eIH3Bdw" },
  { label: "BOXABL Factory Update", videoId: "b9erzF6LHKI" },
  { label: "BOXABL Factory Update", videoId: "9ThVUNGj3To" },
  { label: "BOXABL Factory Update", videoId: "rUArr8SWyf8" },
  { label: "NYSE interview", videoId: "Cz9cwL_fqTI" },
].map((source) => ({
  ...source,
  url: `https://youtu.be/${source.videoId}`,
}));

export const BOXABL_DRIVE_SOURCE = {
  label: "Elon Musk Clips",
  url: "https://drive.google.com/file/d/1OnH2B5N-TaGyHqPdmT0uxdCeA-38FXry/view",
};

export default function BoxablCampaignPanel({
  active = false,
  driveConfigured = false,
  profileAvailable = true,
  onApply,
}) {
  const [copyStatus, setCopyStatus] = useState("");

  const copyLink = async (url) => {
    try {
      await navigator.clipboard.writeText(url);
      setCopyStatus("Link copied. Paste it into the video-link field above.");
    } catch {
      setCopyStatus("Copy was blocked by the browser. Open the source and copy its URL.");
    }
  };

  return (
    <section
      aria-labelledby="boxabl-campaign-heading"
      className="overflow-hidden rounded-2xl border border-amber-400/20 bg-gradient-to-br from-amber-400/[0.08] via-slate-900/80 to-slate-950/80 shadow-xl shadow-black/10"
    >
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-amber-400/10 px-5 py-5 sm:px-6">
        <div className="max-w-3xl">
          <div className="text-[10px] font-semibold uppercase tracking-[0.2em] text-amber-300">
            Campaign brief · approved source list
          </div>
          <h3 id="boxabl-campaign-heading" className="mt-1 text-lg font-semibold text-white">
            BOXABL Casita
          </h3>
          <p className="mt-1 text-sm leading-6 text-slate-300">
            20×20 ft foldable, factory-built home. The campaign edit beat is hook 0:00–0:02,
            whole home 0:02–0:05, exact BOXABL branding 0:05–0:08, then features, stated price,
            or a genuine guest reaction.
          </p>
        </div>
        <button
          type="button"
          onClick={onApply}
          disabled={!profileAvailable}
          aria-pressed={active}
          className={`rounded-xl px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${
            active
              ? "border border-amber-300/30 bg-amber-300/15 text-amber-100"
              : "bg-amber-300 text-slate-950 hover:bg-amber-200"
          }`}
        >
          {active ? "Campaign preset applied" : "Apply campaign preset"}
        </button>
      </div>

      <div className="grid gap-5 p-5 sm:p-6 lg:grid-cols-2">
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-300">
            Approved YouTube video links
          </h4>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            Only these individual links are accepted by the BOXABL profile. The official channel
            is a discovery reference only; new videos need campaign approval before being added here.
          </p>
          <ul className="mt-3 grid gap-1.5 sm:grid-cols-2">
            {BOXABL_APPROVED_SOURCES.map((source) => (
              <li key={source.videoId} className="flex min-w-0 items-center gap-2 text-xs">
                <a
                  href={source.url}
                  target="_blank"
                  rel="noreferrer"
                  className="min-w-0 flex-1 truncate text-slate-300 hover:text-amber-200 hover:underline"
                >
                  {source.label}
                </a>
                <button
                  type="button"
                  onClick={() => copyLink(source.url)}
                  aria-label={`Copy ${source.label} source link`}
                  className="shrink-0 rounded px-1.5 py-1 text-[10px] text-slate-500 hover:bg-white/5 hover:text-white"
                >
                  Copy
                </button>
              </li>
            ))}
          </ul>
          <a
            href="https://www.youtube.com/@BOXABL/videos"
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-block text-xs text-amber-200 hover:underline"
          >
            Open the official BOXABL YouTube channel ↗
          </a>
        </div>

        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-300">
            Google Drive source
          </h4>
          <div className="mt-3 flex items-center gap-3 rounded-xl border border-white/[0.07] bg-slate-950/50 p-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-400/10 text-sm text-emerald-200" aria-hidden="true">
              ▶
            </span>
            <div className="min-w-0 flex-1">
              <a
                href={BOXABL_DRIVE_SOURCE.url}
                target="_blank"
                rel="noreferrer"
                className="block truncate text-sm font-medium text-white hover:text-amber-200 hover:underline"
              >
                {BOXABL_DRIVE_SOURCE.label}
              </a>
              <p className="mt-1 text-[11px] text-slate-500">
                {driveConfigured
                  ? "Private Drive link import is configured on the server."
                  : "Add Drive OAuth refresh-token credentials to the server to import private files."}
              </p>
            </div>
            <button
              type="button"
              onClick={() => copyLink(BOXABL_DRIVE_SOURCE.url)}
              className="shrink-0 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:border-amber-300/40"
            >
              Copy link
            </button>
          </div>
          <p className="mt-3 text-xs leading-5 text-slate-500">
            Use the URL field to add the copied link. The Drive account used by this server must
            have Viewer access to the file; the assistant’s connected Drive account is separate.
          </p>
        </div>
      </div>

      <div className="border-t border-amber-400/10 px-5 py-4 sm:px-6">
        <p className="text-xs leading-5 text-slate-400">
          Pre-submit checklist: whole Casita shown · BOXABL exact spelling in captions · @boxabl
          in the caption (X requires your own trackable affiliate URL) · publish Instagram manually
          with its native account tag · no Kanye West, stock/equity/ticker talk, political or negative
          bait, or unedited rips. The clipper will create a 0:05–0:08 BOXABL title card, but a person
          must still review the footage, timing, source,
          captions and context before publishing.
        </p>
        {copyStatus ? (
          <p role="status" className="mt-2 text-xs text-amber-200">
            {copyStatus}
          </p>
        ) : null}
      </div>
    </section>
  );
}
