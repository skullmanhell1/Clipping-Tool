import { useEffect, useState } from "react";
import { api } from "../api.js";
const fmt=t=>t?new Date(t*1000).toLocaleString():"—";
const colors={published:"text-emerald-400",private:"text-blue-400",draft:"text-blue-400",failed:"text-rose-400",review_required:"text-amber-400",scheduled:"text-violet-400",queued:"text-slate-300",uploading:"text-cyan-400"};

// PB2: which actions an attempt in a given state can take.
//
// The /approve and /retry endpoints existed with zero references anywhere in the frontend,
// so an attempt that came back `review_required` stopped permanently: three of the five
// publishers can return that state (Instagram and X without direct-publish approval, Whop
// when the upload could not be attached to a target) and the dashboard offered no way to act.
//
// Approve and retry stay separate controls rather than one "resume" button, because they are
// different decisions. Approve escalates a review-mode attempt into a live post; retry re-runs
// it exactly as submitted, for transient trouble like an expired token or a missing file. A
// single button would have to guess which the user meant, and guessing wrong publishes
// something that was deliberately held back.
const CAN_APPROVE = new Set(["review_required"]);
const CAN_RETRY = new Set(["review_required", "failed"]);

function AttemptActions({ attempt, onDone }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const approvable = CAN_APPROVE.has(attempt.state);
  const retryable = CAN_RETRY.has(attempt.state);
  if (!approvable && !retryable) return <span className="text-slate-600">—</span>;

  const run = (action, call) => {
    setBusy(action);
    setError("");
    call(attempt.id)
      // Refresh from the server rather than patching local state: the attempt is queued, and
      // the worker may already have advanced it past `queued` by the time this resolves.
      .then(() => onDone())
      .catch((e) => setError(e?.message || "failed"))
      .finally(() => setBusy(""));
  };

  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-2">
        {approvable && (
          <button
            type="button"
            disabled={!!busy}
            onClick={() => run("approve", api.approvePublishAttempt)}
            title="Publish this attempt directly, overriding the review hold"
            className="rounded border border-emerald-700 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-950 disabled:opacity-50"
          >
            {busy === "approve" ? "Approving…" : "Approve"}
          </button>
        )}
        {retryable && (
          <button
            type="button"
            disabled={!!busy}
            onClick={() => run("retry", api.retryPublishAttempt)}
            title="Re-run this attempt unchanged, without escalating a review hold"
            className="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-50"
          >
            {busy === "retry" ? "Retrying…" : "Retry"}
          </button>
        )}
      </div>
      {error && <span className="text-xs text-rose-400">{error}</span>}
    </div>
  );
}

export default function HistoryView(){const [data,setData]=useState({clips:[],publish_attempts:[]});const [filter,setFilter]=useState("");const load=()=>api.history(filter).then(setData).catch(()=>{});useEffect(()=>{load();const i=setInterval(load,3000);return()=>clearInterval(i)},[filter]);
 return <section className="space-y-6"><div className="flex items-center justify-between"><h2 className="text-xl font-semibold">History</h2><select value={filter} onChange={e=>setFilter(e.target.value)} className="rounded-lg border border-slate-700 bg-slate-900 p-2 text-sm"><option value="">All platforms</option>{["whop","youtube","tiktok","instagram","x"].map(p=><option key={p}>{p}</option>)}</select></div>
 <div className="overflow-x-auto rounded-xl border border-slate-800"><table className="w-full text-left text-sm"><thead className="bg-slate-900 text-slate-400"><tr><th className="p-3">Time</th><th>Platform</th><th>Campaign / Account</th><th>Status</th><th>Message</th><th>Link</th><th>Actions</th></tr></thead><tbody>{data.publish_attempts.map(a=><tr key={a.id} className="border-t border-slate-800"><td className="p-3 text-slate-500">{fmt(a.created_at)}</td><td className="capitalize">{a.platform}</td><td>{a.campaign_id||"—"}<div className="text-xs text-slate-500">{a.account_id}</div></td><td className={colors[a.state]||""}>{a.state}</td><td className="max-w-xs text-slate-400">{a.error||a.message||"—"}</td><td>{a.url?<a className="text-brand-accent" href={a.url} target="_blank" rel="noreferrer">Open</a>:"—"}</td><td><AttemptActions attempt={a} onDone={load}/></td></tr>)}{!data.publish_attempts.length&&<tr><td colSpan="7" className="p-8 text-center text-slate-500">No publish attempts yet.</td></tr>}</tbody></table></div>
 <div><h3 className="mb-3 font-medium">Created clips ({data.clips.length})</h3><div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{data.clips.map(c=><div key={c.id} className="rounded-lg border border-slate-800 bg-slate-900 p-3"><b>{c.title||c.filename}</b><div className="text-xs text-slate-500">{fmt(c.created_at)} · score {Math.round(c.score||0)}</div><div className="mt-1 text-xs text-slate-400">{(c.hashtags||[]).join(" ")}</div></div>)}</div></div></section>}
