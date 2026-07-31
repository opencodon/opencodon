/**
 * Projects — the landing surface.
 *
 * Everything else in the app is scoped to one project: the session list, the
 * file tree, terminals, artifacts. This is the surface where that choice is
 * made, and the only one that is deliberately unscoped — what exists, how much
 * work is in each, when it was last touched, and the recent sessions across all
 * of them, so returning after a week starts with "where was I" instead of a
 * folder picker.
 *
 * It also hosts the surfaces that are *not* per-project — settings, skills,
 * models, profiles, cron, agents — because they belong to the profile rather
 * than to any one project, and a project-scoped shell is the wrong place to
 * hang them.
 *
 * The browser opens here (`mount({ home: 'projects' })` in `apps/web`); the
 * Electron shell still opens on chat and follows later. Both render this same
 * component from the same route.
 */

import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { Archive, Loader2, Plus, RefreshCw, Settings } from '@/lib/icons'
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
import { PageSearchShell } from '../page-search-shell'
import { projectRoute, SETTINGS_ROUTE } from '../routes'
import { formatAge } from '../science/format'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

interface ProjectsViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

export function ProjectsView({ setStatusbarItemGroup: _statusbar, ...props }: ProjectsViewProps) {
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

  // This page can be the first surface opened — it is the browser host's home,
  // and a deep link or ⌘K reaches it — so it loads its own data. `projects.list`
  // matters as much as the tree here: slugs live on those rows, and a slug is
  // what a project route is keyed by.
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

  return (
    <PageSearchShell
      {...props}
      activeTab="projects"
      onSearchChange={setSearch}
      onTabChange={() => {}}
      searchPlaceholder={p.search}
      searchTrailingAction={
        // Deliberately sparse. The dashboard's job is "pick a project" — the
        // per-project surfaces live inside a project, and the profile-wide ones
        // behind Settings. Keep new entries out of here unless they genuinely
        // belong to choosing, not to working.
        <>
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
          <Tip label={p.refresh}>
            <Button aria-label={p.refresh} disabled={loading} onClick={refreshAll} size="icon-titlebar" variant="ghost">
              {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            </Button>
          </Tip>
        </>
      }
      searchValue={search}
      tabs={[{ id: 'projects', label: p.title, meta: tree.length || null }]}
    >
      <div className="h-full overflow-y-auto [scrollbar-gutter:stable]">
        {tree.length === 0 && loading ? (
          <PageLoader label={p.loading} />
        ) : tree.length === 0 && archived.length === 0 ? (
          // Nothing is discovered for you, so a first run genuinely has nothing
          // — the empty state has to be the way out of it, not a description of
          // it.
          <div className="grid min-h-48 place-items-center text-center">
            <div className="flex flex-col items-center gap-3">
              <EmptyState className="min-h-0" description={p.emptyDesc} title={p.emptyTitle} />
              <Button className="gap-2" onClick={() => openProjectCreate()} size="sm">
                <Plus />
                <span className="normal-case">{p.newProject}</span>
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-6 lg:flex-row">
            <section className="flex min-w-0 flex-1 flex-col gap-2">
              {/* "New project" lives here rather than in the shell's trailing
                  header cell: that cell is a fixed grid column sized for icon
                  buttons, and a labelled button overflows it into the tabs. */}
              <div className="flex items-center justify-between gap-2">
                <h2 className="text-xs text-(--ui-text-tertiary)">{p.title}</h2>
                <Button className="gap-2" onClick={() => openProjectCreate()} size="xs" variant="secondary">
                  <Plus />
                  <span className="normal-case">{p.newProject}</span>
                </Button>
              </div>
              <ul className="flex flex-col gap-2">
                {projects.map(project => (
                  <li key={project.id}>
                    <button
                      className="flex w-full items-center justify-between gap-4 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) px-4 py-3 text-left hover:border-(--ui-stroke-secondary)"
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
                <div className="mt-2 flex flex-col gap-2">
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

            <section className="flex min-w-0 flex-1 flex-col gap-2">
              <h2 className="text-xs text-(--ui-text-tertiary)">{p.recent}</h2>
              {recentSessions.length === 0 ? (
                <EmptyState description={p.noRecentDesc} title={p.noRecentTitle} />
              ) : (
                <ul className="flex flex-col gap-1">
                  {recentSessions.map(({ project, session }) => (
                    <li key={session.id}>
                      <button
                        className="flex w-full flex-col gap-0.5 rounded-md px-3 py-2 text-left hover:bg-(--ui-chat-bubble-background)"
                        onClick={() => openSessionInProject(project.id, session.id)}
                        type="button"
                      >
                        <span className="min-w-0 truncate text-sm">{session.title || session.id}</span>
                        <span className="flex items-baseline gap-2 text-xs text-(--ui-text-tertiary)">
                          <span className="min-w-0 truncate">{project.label}</span>
                          <span className="shrink-0">{formatAge(session.last_active || session.started_at)}</span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </div>
    </PageSearchShell>
  )
}
