import { mount } from '@opencodon/client'

import { installWebBridge } from './web-bridge'

// Must run before the UI reads `window.opencodonDesktop`: in a browser there
// is no preload script, so this host supplies the bridge itself.
installWebBridge()

mount()
