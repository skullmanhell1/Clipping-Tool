/** @type {import('tailwindcss').Config} */
export default {
  // Dark theme is enabled via the `dark` class on <html> (see index.html).
  darkMode: "class",
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
