import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

const platformRoot = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  plugins: [react()],
  base: '/debug/',
  resolve: {
    alias: {
      '@platform': `${platformRoot}/src`,
      '@widget-debug/end-to-end': fileURLToPath(
        new URL('../end_to_end_debug/frontend/src/index.ts', import.meta.url),
      ),
      '@widget-debug/interface': fileURLToPath(
        new URL('../interface_debug/frontend/src/index.ts', import.meta.url),
      ),
      '@widget-debug/card-renderer': fileURLToPath(
        new URL('../card_renderer/frontend/src/index.ts', import.meta.url),
      ),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/debug': {
        target: 'http://127.0.0.1:8888',
        ws: true,
      },
    },
  },
  build: {
    outDir: '../end_to_end_debug/backend/static',
    emptyOutDir: true,
  },
});
