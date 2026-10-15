import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// In dev the console calls /api/... and Vite forwards it to the FastAPI backend, so no CORS setup is needed.
// For a deployed build, set VITE_API_URL to the backend's absolute URL.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.HINDSIGHT_API ?? 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
})
