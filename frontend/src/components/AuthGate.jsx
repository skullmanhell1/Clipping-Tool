import { useCallback, useEffect, useState } from "react";

import { api } from "../api.js";
import LoginScreen from "./LoginScreen.jsx";

// U12: decides whether to render the application or a sign-in form.
//
// Two questions, in this order, and the order is the point:
//
//   1. `GET /api/auth/config` - does this deployment use accounts at all? When it does not
//      (the default), the gate renders its children immediately and nothing else here runs.
//      Asking the *session* endpoint first would give a 401 on a single-tenant install and
//      produce a login form for an account system that does not exist.
//   2. `GET /api/auth/session` - am I signed in? A 401 here is the normal signed-out answer,
//      not an error worth showing.
//
// Children are given as a render function rather than plain nodes so the app is not mounted
// at all while signed out. That is deliberate: an unmounted app fires no authenticated
// requests, so no component below needs its own 401 handling, and there is no window in which
// a stray effect races the sign-in.

export default function AuthGate({ children }) {
  const [state, setState] = useState({ status: "loading", enabled: false, user: null });

  const load = useCallback(async () => {
    try {
      const config = await api.authConfig();
      if (!config.auth_enabled) {
        setState({ status: "ready", enabled: false, user: null });
        return;
      }
      try {
        const session = await api.authSession();
        setState({
          status: session.user ? "ready" : "signed-out",
          enabled: true,
          user: session.user || null,
        });
      } catch {
        // 401 is the expected signed-out answer.
        setState({ status: "signed-out", enabled: true, user: null });
      }
    } catch (configError) {
      // The config endpoint is unauthenticated, so a failure here is the server being
      // unreachable rather than a permission problem. Reported, because rendering a login
      // form would blame the user for the backend being down.
      setState({
        status: "error",
        enabled: false,
        user: null,
        error: configError.message || "Could not reach the server.",
      });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const signOut = useCallback(async () => {
    // Caught, not just `finally`-ed: a rejection allowed to escape an onClick handler is an
    // unhandled promise rejection, which is noise in the console and a test failure under a
    // runner that treats it as one.
    try {
      await api.logout();
    } catch {
      // Signed out locally whatever the server said. A logout that failed still means the
      // user asked to leave, and the alternative - staying on screen - is worse on the shared
      // machine that logging out is for.
    }
    setState({ status: "signed-out", enabled: true, user: null });
  }, []);

  if (state.status === "loading") {
    return (
      <div className="flex min-h-full items-center justify-center bg-slate-950 text-slate-500">
        <p role="status">Loading…</p>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="flex min-h-full items-center justify-center bg-slate-950 px-6">
        <p role="alert" className="text-sm text-rose-300">
          {state.error}
        </p>
      </div>
    );
  }

  if (state.status === "signed-out") {
    return (
      <LoginScreen
        onSignedIn={(user) =>
          setState({ status: "ready", enabled: true, user })
        }
      />
    );
  }

  return children({ enabled: state.enabled, user: state.user, signOut });
}
