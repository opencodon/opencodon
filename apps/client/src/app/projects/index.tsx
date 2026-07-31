/**
 * Projects — the landing surface.
 *
 * This is not a page inside the app; for as long as it is showing it *is* the
 * app. Everything else — the session list, the file tree, terminals, artifacts
 * — is scoped to one project, so a surface whose whole job is to choose that
 * project cannot render inside a frame that already assumes one. It replaces
 * the shell (see ContribController) rather than mounting in the workspace pane,
 * which is why it draws its own chrome here instead of reaching for
 * PageSearchShell.
 *
 * It stays deliberately unscoped: what exists, how much work is in each, when
 * it was last touched, and the recent sessions across all of them, so returning
 * after a week starts with "where was I" instead of a folder picker.
 *
 * The browser opens here (`mount({ home: 'projects' })` in `apps/web`); the
 * Electron shell still opens on chat and follows later.
 */

import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { Input } from '@/components/ui/input'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { Archive, Loader2, Plus, RefreshCw, Search, Settings } from '@/lib/icons'
import { normalize } from '@/lib/text'
import {
  $activeProjectId,
  $projects,
  $projectTree,
  $projectTreeLoading,
  enterProject,
  openProjectCreate,
  refreshProjects,
  refreshProjectTree
} from '@/store/projects'

import { sortProjectsForOverview } from '../chat/sidebar/projects/model'
import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { projectRoute, SETTINGS_ROUTE } from '../routes'
import { formatAge } from '../science/format'
import { TITLEBAR_HEIGHT } from '../shell/titlebar'

export function ProjectsLanding() {
  const { t } = useI18n()
  const p = t.projectsOverview
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [showArchived, setShowArchived] = useState(false)

  const tree = useStore($projectTree)
  const loading = useStore($projectTreeLoading)
  const activeProjectId = useStore($activeProjectId)
  const projectRows = useStore($projects)

  const refreshAll = () => void Promise.all([refreshProjects(), refreshProjectTree()])

  useRefreshHotkey(refreshAll)

  // This surface can be the first thing opened — it is the browser host's home,
  // and a deep link or ⌘K reaches it — so it loads its own data. `projects.list`
  // matters as much as the tree here: archived rows exist only on the list.
  useEffect(() => {
    if (tree.length === 0) {
      void refreshProjectTree()
    }

    if (projectRows.length === 0) {
      void refreshProjects()
    }
    // Mount-only: a project the user then deletes must not retrigger a fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const needle = normalize(search)

  const projects = useMemo(
    () => sortProjectsForOverview(tree, activeProjectId).filter(project => normalize(project.label).includes(needle)),
    [tree, activeProjectId, needle]
  )

  // Archived projects are excluded from the tree, so they come off the raw rows.
  const archived = useMemo(
    () => projectRows.filter(project => project.archived && normalize(project.name).includes(needle)),
    [projectRows, needle]
  )

  // Newest work first, across every project — the other half of "where was I".
  const recentSessions = useMemo(
    () =>
      tree
        .flatMap(project => (project.previewSessions ?? []).map(session => ({ project, session })))
        .filter(entry => normalize(entry.session.title ?? entry.session.id).includes(needle))
        .sort((a, b) => (b.session.last_active || 0) - (a.session.last_active || 0))
        .slice(0, 12),
    [tree, needle]
  )

  // Recents open INSIDE their project rather than at the bare `/:sessionId`
  // route, so the sidebar and file tree are already scoped when the chat paints
  // — no visible re-home a beat later.
  const openSessionInProject = (projectId: string, sessionId: string) =>
    navigate(projectRoute(projectId, sessionId))

  const empty = tree.length === 0 && archived.length === 0

  return (
    <div className="flex h-screen min-h-0 w-screen flex-col overflow-hidden bg-(--ui-bg-chrome) text-(--ui-text-primary)">
      {/* Nothing but a drag handle. The real TitlebarControls (traffic lights
          left, system tools right) are fixed at a higher layer and stay mounted
          across the shell swap — this band is the clearance they need, and
          keeping it empty is what stops the landing from growing a titlebar. */}
      <div
        aria-hidden="true"
        className="shrink-0 [-webkit-app-region:drag]"
        style={{ height: `${TITLEBAR_HEIGHT}px` }}
      />

      <div className="min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-6 pb-16 pt-10">
          <header className="flex flex-col gap-5">
            <div className="flex items-center justify-between gap-3">
              <h1 className="text-2xl">{p.title}</h1>
              <div className="flex items-center gap-1">
                <Tip label={p.refresh}>
                  <Button
                    aria-label={p.refresh}
                    disabled={loading}
                    onClick={refreshAll}
                    size="icon-titlebar"
                    variant="ghost"
                  >
                    {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                  </Button>
                </Tip>
                {/* Deliberately sparse. Choosing a project is the job; the
                    per-project surfaces live inside a project and the
                    profile-wide ones behind Settings. Keep new entries out of
                    here unless they belong to choosing, not to working. */}
                <Tip label={p.manageSettings}>
                  <Button
                    aria-label={p.manageSettings}
                    onClick={() => navigate(SETTINGS_ROUTE)}
                    size="icon-titlebar"
                    variant="ghost"
                  >
                    <Settings />
                  </Button>
                </Tip>
              </div>
            </div>

            {empty ? null : (
              <div className="flex items-center gap-2">
                <div className="relative min-w-0 flex-1">
                  <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-(--ui-text-quaternary)" />
                  <Input
                    aria-label={p.search}
                    className="pl-9"
                    onChange={event => setSearch(event.target.value)}
                    placeholder={p.search}
                    value={search}
                  />
                </div>
                <Button className="shrink-0 gap-2" onClick={() => openProjectCreate()}>
                  <Plus />
                  <span className="normal-case">{p.newProject}</span>
                </Button>
              </div>
            )}
          </header>

          {tree.length === 0 && loading ? (
            <PageLoader label={p.loading} />
          ) : empty ? (
            // Nothing is discovered for you, so a first run genuinely has
            // nothing — the empty state has to be the way out of it, not a
            // description of it.
            <div className="grid min-h-64 place-items-center text-center">
              <div className="flex flex-col items-center gap-3">
                <EmptyState className="min-h-0" description={p.emptyDesc} title={p.emptyTitle} />
                <Button className="gap-2" onClick={() => openProjectCreate()}>
                  <Plus />
                  <span className="normal-case">{p.newProject}</span>
                </Button>
              </div>
            </div>
          ) : (
            <>
              <section className="flex flex-col gap-2">
                <ul className="flex flex-col gap-2">
                  {projects.map(project => (
                    <li key={project.id}>
                      <button
                        className="flex w-full items-center justify-between gap-4 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) px-4 py-3.5 text-left hover:border-(--ui-stroke-secondary)"
                        onClick={() => enterProject(project.id)}
                        type="button"
                      >
                        <span className="flex min-w-0 flex-col gap-0.5">
                          <span className="flex min-w-0 items-center gap-2">
                            {project.color ? (
                              <span
                                aria-hidden
                                className="size-2 shrink-0 rounded-full"
                                style={{ backgroundColor: project.color }}
                              />
                            ) : null}
                            <span className="min-w-0 truncate text-sm">{project.label}</span>
                          </span>
                          {project.path ? (
                            <span className="min-w-0 truncate font-mono-ui text-xs text-(--ui-text-tertiary)">
                              {project.path}
                            </span>
                          ) : null}
                        </span>
                        <span className="flex shrink-0 items-baseline gap-3 text-xs text-(--ui-text-tertiary)">
                          <span>{p.sessions(project.sessionCount)}</span>
                          <span>{formatAge(project.lastActive ?? null)}</span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>

                {archived.length > 0 ? (
                  <div className="mt-1 flex flex-col gap-2">
                    <Button
                      className="gap-2 self-start"
                      onClick={() => setShowArchived(value => !value)}
                      size="sm"
                      variant="text"
                    >
                      <Archive />
                      <span className="normal-case">{p.archived(archived.length)}</span>
                    </Button>
                    {showArchived ? (
                      <ul className="flex flex-col gap-1">
                        {archived.map(project => (
                          <li key={project.id}>
                            <button
                              className="flex w-full items-center justify-between gap-4 rounded-md px-4 py-2 text-left text-(--ui-text-secondary) hover:bg-(--ui-chat-bubble-background)"
                              onClick={() => enterProject(project.id)}
                              type="button"
                            >
                              <span className="min-w-0 truncate text-sm">{project.name}</span>
                              {project.primary_path ? (
                                <span className="min-w-0 shrink truncate font-mono-ui text-xs text-(--ui-text-tertiary)">
                                  {project.primary_path}
                                </span>
                              ) : null}
                            </button>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}
              </section>

              {recentSessions.length > 0 ? (
                <section className="flex flex-col gap-2">
                  <h2 className="text-xs text-(--ui-text-tertiary)">{p.recent}</h2>
                  <ul className="flex flex-col gap-0.5">
                    {recentSessions.map(({ project, session }) => (
                      <li key={session.id}>
                        <button
                          className="flex w-full items-baseline justify-between gap-3 rounded-md px-3 py-2 text-left hover:bg-(--ui-chat-bubble-background)"
                          onClick={() => openSessionInProject(project.id, session.id)}
                          type="button"
                        >
                          <span className="min-w-0 truncate text-sm">{session.title || session.id}</span>
                          <span className="flex shrink-0 items-baseline gap-3 text-xs text-(--ui-text-tertiary)">
                            <span className="max-w-40 truncate">{project.label}</span>
                            <span>{formatAge(session.last_active || session.started_at)}</span>
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
