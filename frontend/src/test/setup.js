// Vitest setup, applied before every test file.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// Unmount anything rendered in a test. Without this, components persist between tests
// and a query like getByRole can match a leftover from an earlier test — which produces
// failures that depend on file order rather than on the code under test.
afterEach(() => {
  cleanup();
});

// Turn a failed propType into a failed test.
//
// This is what makes the propTypes declarations added in Phase 5.4 worth having. React reports a
// violation by calling `console.error` and then rendering anyway, so on its own a wrong prop is a
// line of grey text in a log nobody reads — the suite stays green and the declaration is
// decoration. The reason for choosing propTypes over an incremental TypeScript migration was that
// they check every boundary at once inside the suite that already exists; that is only true if the
// suite actually fails.
//
// Scoped to prop-type warnings on purpose. React also routes genuine application errors through
// `console.error` (a thrown render caught by an error boundary, for instance) and several tests here
// deliberately drive components into their failure paths, so blanket-failing on any `console.error`
// would break tests that are asserting exactly the right thing. The pattern below is only ever
// emitted by React's own development-mode validation, never by our code.
//
// It has to match the *uninterpolated* format string. React does not build the message before
// calling console.error; it passes `"Warning: Failed %s type: %s%s"` with the placeholders as
// separate arguments, so a pattern like `Failed \w+ type:` silently never matches — `%` is not a
// word character. `\S+` matches both the literal `%s` and the interpolated `prop`.
const REACT_VALIDATION_WARNING = /Failed\s+\S+\s+type:/;

beforeEach(() => {
  const realError = console.error;
  // `vi.spyOn` so that a test which wants to assert on or silence console output can still
  // override it, and so the original is restored automatically between files.
  vi.spyOn(console, "error").mockImplementation((...args) => {
    const first = typeof args[0] === "string" ? args[0] : "";
    if (REACT_VALIDATION_WARNING.test(first)) {
      // Format the warning the way React would have, so the failure names the component and the
      // prop rather than showing raw `%s` placeholders.
      const rest = args.slice(1);
      const message = first.replace(/%s/g, () => (rest.length ? String(rest.shift()) : "%s"));
      throw new Error(`React prop validation failed: ${message}`);
    }
    realError(...args);
  });
});
