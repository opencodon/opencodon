import { defineConfig } from 'vite'

import { clientBase, fsAllow, publicDir } from './vite.base'

/** Config for tooling that runs against the UI package directly (vitest).
 *  Hosts build with their own config; see apps/desktop and apps/web. */
export default defineConfig({ ...clientBase, publicDir, server: { fs: { allow: fsAllow } } })
