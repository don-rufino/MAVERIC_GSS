import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The build SHA is no longer baked into the bundle. The backend resolves
// it at launch via `git rev-parse --short HEAD` and returns it through
// /api/config as general.build_sha, so backend-only commits no longer
// dirty dist/.

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // Resolves the "@/*" -> "./src/*" mapping from tsconfig.app.json's
    // compilerOptions.paths natively — Vite doesn't read tsconfig paths
    // unless this is explicitly turned on. (vitest.config.ts hand-rolls
    // the same mapping via resolve.alias instead, since Vitest's config
    // predates this option.)
    tsconfigPaths: true,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1000,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
})
