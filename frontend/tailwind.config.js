/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // Warm industrial palette
        warehouse: {
          bg: {
            primary: '#1a1915',
            secondary: '#242118',
            tertiary: '#2d2a23',
          },
          text: {
            primary: '#e8e4dc',
            secondary: '#9a958a',
            tertiary: '#5c584f',
          },
          accent: '#f59e0b',
          border: '#3d3930',
        },
        // Legacy brand color (can be removed later)
        brand: { green: "#b1f11e" },
      },
      fontFamily: {
        display: ['Space Grotesk', 'system-ui', 'sans-serif'],
        body: ['IBM Plex Sans', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
};
