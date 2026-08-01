import { atom } from 'nanostores'
import type { ReactNode } from 'react'

import { registry } from '@/contrib/registry'

export const SESSION_ROUTE_PREFIX = '/'
export const NEW_CHAT_ROUTE = '/'
// Project-scoped routes nest under the projects dashboard, so the root
// namespace stays the session namespace (see routeSessionId):
//
//   /projects                              the dashboard (PROJECTS_ROUTE)
//   /projects/:projectId                   a project's new-chat draft
//   /projects/:projectId/sessions/:id      a session inside it
//
// `:projectId` is the projects.db row id — every project is user-created, so
// there is no path-shaped or otherwise unstable id to encode around.
export const PROJECT_ROUTE_PREFIX = '/projects/'
export const PROJECT_SESSION_SEGMENT = 'sessions'
export const SETTINGS_ROUTE = '/settings'
export const COMMAND_CENTER_ROUTE = '/command-center'
export const SKILLS_ROUTE = '/skills'
export const MESSAGING_ROUTE = '/messaging'
export const ARTIFACTS_ROUTE = '/artifacts'
export const SCIENCE_ROUTE = '/science'
export const PROJECTS_ROUTE = '/projects'
export const CRON_ROUTE = '/cron'
export const PROFILES_ROUTE = '/profiles'
export const AGENTS_ROUTE = '/agents'
export const STARMAP_ROUTE = '/starmap'

// ── Home surface — where a cold start lands with nothing to restore ─────────
// Set once by the host at mount (see `mount()` in root.tsx). `chat` is the
// historical desktop home; `projects` opens on the project picker, which is
// where both hosts are headed.
export type HomeSurface = 'chat' | 'projects'

let homeSurface: HomeSurface = 'chat'

export function setHomeSurface(surface: HomeSurface): void {
  homeSurface = surface
}

export function homeRoute(): string {
  return homeSurface === 'projects' ? PROJECTS_ROUTE : NEW_CHAT_ROUTE
}

/** True when the host opens on the project picker rather than a chat draft. */
export function isProjectFirstHost(): boolean {
  return homeSurface === 'projects'
}

export type AppView =
  | 'agents'
  | 'artifacts'
  | 'chat'
  | 'command-center'
  | 'cron'
  // A contributed (plugin) full page at its own route — NOT chat. Without this
  // distinction contributed paths fell through appViewForPath's 'chat' default,
  // so the sidebar kept a session highlighted and the titlebar kept the
  // session-title dropdown while a plugin page was showing.
  | 'extension'
  | 'messaging'
  | 'profiles'
  | 'projects'
  | 'science'
  | 'settings'
  | 'skills'
  | 'starmap'

export type AppRouteId =
  | 'agents'
  | 'artifacts'
  | 'command-center'
  | 'cron'
  | 'messaging'
  | 'new'
  | 'profiles'
  | 'projects'
  | 'science'
  | 'settings'
  | 'skills'
  | 'starmap'

export interface AppRoute {
  id: AppRouteId
  path: string
  view: AppView
}

export const APP_ROUTES = [
  { id: 'new', path: NEW_CHAT_ROUTE, view: 'chat' },
  { id: 'settings', path: SETTINGS_ROUTE, view: 'settings' },
  { id: 'command-center', path: COMMAND_CENTER_ROUTE, view: 'command-center' },
  { id: 'skills', path: SKILLS_ROUTE, view: 'skills' },
  { id: 'messaging', path: MESSAGING_ROUTE, view: 'messaging' },
  { id: 'artifacts', path: ARTIFACTS_ROUTE, view: 'artifacts' },
  { id: 'science', path: SCIENCE_ROUTE, view: 'science' },
  { id: 'projects', path: PROJECTS_ROUTE, view: 'projects' },
  { id: 'cron', path: CRON_ROUTE, view: 'cron' },
  { id: 'profiles', path: PROFILES_ROUTE, view: 'profiles' },
  { id: 'agents', path: AGENTS_ROUTE, view: 'agents' },
  { id: 'starmap', path: STARMAP_ROUTE, view: 'starmap' }
] as const satisfies readonly AppRoute[]

const APP_VIEW_BY_PATH = new Map<string, AppView>(APP_ROUTES.map(route => [route.path, route.view]))
const RESERVED_PATHS: ReadonlySet<string> = new Set(APP_ROUTES.map(route => route.path))

// ── Contributed routes — the `routes` registry area ─────────────────────────
// A contribution mounts a FULL PAGE in the workspace pane at `data.path`
// (`render` on the contribution itself, like every other area). Contributed
// paths are reserved exactly like APP_ROUTES so the session-id parser never
// mistakes them for a session route. Navigate with `host.navigate(path)`.

export const ROUTES_AREA = 'routes'

/** Payload of a `routes` contribution's `data`. */
export interface RouteContribution {
  /** Absolute path, e.g. `/kanban`. One segment; no params. */
  path: string
}

export function contributedRoutes(): Array<{ key: string; path: string; title?: string; render: () => ReactNode }> {
  return registry
    .getArea(ROUTES_AREA)
    .map(c => ({
      key: `${c.source ?? 'core'}:${c.id}`,
      path: (c.data as RouteContribution | undefined)?.path ?? '',
      title: c.title,
      render: c.render!
    }))
    .filter(
      route =>
        Boolean(route.path.startsWith('/') && route.render) &&
        !RESERVED_PATHS.has(route.path) &&
        // The project namespace is the app's, not a contribution's — a plugin
        // page at `/p/x` would shadow a project route.
        !route.path.startsWith(PROJECT_ROUTE_PREFIX)
    )
}

function isContributedPath(pathname: string): boolean {
  return contributedRoutes().some(route => route.path === pathname)
}

// ── Contributed sidebar nav — the `sidebar.nav` registry area ────────────────
// A DATA contribution adds a row to the sidebar's top nav (below Artifacts).
// Pair with a ROUTES_AREA page: the row navigates to `path` and lights up
// while the app is there.

export const SIDEBAR_NAV_AREA = 'sidebar.nav'

/** Payload of a `sidebar.nav` data contribution. */
export interface SidebarNavContribution {
  /** Codicon name, e.g. `'project'`. */
  codicon: string
  label: string
  /** Route to navigate to (usually a contributed page's path). */
  path: string
}

// Views that render as a full-screen modal card (OverlayView) over the shell.
// While one is open the app's titlebar control clusters must hide so they don't
// bleed over the overlay (they sit at a higher z-index than the overlay card).
export const OVERLAY_VIEWS: ReadonlySet<AppView> = new Set([
  'agents',
  'command-center',
  'cron',
  'profiles',
  'settings',
  'starmap'
])

export function isOverlayView(view: AppView): boolean {
  return OVERLAY_VIEWS.has(view)
}

// ── Project routes ──────────────────────────────────────────────────────────
// See PROJECT_ROUTE_PREFIX above for the shape. The literal `sessions` segment
// is what keeps the two levels unambiguous: without it, `/projects/:a/:b` can't
// say whether `:b` is a session or a mis-typed sub-resource, and a project id
// that happened to contain a slash would silently parse as a session route.

export interface ProjectRouteParts {
  projectId: string
  /** Null on a project's home route (its new-chat draft). */
  sessionId: null | string
}

export function parseProjectRoute(pathname: string): null | ProjectRouteParts {
  if (!pathname.startsWith(PROJECT_ROUTE_PREFIX)) {
    return null
  }

  const [rawProjectId, segment, rawSessionId, ...extra] = pathname.slice(PROJECT_ROUTE_PREFIX.length).split('/')

  if (!rawProjectId) {
    return null
  }

  const projectId = decodeURIComponent(rawProjectId)

  if (segment === undefined) {
    return { projectId, sessionId: null }
  }

  // Anything past the project id must be exactly `sessions/:id`. A partial or
  // unknown tail is a bad URL, not a project home — say so rather than quietly
  // dropping the tail and landing the user somewhere they didn't ask for.
  if (segment !== PROJECT_SESSION_SEGMENT || !rawSessionId || extra.length > 0) {
    return null
  }

  return { projectId, sessionId: decodeURIComponent(rawSessionId) }
}

/** The project this path is scoped to, or null outside a project route. */
export function routeProjectId(pathname: string): null | string {
  return parseProjectRoute(pathname)?.projectId ?? null
}

/**
 * What a path SAYS about project scope — which is not the same question as
 * `routeProjectId`, and the difference is the whole point:
 *
 *   a project id  the route names that project
 *   null          the route means "no project" (the landing, a detached draft)
 *   undefined     the route says NOTHING about projects
 *
 * Capabilities, Artifacts, Provenance, Settings and every plugin page are the
 * third case. They are a change of SURFACE, not a change of project — you are
 * still working in the project you were working in, you are just looking at
 * something else for a moment. Collapsing them into `null` is what silently
 * dropped the user out of their project whenever they opened one, taking the
 * scoped session list and file tree with it.
 *
 * Callers must no-op on `undefined` rather than coercing it.
 */
export function routeProjectScope(pathname: string): null | string | undefined {
  const parts = parseProjectRoute(pathname)

  if (parts) {
    return parts.projectId
  }

  // The landing is the one surface that positively means "no project chosen".
  if (pathname === PROJECTS_ROUTE) {
    return null
  }

  // A detached draft, and a bare `/:sessionId` — the latter gets re-homed into
  // its owning project by adoptBareSessionRoute once the tree can say which.
  if (pathname === NEW_CHAT_ROUTE || routeSessionId(pathname)) {
    return null
  }

  return undefined
}

export function projectRoute(projectId: string, sessionId?: null | string): string {
  const base = `${PROJECT_ROUTE_PREFIX}${encodeURIComponent(projectId)}`

  return sessionId ? `${base}/${PROJECT_SESSION_SEGMENT}/${encodeURIComponent(sessionId)}` : base
}

export function isNewChatRoute(pathname: string): boolean {
  if (pathname === NEW_CHAT_ROUTE) {
    return true
  }

  const parts = parseProjectRoute(pathname)

  // A project's home route IS its new chat — the draft just starts in the
  // project's root instead of detached.
  return parts !== null && parts.sessionId === null
}

export function routeSessionId(pathname: string): string | null {
  const scoped = parseProjectRoute(pathname)

  if (scoped) {
    return scoped.sessionId
  }

  if (!pathname.startsWith(SESSION_ROUTE_PREFIX) || RESERVED_PATHS.has(pathname) || isContributedPath(pathname)) {
    return null
  }

  const id = pathname.slice(SESSION_ROUTE_PREFIX.length)

  return id && !id.includes('/') ? decodeURIComponent(id) : null
}

// The project the app is currently inside, mirrored here by the wiring
// controller. `sessionRoute` defaults to it so that every existing call site —
// the command palette, keybinds, notification clicks, tab close — keeps the
// user inside the project they are working in, without each one having to know
// about project scope. Module-level rather than an atom because route BUILDING
// is a pure string operation on the side of a navigation that already happened.
let currentProjectId: null | string = null

export function setRouteProjectId(projectId: null | string): void {
  currentProjectId = projectId
}

/** Route to a session — inside `projectId`'s namespace, defaulting to the
 *  project the app is currently in. Pass `null` for the bare, unscoped form. */
export function sessionRoute(sessionId: string, projectId: null | string | undefined = currentProjectId): string {
  if (projectId) {
    return projectRoute(projectId, sessionId)
  }

  return `${SESSION_ROUTE_PREFIX}${encodeURIComponent(sessionId)}`
}

/**
 * Where a NEW chat starts — the current project's home route, which is that
 * project's draft (see isNewChatRoute), or the detached `/` when no project is
 * in scope. The sibling of `sessionRoute` for the not-yet-a-session case:
 * without it, "New session" hard-navigated to `/` and dropped the user out of
 * the project they were working in.
 */
export function newChatRoute(projectId: null | string | undefined = currentProjectId): string {
  return projectId ? projectRoute(projectId) : NEW_CHAT_ROUTE
}

export function appViewForPath(pathname: string): AppView {
  if (isNewChatRoute(pathname) || routeSessionId(pathname)) {
    return 'chat'
  }

  if (isContributedPath(pathname)) {
    return 'extension'
  }

  return APP_VIEW_BY_PATH.get(pathname) ?? 'chat'
}

/** True while the workspace pane shows a FULL PAGE (skills/messaging/
 *  artifacts/plugin routes) instead of the chat. Published by the wiring
 *  (which owns the router location); the workspace pane contribution mirrors
 *  it as `headerVeto` so the zone tab bar stands down on pages. Overlays
 *  (settings/…) don't count — the chat stays beneath them. */
export const $workspaceIsPage = atom(false)

export function syncWorkspaceIsPage(pathname: string): void {
  const view = appViewForPath(pathname)
  const isPage = view !== 'chat' && !isOverlayView(view)

  if (isPage !== $workspaceIsPage.get()) {
    $workspaceIsPage.set(isPage)
  }
}

// ── Landing vs shell ────────────────────────────────────────────────────────
// The projects landing is not a page inside the workspace pane — it stands in
// FOR the whole shell. "No project is selected" is an app state, not a route
// within a project-scoped frame: rendering it in the workspace pane wrapped it
// in a sidebar listing every session in the profile, which is exactly the
// scoping the landing exists to establish.

/** True on the one route the landing owns. */
export function isLandingRoute(pathname: string): boolean {
  return pathname === PROJECTS_ROUTE
}

// The last non-overlay path. Overlays (settings/command-center/…) render OVER
// whatever surface is beneath, so they must not change which surface that is —
// without this, opening Settings from the landing paints the entire chat shell
// behind the overlay card, and closing it lands the user in a project they
// never picked.
let baseRoutePath: string = NEW_CHAT_ROUTE

/** True while the landing replaces the shell. Read by the app root. */
export const $landingOpen = atom(false)

export function syncLandingOpen(pathname: string): void {
  if (!isOverlayView(appViewForPath(pathname))) {
    baseRoutePath = pathname
  }

  const open = isLandingRoute(baseRoutePath)

  if (open !== $landingOpen.get()) {
    $landingOpen.set(open)
  }
}
