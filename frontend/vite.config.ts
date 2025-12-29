import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwind from '@tailwindcss/postcss'
import { fileURLToPath, URL } from 'node:url'

// Explicitly wire Tailwind v4 PostCSS plugin via Vite.
// This guarantees it runs even if postcss.config.js isn't picked up.
export default defineConfig({
  plugins: [react()],
  css: {
    postcss: {
      plugins: [tailwind()],
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

})


