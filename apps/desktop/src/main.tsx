import { mount } from '@opencodon/client'

// The preload script has already installed `window.opencodonDesktop` by the
// time this runs, so the Electron host has nothing to set up — it just mounts
// the shared UI.
mount()
