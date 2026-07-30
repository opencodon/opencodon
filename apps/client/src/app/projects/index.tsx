/**
 * Projects overview — every project and the work in it, at a glance.
 *
 * The sidebar already lists projects, but only as a scoping control: you can
 * pick one, not survey them. This is the survey — what exists, how much is in
 * it, when it was last touched, and the recent sessions in each, so returning
 * after a week starts with "where was I" instead of a folder picker.
 *
 * It is a destination rather than a replacement for the chat home. This UI is
 * shared with the Electron shell, whose stated information architecture makes
 * chat the launch surface, so changing where the app opens is a product
 * decision — not something a new page should take by side effect.
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
import { Loader2, RefreshCw } from '@/lib/icons'
import { normalize } from '@/lib/text'
import { $activeProjectId, $projectTree, $projectTreeLoading, enterProject, refreshProjectTree } from '@/store/projects'

import { sortProjectsForOverview } from '../chat/sidebar/projects/model'
import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { PageSearchShell } from '../page-search-shell'
import { sessionRoute } from '../routes'
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

  const tree = useStore($projectTree)
  const loading = useStore($projectTreeLoading)
  const activeProjectId = useStore($activeProjectId)

  useRefreshHotkey(() => void refreshProjectTree())

  // This page can be the first surface opened (deep link, ⌘K), before the
  // sidebar's project section has ever mounted to load the tree.
  useEffect(() => {
    if (tree.length === 0) {
      void refreshProjectTree()
    }
  }, [tree.length])

  const needle = normalize(search)

  const projects = useMemo(
    () => sortProjectsForOverview(tree, activeProjectId).filter(project => normalize(project.label).includes(needle)),
    [tree, activeProjectId, needle]
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

  const openProject = (id: string) => {
    enterProject(id)
    navigate('/')
  }

  return (
    <PageSearchShell
      {...props}
      activeTab="projects"
      onSearchChange={setSearch}
      onTabChange={() => {}}
      searchPlaceholder={p.search}
      searchTrailingAction={
        <Tip label={p.refresh}>
          <Button
            aria-label={p.refresh}
            disabled={loading}
            onClick={() => void refreshProjectTree()}
            size="icon-titlebar"
            variant="ghost"
          >
            {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          </Button>
        </Tip>
      }
      searchValue={search}
      tabs={[{ id: 'projects', label: p.title, meta: tree.length || null }]}
    >
      <div className="h-full overflow-y-auto [scrollbar-gutter:stable]">
        {tree.length === 0 && loading ? (
          <PageLoader label={p.loading} />
        ) : tree.length === 0 ? (
          <EmptyState description={p.emptyDesc} title={p.emptyTitle} />
        ) : (
          <div className="flex flex-col gap-6 lg:flex-row">
            <section className="flex min-w-0 flex-1 flex-col gap-2">
              <h2 className="text-xs text-(--ui-text-tertiary)">{p.title}</h2>
              <ul className="flex flex-col gap-2">
                {projects.map(project => (
                  <li key={project.id}>
                    <button
                      className="flex w-full items-baseline justify-between gap-4 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) px-4 py-3 text-left hover:border-(--ui-stroke-secondary)"
                      onClick={() => openProject(project.id)}
                      type="button"
                    >
                      <span className="min-w-0 truncate text-sm">{project.label}</span>
                      <span className="flex shrink-0 items-baseline gap-3 text-xs text-(--ui-text-tertiary)">
                        <span>{p.sessions(project.sessionCount)}</span>
                        <span>{formatAge(project.lastActive ?? null)}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
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
                        onClick={() => navigate(sessionRoute(session.id))}
                        type="button"
                      >
                        <span className="min-w-0 truncate text-sm">{session.title || session.id}</span>
                        <span className="flex items-baseline gap-2 text-xs text-(--ui-text-tertiary)">
                          <span className="min-w-0 truncate">{project.label}</span>
                          <span className="shrink-0">
                            {formatAge(session.last_active || session.started_at)}
                          </span>
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
