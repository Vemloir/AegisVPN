import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API lives on the same origin in production (nginx proxies /api to the
// FastAPI service). In dev we proxy to it directly so cookies stay first-party.
export default defineConfig({
  plugins: [react()],
  // Baked into the bundle at build time and shown in the footer, so "which
  // build is this browser actually rendering" is answerable by looking at
  // the page instead of digging through DevTools.
  define: {
    __BUILD_STAMP__: JSON.stringify(new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC'),
  },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000' /* uvicorn src.api.main:app --port 8000 */, changeOrigin: true },
    },
  },
})
