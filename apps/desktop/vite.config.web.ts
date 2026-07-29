import path from 'path'
import { defineConfig, mergeConfig } from 'vite'

import base from './vite.config'

/**
 * Browser build of the renderer.
 *
 * The same React app that Electron loads from disk, built to be served by the
 * dashboard's FastAPI process at `/app/`. Nothing about the app changes —
 * `src/lib/web-bridge.ts` supplies `window.opencodonDesktop` over HTTP/WS
 * instead of IPC, and the renderer can't tell the difference.
 *
 *   npm run build:web   →   opencodon_cli/web_app_dist/
 *
 * `base` must be absolute (not the Electron build's './') because the app is
 * served from a path prefix while its own routes live on the hash, so relative
 * asset URLs would resolve against whatever route the user reloaded on.
 */
/** Dashboard to develop against: `OPENCODON_DASHBOARD_URL=... npm run dev:web`. */
const backend = process.env.OPENCODON_DASHBOARD_URL || 'http://127.0.0.1:9119'

/**
 * In production the FastAPI server injects the bootstrap globals into
 * index.html. The Vite dev server serves that file itself, so nothing injects
 * them and the app boots with no credential. Mirror the injection here from
 * the same env var the server reads for its token
 * (`OPENCODON_DASHBOARD_SESSION_TOKEN`), so one exported value in the shell
 * lets `dashboard` and `dev:web` agree:
 *
 *   export OPENCODON_DASHBOARD_SESSION_TOKEN=dev-token
 *   opencodon dashboard --no-open & npm run dev:web
 */
const injectBootstrap = () => ({
  name: 'opencodon-web-bootstrap',
  apply: 'serve' as const,
  transformIndexHtml: (html: string) => {
    const token = process.env.OPENCODON_DASHBOARD_SESSION_TOKEN || ''

    return html.replace(
      '</head>',
      `<script>window.__OPENCODON_SESSION_TOKEN__=${JSON.stringify(token)};` +
        `window.__OPENCODON_BASE_PATH__="";window.__OPENCODON_AUTH_REQUIRED__=false;</script></head>`
    )
  }
})

export default mergeConfig(
  base,
  defineConfig({
    base: '/app/',
    plugins: [injectBootstrap()],
    build: {
      outDir: path.resolve(__dirname, '../../opencodon_cli/web_app_dist'),
      emptyOutDir: true
    },
    server: {
      port: 5175,
      // The dev server has no backend of its own, so every API and socket
      // call is forwarded to a running dashboard. `ws: true` matters most:
      // the gateway, PTY, and shell are all WebSockets, and without it the
      // upgrade requests 404 and the app boots into a disconnected shell.
      proxy: {
        '/api': { target: backend, changeOrigin: true, ws: true }
      }
    }
  })
)
