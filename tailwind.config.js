/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./loja/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        "brand-pink": "#fc03a9",
        "brand-pink-dark": "#e60090",
        "brand-pink-light": "#fdf0f5",
      },
    },
  },
  plugins: [],
};
