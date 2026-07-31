import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

/**
 * PB7: a month calendar of scheduled posts, with rescheduling and best-time suggestions.
 *
 * Scheduling existed as a single `datetime-local` input and nothing else: an operator could set a
 * time, and then had no way to see what was already scheduled, move it, or cancel it. The only
 * recourse for a wrong time was to let it publish.
 *
 * Every state is shown, not only pending ones. A calendar that hid what had already gone out would
 * show an empty week the operator had in fact filled, and "what did I post on Tuesday" is the same
 * question as "what am I posting on Thursday".
 */

const STATE_STYLES = {
  scheduled: "bg-sky-500/20 text-sky-200 border-sky-500/40",
  queued: "bg-amber-500/20 text-amber-200 border-amber-500/40",
  uploading: "bg-amber-500/30 text-amber-100 border-amber-400/50",
  published: "bg-emerald-500/20 text-emerald-200 border-emerald-500/40",
  private: "bg-emerald-500/10 text-emerald-200/80 border-emerald-500/30",
  draft: "bg-slate-500/20 text-slate-200 border-slate-500/40",
  review_required: "bg-violet-500/20 text-violet-200 border-violet-500/40",
  failed: "bg-rose-500/20 text-rose-200 border-rose-500/40",
};

// Only these can be moved or cancelled; the API enforces the same set and returns 409 otherwise.
const PENDING_STATES = new Set(["queued", "scheduled"]);

const PLATFORMS = ["tiktok", "instagram", "youtube", "youtube_shorts", "x", "whop"];

const startOfMonth = (date) => new Date(date.getFullYear(), date.getMonth(), 1);
const endOfMonth = (date) => new Date(date.getFullYear(), date.getMonth() + 1, 0, 23, 59, 59);

/** The Monday-first grid of days covering `month`, padded to whole weeks. */
const monthGrid = (month) => {
  const first = startOfMonth(month);
  const offset = (first.getDay() + 6) % 7; // JS weeks start Sunday; the grid starts Monday.
  const start = new Date(first);
  start.setDate(first.getDate() - offset);
  return Array.from({ length: 42 }, (_, index) => {
    const day = new Date(start);
    day.setDate(start.getDate() + index);
    return day;
  });
};

const sameDay = (a, b) =>
  a.getFullYear() === b.getFullYear() &&
  a.getMonth() === b.getMonth() &&
  a.getDate() === b.getDate();

const timeLabel = (epoch) =>
  new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** `datetime-local` wants local wall-clock text, not an ISO/UTC string. */
const toLocalInput = (epoch) => {
  const date = new Date(epoch * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
};

export default function ScheduleCalendar({ onError }) {
  const [month, setMonth] = useState(() => startOfMonth(new Date()));
  const [attempts, setAttempts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(null);
  const [editing, setEditing] = useState("");
  const [busy, setBusy] = useState(false);
  const [platform, setPlatform] = useState("tiktok");
  const [suggestions, setSuggestions] = useState([]);
  const [basis, setBasis] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const from = startOfMonth(month).getTime() / 1000;
      const to = endOfMonth(month).getTime() / 1000;
      const data = await api.schedule(from, to);
      setAttempts(data.attempts || []);
    } catch (error) {
      onError?.(error.message || "Could not load the schedule");
    } finally {
      setLoading(false);
    }
  }, [month, onError]);

  useEffect(() => {
    load();
  }, [load]);

  const loadSuggestions = useCallback(async () => {
    try {
      const data = await api.scheduleSuggestions(platform, 7, 2);
      setSuggestions(data.suggestions || []);
      setBasis(data.basis || "");
    } catch (error) {
      onError?.(error.message || "Could not load suggestions");
    }
  }, [platform, onError]);

  const byDay = useMemo(() => {
    const map = new Map();
    for (const attempt of attempts) {
      if (!attempt.scheduled_at) continue;
      const key = new Date(attempt.scheduled_at * 1000).toDateString();
      map.set(key, [...(map.get(key) || []), attempt]);
    }
    for (const list of map.values()) list.sort((a, b) => a.scheduled_at - b.scheduled_at);
    return map;
  }, [attempts]);

  const days = useMemo(() => monthGrid(month), [month]);
  const today = new Date();

  const shiftMonth = (delta) =>
    setMonth(new Date(month.getFullYear(), month.getMonth() + delta, 1));

  const openAttempt = (attempt) => {
    setSelected(attempt);
    setEditing(attempt.scheduled_at ? toLocalInput(attempt.scheduled_at) : "");
  };

  const applyReschedule = async () => {
    if (!selected || !editing) return;
    const epoch = new Date(editing).getTime() / 1000;
    if (!Number.isFinite(epoch)) {
      onError?.("That is not a valid date and time");
      return;
    }
    setBusy(true);
    try {
      await api.reschedulePublishAttempt(selected.id, epoch);
      setSelected(null);
      await load();
    } catch (error) {
      onError?.(error.message || "Could not reschedule");
    } finally {
      setBusy(false);
    }
  };

  const cancelAttempt = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.cancelPublishAttempt(selected.id);
      setSelected(null);
      await load();
    } catch (error) {
      onError?.(error.message || "Could not cancel");
    } finally {
      setBusy(false);
    }
  };

  const monthLabel = month.toLocaleDateString([], { month: "long", year: "numeric" });

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => shiftMonth(-1)}
            className="rounded-lg border border-slate-700 px-2 py-1 text-sm text-slate-300 hover:border-brand-accent"
            aria-label="Previous month"
          >
            ‹
          </button>
          <h3 className="text-sm font-semibold text-slate-100">{monthLabel}</h3>
          <button
            type="button"
            onClick={() => shiftMonth(1)}
            className="rounded-lg border border-slate-700 px-2 py-1 text-sm text-slate-300 hover:border-brand-accent"
            aria-label="Next month"
          >
            ›
          </button>
          <button
            type="button"
            onClick={() => setMonth(startOfMonth(new Date()))}
            className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:border-brand-accent"
          >
            Today
          </button>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={platform}
            onChange={(event) => setPlatform(event.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200"
            aria-label="Platform for suggestions"
          >
            {PLATFORMS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={loadSuggestions}
            className="rounded-lg border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-brand-accent"
          >
            Suggest times
          </button>
        </div>
      </header>

      {loading ? <p className="mb-2 text-xs text-slate-500">Loading schedule…</p> : null}

      <div className="grid grid-cols-7 gap-1 text-center text-[10px] uppercase tracking-wide text-slate-500">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((label) => (
          <div key={label}>{label}</div>
        ))}
      </div>
      <div className="mt-1 grid grid-cols-7 gap-1">
        {days.map((day) => {
          const inMonth = day.getMonth() === month.getMonth();
          const items = byDay.get(day.toDateString()) || [];
          return (
            <div
              key={day.toISOString()}
              className={`min-h-[68px] rounded-lg border p-1 text-left ${
                inMonth ? "border-slate-800 bg-slate-950/60" : "border-slate-900 bg-slate-950/20"
              } ${sameDay(day, today) ? "ring-1 ring-brand-accent" : ""}`}
            >
              <div className={`text-[10px] ${inMonth ? "text-slate-400" : "text-slate-600"}`}>
                {day.getDate()}
              </div>
              <div className="mt-1 space-y-1">
                {items.slice(0, 3).map((attempt) => (
                  <button
                    key={attempt.id}
                    type="button"
                    onClick={() => openAttempt(attempt)}
                    title={`${attempt.platform} — ${attempt.state}`}
                    className={`block w-full truncate rounded border px-1 py-0.5 text-left text-[10px] ${
                      STATE_STYLES[attempt.state] || STATE_STYLES.draft
                    }`}
                  >
                    {timeLabel(attempt.scheduled_at)} {attempt.platform}
                  </button>
                ))}
                {items.length > 3 ? (
                  <div className="text-[10px] text-slate-500">+{items.length - 3} more</div>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>

      {suggestions.length ? (
        <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/50 p-2">
          <h4 className="text-xs font-semibold text-slate-200">
            Suggested times for {platform}
          </h4>
          {/* The basis is rendered, not hidden: these are published heuristics, not this
              account's measured engagement. */}
          {basis ? <p className="mt-1 text-[10px] leading-snug text-slate-500">{basis}</p> : null}
          <div className="mt-2 flex flex-wrap gap-1">
            {suggestions.slice(0, 8).map((item) => (
              <span
                key={item.at}
                className="rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-300"
              >
                {new Date(item.at * 1000).toLocaleString([], {
                  weekday: "short",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {selected ? (
        <div className="mt-3 rounded-lg border border-slate-700 bg-slate-950 p-3">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-semibold text-slate-100">
              {selected.platform} — {selected.state}
            </h4>
            <button
              type="button"
              onClick={() => setSelected(null)}
              className="text-xs text-slate-400 hover:text-slate-200"
            >
              Close
            </button>
          </div>
          {PENDING_STATES.has(selected.state) ? (
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <label className="text-[10px] text-slate-400">
                Move to
                <input
                  type="datetime-local"
                  value={editing}
                  onChange={(event) => setEditing(event.target.value)}
                  className="mt-1 block rounded-lg border border-slate-700 bg-slate-950 p-1 text-xs text-slate-100"
                />
              </label>
              <button
                type="button"
                disabled={busy}
                onClick={applyReschedule}
                className="rounded-lg bg-brand-accent px-2 py-1 text-xs font-semibold text-slate-950 disabled:opacity-50"
              >
                {busy ? "Saving…" : "Reschedule"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={cancelAttempt}
                className="rounded-lg border border-rose-500/40 px-2 py-1 text-xs text-rose-200 disabled:opacity-50"
              >
                Cancel post
              </button>
            </div>
          ) : (
            <p className="mt-2 text-[10px] text-slate-500">
              This attempt is {selected.state} and can no longer be moved or cancelled.
              {selected.url ? (
                <>
                  {" "}
                  <a
                    href={selected.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-brand-accent underline"
                  >
                    View post
                  </a>
                </>
              ) : null}
            </p>
          )}
          {selected.error ? (
            <p className="mt-2 break-words text-[10px] text-rose-300">{selected.error}</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
