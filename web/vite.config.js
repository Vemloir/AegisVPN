import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API lives on the same origin in production (nginx proxies /api to the
// FastAPI service). In dev we proxy to it directly so cookies stay first-party.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000' /* uvicorn src.api.main:app --port 8000 */, changeOrigin: true },
    },
  },
})
