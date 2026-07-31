import { afterEach, describe, expect, it } from 'vitest'

import {
  appViewForPath,
  homeRoute,
  isNewChatRoute,
  isProjectFirstHost,
  NEW_CHAT_ROUTE,
  parseProjectRoute,
  projectRoute,
  PROJECTS_ROUTE,
  routeProjectId,
  routeSessionId,
  sessionRoute,
  setHomeSurface,
  setRouteProjectId
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
