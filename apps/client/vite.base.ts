import fs from 'fs'
import path from 'path'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import type { UserConfig } from 'vite'

/**
 * Vite configuration shared by every host of this UI.
 *
 * `apps/desktop` and `apps/web` each own only what genuinely differs — the
 * base path, the output directory, the dev server — and inherit the rest from
 * here, so the two hosts can never drift into building the same source
 * differently.
 */

const clientRoot = __dirname

/** `hgui` symlinks a worktree's node_modules to the main checkout. Vite
 *  realpaths those before enforcing server.fs.allow, so codicon/font assets
 *  resolve outside the worktree root and 404. Whitelist the real locations. */
const real = (p: string): null | string => {
  try {
    return fs.realpathSync(p)
  } catch {
    return null
  }
}

export const fsAllow = [
  ...new Set(
    [
      path.resolve(clientRoot, '../..'),
      real(path.resolve(clientRoot, 'node_modules')),
      real(path.resolve(clientRoot, '../../node_modules'))
    ].filter((p): p is string => p !== null)
  )
]

/** Static assets (icons, ds-assets) belong to the UI, not to a host. */
export const publicDir = path.resolve(clientRoot, 'public')

export const clientBase: UserConfig = {
  plugins: [react(), tailwindcss()],
  css: {
    // Pin an explicit (empty) PostCSS config. Tailwind is handled entirely by
    // `@tailwindcss/vite`, so this needs no PostCSS plugins — and without
    // this, Vite's `postcss-load-config` walks UP the filesystem looking for a
    // stray `postcss.config.*` / `tailwind.config.*`. The desktop build runs
    // from inside the user's home tree, so an unrelated Tailwind v3 config
    // higher up gets picked up and reprocesses our v4 stylesheet, failing the
    // build. Pinning the config makes the build hermetic.
    postcss: { plugins: [] }
  },
  build: {
    // Shiki ships many dynamic chunks by default, and electron-builder can OOM
    // scanning thousands of files, so the desktop build collapses to one
    // chunk. The bundle is large by design (~28 MB); raise the warning ceiling
    // above it so the cosmetic nag stays quiet while still acting as a
    // regression alarm if it balloons well past today's size.
    chunkSizeWarningLimit: 25000,
    rolldownOptions: {
      output: {
        codeSplitting: false
      }
    }
  },
  resolve: {
    alias: {
      '@': path.resolve(clientRoot, 'src'),
      '@opencodon/client': path.resolve(clientRoot, 'src/root.tsx'),
      '@opencodon/plugin-sdk': path.resolve(clientRoot, 'src/sdk/index.ts'),
      '@opencodon/shared/billing': path.resolve(clientRoot, '../shared/src/billing-types.ts'),
      '@opencodon/shared': path.resolve(clientRoot, '../shared/src'),
      react: path.resolve(clientRoot, '../../node_modules/react'),
      'react-dom': path.resolve(clientRoot, '../../node_modules/react-dom'),
      'react/jsx-dev-runtime': path.resolve(clientRoot, '../../node_modules/react/jsx-dev-runtime.js'),
      'react/jsx-runtime': path.resolve(clientRoot, '../../node_modules/react/jsx-runtime.js')
    },
    dedupe: ['react', 'react-dom']
  }
}
