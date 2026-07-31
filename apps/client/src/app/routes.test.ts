import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  $landingOpen,
  appViewForPath,
  ARTIFACTS_ROUTE,
  COMMAND_CENTER_ROUTE,
  homeRoute,
  isLandingRoute,
  isNewChatRoute,
  isProjectFirstHost,
  NEW_CHAT_ROUTE,
  newChatRoute,
  parseProjectRoute,
  projectRoute,
  PROJECTS_ROUTE,
  routeProjectId,
  routeProjectScope,
  routeSessionId,
  SCIENCE_ROUTE,
  sessionRoute,
  setHomeSurface,
  setRouteProjectId,
  SETTINGS_ROUTE,
  SKILLS_ROUTE,
  syncLandingOpen
} from './routes'

// Sessions live at the ROOT of the route namespace, so every reserved prefix is
// a claim against a possible session id. These tests exist because a regression
// here doesn't fail loudly — it silently routes a session to the wrong surface.

afterEach(() => {
  setRouteProjectId(null)
  setHomeSurface('chat')
})

describe('parseProjectRoute', () => {
  it('reads a project home route', () => {
    expect(parseProjectRoute('/projects/p_123')).toEqual({ projectId: 'p_123', sessionId: null })
  })

  it('reads a session inside a project', () => {
    expect(parseProjectRoute('/projects/p_123/sessions/sess_1')).toEqual({
      projectId: 'p_123',
      sessionId: 'sess_1'
    })
  })

  it('is null outside the project namespace', () => {
    expect(parseProjectRoute('/')).toBeNull()
    expect(parseProjectRoute('/sess_1')).toBeNull()
    // The dashboard itself is not a project — one trailing slash apart, and the
    // mistake would scope the whole app to a project named "".
    expect(parseProjectRoute(PROJECTS_ROUTE)).toBeNull()
    expect(parseProjectRoute('/projects/')).toBeNull()
  })

  it('rejects an unknown or partial tail rather than guessing', () => {
    // Silently treating these as the project home would land the user somewhere
    // they didn't ask for, with no sign the URL was wrong.
    expect(parseProjectRoute('/projects/p_123/sessions')).toBeNull()
    expect(parseProjectRoute('/projects/p_123/frames/sess_1')).toBeNull()
    expect(parseProjectRoute('/projects/p_123/sessions/sess_1/extra')).toBeNull()
  })
})

describe('routeSessionId', () => {
  it('reads the bare session route', () => {
    expect(routeSessionId('/sess_1')).toBe('sess_1')
  })

  it('reads a project-scoped session', () => {
    expect(routeSessionId('/projects/p_123/sessions/sess_1')).toBe('sess_1')
  })

  it('is null on a project home — that is a draft, not a session', () => {
    expect(routeSessionId('/projects/p_123')).toBeNull()
  })

  it('never mistakes a reserved page for a session', () => {
    expect(routeSessionId(PROJECTS_ROUTE)).toBeNull()
    expect(routeSessionId('/settings')).toBeNull()
    expect(routeSessionId(NEW_CHAT_ROUTE)).toBeNull()
  })
})

describe('sessionRoute', () => {
  it('builds the bare form with no project in play', () => {
    expect(sessionRoute('sess_1')).toBe('/sess_1')
  })

  it('keeps the user inside the project the app is currently in', () => {
    setRouteProjectId('p_123')
    expect(sessionRoute('sess_1')).toBe('/projects/p_123/sessions/sess_1')
  })

  it('takes an explicit null to force the bare form', () => {
    setRouteProjectId('p_123')
    expect(sessionRoute('sess_1', null)).toBe('/sess_1')
  })

  it('round-trips an id needing encoding', () => {
    const id = 'sess/with space'

    expect(routeSessionId(sessionRoute(id))).toBe(id)
    setRouteProjectId('p_123')
    expect(routeSessionId(sessionRoute(id))).toBe(id)
  })
})

describe('projectRoute + routeProjectId', () => {
  it('round-trips a project id', () => {
    expect(routeProjectId(projectRoute('p_123'))).toBe('p_123')
    expect(routeProjectId(projectRoute('p_123', 'sess_1'))).toBe('p_123')
  })

  it('is null off a project route', () => {
    expect(routeProjectId('/sess_1')).toBeNull()
  })
})

describe('appViewForPath', () => {
  it('treats a project route as chat — the project is scope, not a view', () => {
    expect(appViewForPath('/projects/p_123')).toBe('chat')
    expect(appViewForPath('/projects/p_123/sessions/sess_1')).toBe('chat')
  })

  it('still resolves the dashboard', () => {
    expect(appViewForPath(PROJECTS_ROUTE)).toBe('projects')
  })
})

describe('isNewChatRoute', () => {
  it("counts a project's home as a new chat", () => {
    expect(isNewChatRoute(NEW_CHAT_ROUTE)).toBe(true)
    expect(isNewChatRoute('/projects/p_123')).toBe(true)
    expect(isNewChatRoute('/projects/p_123/sessions/sess_1')).toBe(false)
  })
})

describe('home surface', () => {
  it('defaults to chat', () => {
    expect(homeRoute()).toBe(NEW_CHAT_ROUTE)
    expect(isProjectFirstHost()).toBe(false)
  })

  it('lands on the picker for a project-first host', () => {
    setHomeSurface('projects')
    expect(homeRoute()).toBe(PROJECTS_ROUTE)
    expect(isProjectFirstHost()).toBe(true)
  })
})

// The landing REPLACES the shell rather than rendering inside it, so this flag
// decides which of the app's two halves is mounted. Getting it wrong is not a
// styling bug: a false negative wraps the project picker in a sidebar listing
// every session in the profile, and a false positive unmounts the user's chat.
describe('landing surface', () => {
  beforeEach(() => {
    // Re-establish a known base route — the module remembers the last
    // non-overlay path across calls, which is the whole point of it.
    syncLandingOpen(NEW_CHAT_ROUTE)
  })

  it('owns exactly one route', () => {
    expect(isLandingRoute(PROJECTS_ROUTE)).toBe(true)
    // A project is inside the shell, not the landing — including its home.
    expect(isLandingRoute('/projects/p_123')).toBe(false)
    expect(isLandingRoute('/projects/p_123/sessions/sess_1')).toBe(false)
    expect(isLandingRoute(NEW_CHAT_ROUTE)).toBe(false)
  })

  it('opens on the landing route and closes on entering a project', () => {
    syncLandingOpen(PROJECTS_ROUTE)
    expect($landingOpen.get()).toBe(true)

    syncLandingOpen('/projects/p_123')
    expect($landingOpen.get()).toBe(false)
  })

  it('stays open under an overlay opened FROM the landing', () => {
    syncLandingOpen(PROJECTS_ROUTE)
    // Settings renders over whatever surface is beneath. If the overlay
    // reset the base surface, the entire chat shell would paint behind the
    // settings card — and closing it would land the user in a project they
    // never picked.
    syncLandingOpen(SETTINGS_ROUTE)
    expect($landingOpen.get()).toBe(true)

    syncLandingOpen(COMMAND_CENTER_ROUTE)
    expect($landingOpen.get()).toBe(true)

    // Closing the overlay returns to the landing, still open.
    syncLandingOpen(PROJECTS_ROUTE)
    expect($landingOpen.get()).toBe(true)
  })

  it('stays closed under an overlay opened from inside a project', () => {
    syncLandingOpen('/projects/p_123/sessions/sess_1')
    syncLandingOpen(SETTINGS_ROUTE)
    expect($landingOpen.get()).toBe(false)
  })

  it('closes on a full page, which renders in the shell', () => {
    syncLandingOpen(PROJECTS_ROUTE)
    // Artifacts is a workspace-pane page, not an overlay: it belongs to the
    // shell, so reaching it leaves the landing.
    syncLandingOpen(ARTIFACTS_ROUTE)
    expect($landingOpen.get()).toBe(false)
  })
})

// The distinction this encodes is the one that broke: a route that says
// NOTHING about projects is not the same as one that says "no project".
// Collapsing the two ejected the user from their project — losing the scoped
// session list, file tree and new-session cwd — every time they opened
// Capabilities, Artifacts or Settings.
describe('routeProjectScope', () => {
  it('names the project on a project route', () => {
    expect(routeProjectScope('/projects/p_123')).toBe('p_123')
    expect(routeProjectScope('/projects/p_123/sessions/sess_1')).toBe('p_123')
  })

  it('says "no project" only where that is actually true', () => {
    // The landing is the surface for choosing one, so nothing is chosen.
    expect(routeProjectScope(PROJECTS_ROUTE)).toBeNull()
    // A detached draft, and a bare session route (re-homed by adoption).
    expect(routeProjectScope(NEW_CHAT_ROUTE)).toBeNull()
    expect(routeProjectScope('/sess_1')).toBeNull()
  })

  it('stays silent on pages and overlays — a surface change, not a project change', () => {
    expect(routeProjectScope(SKILLS_ROUTE)).toBeUndefined()
    expect(routeProjectScope(ARTIFACTS_ROUTE)).toBeUndefined()
    expect(routeProjectScope(SCIENCE_ROUTE)).toBeUndefined()
    expect(routeProjectScope(SETTINGS_ROUTE)).toBeUndefined()
    expect(routeProjectScope(COMMAND_CENTER_ROUTE)).toBeUndefined()
  })

  it('distinguishes silence from null, which is the whole point', () => {
    // A caller that coerces undefined to null reintroduces the bug, so assert
    // the two are not interchangeable rather than just checking falsiness.
    expect(routeProjectScope(SKILLS_ROUTE)).not.toBeNull()
    expect(routeProjectScope(PROJECTS_ROUTE)).not.toBeUndefined()
  })
})

describe('newChatRoute', () => {
  it('starts a draft inside the current project', () => {
    setRouteProjectId('p_123')
    expect(newChatRoute()).toBe('/projects/p_123')
    // And that route IS a new chat, so the chat view renders a draft there.
    expect(isNewChatRoute(newChatRoute())).toBe(true)
  })

  it('falls back to the detached root with no project in scope', () => {
    expect(newChatRoute()).toBe(NEW_CHAT_ROUTE)
  })

  it('takes an explicit null to force the detached form', () => {
    setRouteProjectId('p_123')
    expect(newChatRoute(null)).toBe(NEW_CHAT_ROUTE)
  })
})
