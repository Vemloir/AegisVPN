import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API lives on the same origin in production (nginx proxies /api to the
// FastAPI service). In dev we proxy to it directly so cookies stay first-party.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.js',
    include: ['src/**/*.test.{js,jsx}'],
  },
  // Baked into the bundle (not shown on the page) so "which build is this?"
  // stays answerable by grepping the served JS, without putting a timestamp
  // in front of users.
  define: {
    __BUILD_STAMP__: JSON.stringify(new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC'),
  },
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000' /* uvicorn src.api.main:app --port 8000 */, changeOrigin: true },
    },
  },
})
