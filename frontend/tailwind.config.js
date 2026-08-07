/** @type {import('tailwindcss').Config} */
export default {
  // `darkMode: "class"` was removed rather than wired up to a toggle, and the reason is that it
  // was never doing anything: there is **not one `dark:` variant** anywhere in `src/` or
  // `index.html`. The palette is hard-coded slate utilities (`bg-slate-950 text-slate-100`), so
  // the `dark` class on `<html>` selected nothing and light mode was unreachable — the config
  // described a feature that did not exist.
  //
  // Implementing the toggle it implied would mean converting every hard-coded colour in seventeen
  // components into a `dark:` pair and designing a light palette. That is a visual redesign, not
  // remediation, and it would have to be judged by eye against the golden renders. Deleting the
  // inert configuration makes the code honest about being dark-only; adding light mode later
  // starts from a truthful baseline instead of a config that pretends it is already half done.
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#6c5ce7",
          accent: "#00d2ff",
        },
      },
    },
  },
  plugins: [],
};
