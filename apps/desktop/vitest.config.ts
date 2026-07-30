import { defineConfig } from 'vitest/config'

/** The renderer's tests live with the UI, in apps/client. What remains here is
 *  the Electron main process and the packaging scripts. */
export default defineConfig({
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}']
  }
})
