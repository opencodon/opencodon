/**
 * Provenance — the execution record as a browsable surface.
 *
 * A *run* (frame) is a root session plus its compression-chain descendants:
 * the unit of scientific work. An *artifact* is a file it produced, kept with
 * every version and a pointer back to the cell that wrote it.
 *
 * This is deliberately read-only. Nothing here submits code; the one action
 * that executes anything — reproduce — replays cells that were already
 * recorded and is gated to a loopback bind by the server.
 *
 * Sub-views live in query params rather than path segments: the router treats
 * a single path segment as a session id, so `/science/artifacts/x` would be
 * parsed as a session. `?tab=artifacts&artifact=<id>` keeps every object
 * addressable — which is the point, since a provenance link that can't be
 * shared isn't much of a citation.
 */

import { useQuery } from '@tanstack/react-query'
import type * as React from 'react'
import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { Loader2, RefreshCw } from '@/lib/icons'
import { normalize } from '@/lib/text'
import { cn } from '@/lib/utils'

import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { PageSearchShell } from '../page-search-shell'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

import { scienceApi } from './api'
import { ArtifactDetail } from './artifact-detail'
import { FrameDetail } from './frame-detail'
import { formatAge, RUN_HEALTH_LABEL_KEY, RUN_HEALTH_TONE, runHealth } from './format'

const SCIENCE_TABS = ['frames', 'artifacts'] as const

type ScienceTab = (typeof SCIENCE_TABS)[number]

export const FRAMES_QUERY_KEY = ['science-frames'] as const
export const ARTIFACTS_QUERY_KEY = ['science-artifacts'] as const

interface ScienceViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

export function ScienceView({ setStatusbarItemGroup: _statusbar, ...props }: ScienceViewProps) {
  const { t } = useI18n()
  const s = t.science
  const [params, setParams] = useSearchParams()
  const [search, setSearch] = useState('')

  const tab = (SCIENCE_TABS as readonly string[]).includes(params.get('tab') ?? '')
    ? (params.get('tab') as ScienceTab)
    : 'frames'
  const selectedFrame = params.get('frame')
  const selectedArtifact = params.get('artifact')

  const select = (key: 'artifact' | 'frame', id: null | string) => {
    const next = new URLSearchParams(params)

    next.set('tab', key === 'frame' ? 'frames' : 'artifacts')

    if (id) {
      next.set(key, id)
    } else {
      next.delete(key)
    }

    setParams(next, { replace: true })
  }

  const frames = useQuery({ queryKey: FRAMES_QUERY_KEY, queryFn: () => scienceApi.frames() })
  const artifacts = useQuery({ queryKey: ARTIFACTS_QUERY_KEY, queryFn: () => scienceApi.artifacts() })

  const active = tab === 'frames' ? frames : artifacts

  useRefreshHotkey(() => void active.refetch())

  const needle = normalize(search)

  const visibleFrames = useMemo(
    () => (frames.data?.frames ?? []).filter(frame => normalize(frame.title ?? frame.frame_id).includes(needle)),
    [frames.data, needle]
  )

  const visibleArtifacts = useMemo(
    () => (artifacts.data?.artifacts ?? []).filter(artifact => normalize(artifact.filename).includes(needle)),
    [artifacts.data, needle]
  )

  const body = () => {
    if (active.isError) {
      return <ErrorState title={s.failedLoad} />
    }

    if (!active.data) {
      return <PageLoader label={s.loading} />
    }

    if (tab === 'frames') {
      if (selectedFrame) {
        return <FrameDetail frameId={selectedFrame} onClose={() => select('frame', null)} onOpenArtifact={id => select('artifact', id)} />
      }

      if (visibleFrames.length === 0) {
        return <EmptyState description={s.noFramesDesc} title={s.noFramesTitle} />
      }

      return (
        <ul className="flex flex-col gap-2">
          {visibleFrames.map(frame => {
            const health = runHealth(frame.cell_count, frame.failed_cell_count)

            return (
              <li key={frame.frame_id}>
                <button
                  className="flex w-full flex-col gap-1.5 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) px-4 py-3 text-left hover:border-(--ui-stroke-secondary)"
                  onClick={() => select('frame', frame.frame_id)}
                  type="button"
                >
                  <span className="flex items-baseline justify-between gap-4">
                    <span className="min-w-0 truncate text-sm">{frame.title || frame.frame_id}</span>
                    <span className="shrink-0 text-xs text-(--ui-text-tertiary)">
                      {formatAge(frame.last_cell_at ?? frame.started_at)}
                    </span>
                  </span>
                  <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-(--ui-text-secondary)">
                    <span>
                      {frame.artifact_count} {s.artifacts}
                    </span>
                    <span>
                      {frame.cell_count} {s.cells}
                    </span>
                    <span className={cn('tabular-nums', RUN_HEALTH_TONE[health])}>{s[RUN_HEALTH_LABEL_KEY[health]]}</span>
                    {frame.languages.map(language => (
                      <span className="text-(--ui-text-tertiary)" key={language}>
                        {language}
                      </span>
                    ))}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )
    }

    if (selectedArtifact) {
      return <ArtifactDetail artifactId={selectedArtifact} onClose={() => select('artifact', null)} />
    }

    if (visibleArtifacts.length === 0) {
      return <EmptyState description={s.noArtifactsDesc} title={s.noArtifactsTitle} />
    }

    return (
      <ul className="flex flex-col gap-2">
        {visibleArtifacts.map(artifact => (
          <li key={artifact.artifact_id}>
            <button
              className="flex w-full items-baseline justify-between gap-4 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background) px-4 py-3 text-left hover:border-(--ui-stroke-secondary)"
              onClick={() => select('artifact', artifact.artifact_id)}
              type="button"
            >
              <span className="min-w-0 truncate text-sm">{artifact.filename}</span>
              <span className="flex shrink-0 items-baseline gap-3 text-xs text-(--ui-text-tertiary)">
                <span>
                  {s.version} {artifact.latest_version_number ?? 1}
                </span>
                <span>{formatAge(artifact.created_at)}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <PageSearchShell
      {...props}
      activeTab={tab}
      onSearchChange={setSearch}
      onTabChange={id => {
        const next = new URLSearchParams(params)

        next.set('tab', id)
        next.delete('frame')
        next.delete('artifact')
        setParams(next, { replace: true })
      }}
      searchPlaceholder={s.search}
      searchTrailingAction={
        <Tip label={s.refresh}>
          <Button
            aria-label={s.refresh}
            disabled={active.isFetching}
            onClick={() => void active.refetch()}
            size="icon-titlebar"
            variant="ghost"
          >
            {active.isFetching ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          </Button>
        </Tip>
      }
      searchValue={search}
      tabs={[
        { id: 'frames', label: s.tabFrames, meta: frames.data ? visibleFrames.length : null },
        { id: 'artifacts', label: s.tabArtifacts, meta: artifacts.data ? visibleArtifacts.length : null }
      ]}
    >
      <div className="h-full overflow-y-auto [scrollbar-gutter:stable]">{body()}</div>
    </PageSearchShell>
  )
}
