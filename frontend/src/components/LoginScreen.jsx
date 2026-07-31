import { useCallback, useState } from "react";

import { api } from "../api.js";

// U12: the sign-in form. Rendered by AuthGate instead of the app, so nothing behind it is
// mounted while signed out - which means no component has to defend itself against a 401,
// and a stray render cannot fire an authenticated request.
//
// The error shown is whatever the API said, and the API deliberately says the same thing for
// a wrong password, an unknown username and a disabled account. Reproducing it verbatim keeps
// that property: any attempt here to be more helpful ("no such user") would hand back the
// username enumeration the backend is careful not to give.

export default function LoginScreen({ onSignedIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = useCallback(
    async (event) => {
      event.preventDefault();
      if (busy) return;
      setBusy(true);
      setError("");
      try {
        const data = await api.login(username, password);
        onSignedIn?.(data.user);
      } catch (loginError) {
        setError(loginError.message || "Could not sign in.");
        setPassword("");
      } finally {
        setBusy(false);
      }
    },
    [busy, onSignedIn, password, username],
  );

  const inputClass =
    "w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm " +
    "text-slate-100 outline-none focus:border-brand-accent";

  return (
    <div className="flex min-h-full items-center justify-center bg-slate-950 px-6 py-16">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-2xl border border-slate-800 bg-slate-900 p-6"
        aria-labelledby="login-heading"
      >
        <div>
          <h1
            id="login-heading"
            className="bg-gradient-to-r from-brand-accent to-brand bg-clip-text text-2xl font-bold text-transparent"
          >
            AI Video Clipper
          </h1>
          <p className="mt-1 text-sm text-slate-400">Sign in to continue.</p>
        </div>

        <div className="space-y-1">
          <label htmlFor="login-username" className="text-xs uppercase tracking-wide text-slate-500">
            Username
          </label>
          <input
            id="login-username"
            className={inputClass}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
          />
        </div>

        <div className="space-y-1">
          <label htmlFor="login-password" className="text-xs uppercase tracking-wide text-slate-500">
            Password
          </label>
          <input
            id="login-password"
            type="password"
            className={inputClass}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </div>

        {error ? (
          <p
            role="alert"
            className="rounded-lg border border-rose-500/40 bg-rose-500/10 px-2 py-1.5 text-xs text-rose-300"
          >
            {error}
          </p>
        ) : null}

        <button
          type="submit"
          disabled={busy || !username || !password}
          className="w-full rounded-lg bg-brand-accent py-2 text-sm font-semibold text-slate-950 disabled:opacity-40"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
