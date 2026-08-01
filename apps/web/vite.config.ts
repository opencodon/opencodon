import path from 'path'
import { defineConfig } from 'vite'

import { clientBase, fsAllow, publicDir } from '../client/vite.base'

/**
 * Browser host for the session UI.
 *
 * Builds the same `@opencodon/client` source the Electron shell does, into the
 * bundle the FastAPI process serves for the whole browser surface — both `/app`
 * and the site root, which used to be a second SPA under `web/`.
 *
 *   npm run build --workspace @opencodon/web   →   src/opencodon/frontends/cli/web_dist/
 *
 * `base` is absolute (not the desktop build's './') because the app is served
 * from a path prefix while its own routes live on the hash — relative asset
 * URLs would resolve against whatever route the user reloaded on. It stays
 * `/app/` even for the root mount: one set of asset URLs works from either
 * entry point, two would not.
 */

/** Dashboard to develop against. */
const backend = process.env.OPENCODON_DASHBOARD_URL || 'http://127.0.0.1:9119'

/**
 * In production the FastAPI mount injects the bootstrap globals into
 * index.html. The Vite dev server serves that file itself, so nothing injects
 * them and the app boots with no credential. Mirror the injection from the
 * same env var the server reads for its token, so one exported value lets
 * `dashboard` and this dev server agree:
 *
 *   export OPENCODON_DASHBOARD_SESSION_TOKEN=dev-token
 *   opencodon dashboard --no-open & npm run dev --workspace @opencodon/web
 */
const injectBootstrap = () => ({
  name: 'opencodon-web-bootstrap',
  apply: 'serve' as const,
  transformIndexHtml: (html: string) =>
    html.replace(
      '</head>',
      `<script>window.__OPENCODON_SESSION_TOKEN__=${JSON.stringify(
        process.env.OPENCODON_DASHBOARD_SESSION_TOKEN || ''
      )};window.__OPENCODON_BASE_PATH__="";window.__OPENCODON_AUTH_REQUIRED__=false;</script></head>`
    )
})

export default defineConfig({
  ...clientBase,
  base: '/app/',
  publicDir,
  plugins: [...(clientBase.plugins ?? []), injectBootstrap()],
  build: {
    ...clientBase.build,
    outDir: path.resolve(__dirname, '../../src/opencodon/frontends/cli/web_dist'),
    emptyOutDir: true,
    // The shared base disables code splitting because electron-builder can OOM
    // scanning thousands of files when packaging the desktop app. Nothing
    // packages the web build, and the single-chunk mode is actively harmful
    // here: it ships ~28 MB on first paint, and collapsing shiki's re-exports
    // into one scope trips a rolldown helper-naming bug that leaves
    // `__reExport$1` undefined — a blank page with no React error, because the
    // module graph dies before the app mounts. Split instead.
    rolldownOptions: {
      output: {
        codeSplitting: true
      }
    }
  },
  server: {
    host: '127.0.0.1',
    port: 5175,
    strictPort: true,
    fs: { allow: fsAllow },
    // Every API and socket call forwards to a running dashboard. `ws: true`
    // matters most: the gateway, PTY, and shell are all WebSockets, and
    // without it those upgrades 404 and the app boots disconnected.
    proxy: {
      '/api': { target: backend, changeOrigin: true, ws: true }
    }
  }
})
