/** @type {import('tailwindcss').Config} */
export default {
  content: ["./src/**/*.{astro,html,js,md,ts}"],
  theme: {
    extend: {
      colors: {
        paper: "#fbf8f1",
        ink: "#141311",
        muted: "#625f58",
        line: "#ded7ca",
        civic: "#174a7c",
        "civic-dark": "#0d3154",
        wash: "#f2ede3",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        serif: ['"Source Serif 4"', "Georgia", "ui-serif", "serif"],
      },
      boxShadow: {
        hairline: "0 1px 0 rgba(20, 19, 17, 0.08)",
      },
      maxWidth: {
        reading: "46rem",
      },
    },
  },
  plugins: [],
};
