import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      ...Object.fromEntries(['components', 'graph', 'interactions', 'parser', 'renderer'].map((name) => [
        `@genui-sdk/${name}`,
        fileURLToPath(new URL(`../card_renderer/runtime/${name}/src/index.ts`, import.meta.url)),
      ])),
      '@widget-debug/end-to-end': fileURLToPath(new URL('../end_to_end_debug/frontend/src/index.ts', import.meta.url)),
      '@widget-debug/interface': fileURLToPath(new URL('../interface_debug/frontend/src/index.ts', import.meta.url)),
      '@widget-debug/card-renderer': fileURLToPath(new URL('../card_renderer/frontend/src/index.ts', import.meta.url)),
    },
  },
  test: {
    include: ['../tests/frontend/platform/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['../tests/frontend/platform/setup.ts'],
  },
});
