import { defineConfig } from 'vitest/config'

import { clientBase } from './vite.base'

// The shared base is spread in directly rather than referenced through
// `test.extends`: the suites import via the `@/…` alias, so if the resolver
// config doesn't reach vitest, every one of them fails at import time.
export default defineConfig({
  ...clientBase,
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform,
    // which can exceed vitest's 5000ms default under CI/load. 15s gives the
    // cold start headroom without masking genuinely hung tests.
    testTimeout: 15_000
  }
})
