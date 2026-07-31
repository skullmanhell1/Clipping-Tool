import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api.js";
import AuthGate from "./AuthGate.jsx";

/**
 * U12: the gate decides whether the application is mounted at all.
 *
 * The tests below are about the two questions it asks and the order it asks them in. Getting
 * that order wrong is not a cosmetic bug: asking about the session first would show a login
 * form on every single-tenant install, which is the default configuration.
 */

const child = (auth) => <div data-testid="app">app for {auth.user?.username ?? "nobody"}</div>;

describe("AuthGate (U12)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the app immediately when the deployment has no accounts", async () => {
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: false });
    const session = vi.spyOn(api, "authSession");
    render(<AuthGate>{child}</AuthGate>);
    expect(await screen.findByTestId("app")).toBeInTheDocument();
    // And it does not even ask: a single-tenant install has no session to report.
    expect(session).not.toHaveBeenCalled();
  });

  it("shows the sign-in form when auth is on and nobody is signed in", async () => {
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockRejectedValue(new Error("Not authenticated."));
    render(<AuthGate>{child}</AuthGate>);
    expect(await screen.findByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.queryByTestId("app")).not.toBeInTheDocument();
  });

  it("does not mount the app while signed out", async () => {
    // The reason the children are a render function: an unmounted app fires no authenticated
    // requests, so nothing below needs its own 401 handling.
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockRejectedValue(new Error("Not authenticated."));
    const spy = vi.fn(child);
    render(<AuthGate>{spy}</AuthGate>);
    await screen.findByLabelText(/username/i);
    expect(spy).not.toHaveBeenCalled();
  });

  it("renders the app for an existing session without asking to sign in", async () => {
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockResolvedValue({
      user: { id: "u1", username: "alice", is_admin: false },
    });
    render(<AuthGate>{child}</AuthGate>);
    expect(await screen.findByText(/app for alice/)).toBeInTheDocument();
  });

  it("treats a session response with a null user as signed out", async () => {
    // The endpoint answers 200 with `user: null` when auth is off; if the config said
    // otherwise, trusting the 200 alone would mount the app with no user.
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockResolvedValue({ user: null });
    render(<AuthGate>{child}</AuthGate>);
    expect(await screen.findByLabelText(/username/i)).toBeInTheDocument();
  });

  it("mounts the app after a successful sign-in", async () => {
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockRejectedValue(new Error("Not authenticated."));
    vi.spyOn(api, "login").mockResolvedValue({
      user: { id: "u1", username: "alice", is_admin: false },
    });

    render(<AuthGate>{child}</AuthGate>);
    await userEvent.type(await screen.findByLabelText(/username/i), "alice");
    await userEvent.type(screen.getByLabelText(/password/i), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByText(/app for alice/)).toBeInTheDocument();
  });

  it("reports a server it cannot reach instead of blaming the user", async () => {
    vi.spyOn(api, "authConfig").mockRejectedValue(new Error("Failed to fetch"));
    render(<AuthGate>{child}</AuthGate>);
    expect(await screen.findByRole("alert")).toHaveTextContent(/failed to fetch/i);
    expect(screen.queryByLabelText(/username/i)).not.toBeInTheDocument();
  });

  it("returns to the sign-in form on sign out", async () => {
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockResolvedValue({
      user: { id: "u1", username: "alice", is_admin: false },
    });
    vi.spyOn(api, "logout").mockResolvedValue({ ok: true });

    render(
      <AuthGate>
        {(auth) => (
          <button type="button" onClick={auth.signOut}>
            leave
          </button>
        )}
      </AuthGate>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "leave" }));
    expect(await screen.findByLabelText(/username/i)).toBeInTheDocument();
  });

  it("signs out locally even when the logout request fails", async () => {
    // A logout that errored still means the user asked to leave, and staying on screen is
    // worse on the shared machine that logging out is for.
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockResolvedValue({
      user: { id: "u1", username: "alice", is_admin: false },
    });
    vi.spyOn(api, "logout").mockRejectedValue(new Error("network down"));

    render(
      <AuthGate>
        {(auth) => (
          <button type="button" onClick={auth.signOut}>
            leave
          </button>
        )}
      </AuthGate>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "leave" }));
    expect(await screen.findByLabelText(/username/i)).toBeInTheDocument();
  });
});

describe("LoginScreen (U12)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "authConfig").mockResolvedValue({ auth_enabled: true });
    vi.spyOn(api, "authSession").mockRejectedValue(new Error("Not authenticated."));
  });

  it("cannot be submitted empty", async () => {
    render(<AuthGate>{child}</AuthGate>);
    expect(await screen.findByRole("button", { name: /sign in/i })).toBeDisabled();
  });

  it("shows the server's message verbatim, which does not say which half was wrong", async () => {
    // The API answers the same thing for a wrong password, an unknown user and a disabled
    // account. Being more "helpful" here would reintroduce username enumeration.
    vi.spyOn(api, "login").mockRejectedValue(
      new Error("Incorrect username or password."),
    );
    render(<AuthGate>{child}</AuthGate>);
    await userEvent.type(await screen.findByLabelText(/username/i), "alice");
    await userEvent.type(screen.getByLabelText(/password/i), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Incorrect username or password.",
    );
  });

  it("clears the password after a failed attempt", async () => {
    vi.spyOn(api, "login").mockRejectedValue(new Error("Incorrect username or password."));
    render(<AuthGate>{child}</AuthGate>);
    await userEvent.type(await screen.findByLabelText(/username/i), "alice");
    const password = screen.getByLabelText(/password/i);
    await userEvent.type(password, "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await screen.findByRole("alert");
    expect(password).toHaveValue("");
  });

  it("uses a password input so the value is masked", async () => {
    render(<AuthGate>{child}</AuthGate>);
    expect(await screen.findByLabelText(/password/i)).toHaveAttribute("type", "password");
  });
});
