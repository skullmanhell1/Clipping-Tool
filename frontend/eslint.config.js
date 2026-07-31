// ESLint flat configuration.
//
// The `lint` script has always existed in package.json but eslint itself was never a
// dependency and no config file existed, so `npm run lint` failed outright — the project
// had a lint command that could not lint.
//
// Flat config (eslint 9) rather than .eslintrc: the legacy format is deprecated and
// eslint 10 drops it.
import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  {
    // Build output and dependencies are not ours to lint.
    // `coverage/**` joins the generated-output list now that `npm run test:coverage` exists:
    // vitest's lcov reporter writes its own JS, which eslint then reports on — a warning about
    // a file nobody wrote and nobody will fix.
    ignores: ["dist/**", "node_modules/**", "coverage/**"],
  },
  js.configs.recommended,
  {
    files: ["**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.es2022,
      },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    settings: {
      // Avoids a startup warning about an unspecified React version.
      react: { version: "detect" },
    },
    plugins: {
      react,
      "react-hooks": reactHooks,
    },
    rules: {
      ...react.configs.flat.recommended.rules,
      // This codebase uses the automatic JSX runtime, so React need not be in scope
      // and JSX-scope/import-React rules would be false positives.
      "react/react-in-jsx-scope": "off",
      "react/jsx-uses-react": "off",
      // Props are not typed with propTypes anywhere in this project; requiring them
      // would produce hundreds of findings that say nothing about correctness.
      "react/prop-types": "off",
      // Narrowed rather than disabled. By default this rule also flags ' and " in JSX
      // text, which are ordinary prose here ("don't", quoted names) and render
      // correctly. The characters worth catching are > and }, where a stray one is a
      // genuine mistake that renders as literal text or breaks the expression.
      "react/no-unescaped-entities": ["error", { forbid: [">", "}"] }],
      // The two rules that catch genuine bugs rather than style.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      // An unused variable is usually a leftover or a typo'd identifier. Argument
      // patterns prefixed with _ are conventionally intentional.
      "no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // Test files additionally run under vitest's globals.
    files: ["**/*.test.{js,jsx}", "**/test/**/*.{js,jsx}"],
    languageOptions: {
      globals: {
        ...globals.node,
        describe: "readonly",
        it: "readonly",
        expect: "readonly",
        vi: "readonly",
        beforeEach: "readonly",
        afterEach: "readonly",
      },
    },
  },
  {
    // Config files run in Node, not the browser.
    files: ["*.config.js", "postcss.config.js", "tailwind.config.js"],
    languageOptions: { globals: { ...globals.node } },
  },
];
