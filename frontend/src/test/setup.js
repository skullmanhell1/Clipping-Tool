// Vitest setup, applied before every test file.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Unmount anything rendered in a test. Without this, components persist between tests
// and a query like getByRole can match a leftover from an earlier test — which produces
// failures that depend on file order rather than on the code under test.
afterEach(() => {
  cleanup();
});
