import { mount } from '@opencodon/client'

import { installWebBridge } from './web-bridge'

// Must run before the UI reads `window.opencodonDesktop`: in a browser there
// is no preload script, so this host supplies the bridge itself.
installWebBridge()

// The browser opens on the project picker: a dashboard tab has no "the app I
// left open" the way a desktop window does, and every scoped surface (sessions,
// files, terminal, artifacts) needs a project chosen to be meaningful.
mount({ home: 'projects' })
