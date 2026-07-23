import React from "react";

// Planned pipeline stages, shown as placeholder cards on the dashboard.
// Real controls (upload, job status, clip gallery) arrive in later phases.
const PIPELINE = [
  { title: "Transcribe", desc: "faster-whisper turns speech into timed text." },
  { title: "Select moments", desc: "An LLM picks the most engaging segments." },
  { title: "Cut & reframe", desc: "Vertical 9:16 with face-tracking." },
  { title: "Captions & effects", desc: "Burned-in captions, emoji, overlays." },
  { title: "Metadata", desc: "Auto titles, descriptions, hashtags." },
  { title: "Publish", desc: "Whop, YouTube, TikTok, Instagram, X." },
];

/**
 * Root application component.
 *
 * A dark-themed placeholder dashboard. This is intentionally feature-free for
 * the scaffold phase; it establishes the styling, layout, and build pipeline.
 */
export default function App() {
  return (
    <div className="min-h-full bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-5xl px-6 py-16">
        <span className="inline-block rounded-full border border-slate-700 px-3 py-1 text-xs uppercase tracking-widest text-brand-accent">
          Scaffold &middot; v0.1.0
        </span>

        <h1 className="mt-5 bg-gradient-to-r from-brand-accent to-brand bg-clip-text text-4xl font-bold text-transparent">
          AI Video Clipper
        </h1>

        <p className="mt-4 max-w-2xl text-slate-400">
          Turn long videos into short, vertical, captioned clips ready to
          auto-publish. This dashboard is a placeholder while the pipeline is
          built phase by phase.
        </p>

        <div className="mt-10 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {PIPELINE.map((step, i) => (
            <div
              key={step.title}
              className="rounded-xl border border-slate-800 bg-slate-900/60 p-5 transition hover:border-brand/60"
            >
              <div className="text-xs font-mono text-brand-accent">
                {String(i + 1).padStart(2, "0")}
              </div>
              <h2 className="mt-1 text-lg font-semibold">{step.title}</h2>
              <p className="mt-1 text-sm text-slate-400">{step.desc}</p>
            </div>
          ))}
        </div>

        <footer className="mt-12 text-sm text-slate-500">
          You are responsible for holding the rights to any source footage you
          process. See the README for content &amp; copyright guidance.
        </footer>
      </div>
    </div>
  );
}
