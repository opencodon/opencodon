import { defineConfig } from 'vite'

import { clientBase, fsAllow, publicDir } from '../client/vite.base'

/** Electron renderer build. `base: './'` because the packaged app loads
 *  index.html from disk over the file: protocol, where absolute asset paths
 *  would resolve against the filesystem root. */
export default defineConfig({
  ...clientBase,
  base: './',
  publicDir,
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    fs: { allow: fsAllow }
  },
  preview: { host: '127.0.0.1', port: 4174 }
})
