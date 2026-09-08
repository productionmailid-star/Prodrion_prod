/** Tailwind config for the pre-built dashboard CSS (see BUILD.md).
 *  Mirrors the inline `tailwind.config` blocks in the HTML files. */
module.exports = {
  content: ["./index.html", "./dashboard.html", "./auth/*.html"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        primary: "#1f7a63",
        "primary-dark": "#165948",
        "background-light": "#ffffff",
        "background-subtle": "#F9FAFB",
        "background-dark": "#131f1c",
        "border-light": "#e2e8e7",
        "text-main": "#121716",
        "text-muted": "#6B7280",
      },
      fontFamily: {
        display: ["Inter", "sans-serif"],
        body: ["Inter", "sans-serif"],
      },
      boxShadow: {
        soft: "0 4px 6px -1px rgba(0,0,0,0.05)",
        glow: "0 0 15px rgba(31,122,99,0.15)",
      },
    },
  },
  plugins: [
    require("@tailwindcss/forms"),
    require("@tailwindcss/container-queries"),
  ],
};
