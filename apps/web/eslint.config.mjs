import globals from 'globals'

import shared from '../../eslint.config.shared.mjs'

export default [
  ...shared,
  {
    // The browser host: its bridge is written against DOM APIs (fetch,
    // WebSocket, localStorage), so it needs the browser globals the shared
    // config withholds from terminal-only workspaces.
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node
      }
    }
  }
]
