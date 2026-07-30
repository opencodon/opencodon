/**
 * One run: what it produced, what it executed, and where it ran.
 *
 * Outputs lead. The cell trace is the evidence behind them, and the
 * environments are what a reproduction attempt would have to match — so they
 * are facts on the page, not controls.
 */

import { useQuery } from '@tanstack/react-query'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { LogView } from '@/components/ui/log-view'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { ChevronLeft, Download } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { frameExportUrl, scienceApi } from './api'
import { formatAge, formatBytes, languageLabel } from './format'

interface FrameDetailProps {
  frameId: string
  onClose: () => void
  onOpenArtifact: (artifactId: string) => void
}

export function FrameDetail({ frameId, onClose, onOpenArtifact }: FrameDetailProps) {
  const { t } = useI18n()
  const s = t.science

  const frame = useQuery({ queryKey: ['science-frame', frameId], queryFn: () => scienceApi.frame(frameId) })
  const cells = useQuery({ queryKey: ['science-frame-cells', frameId], queryFn: () => scienceApi.frameCells(frameId) })

  if (frame.isError) {
    return <ErrorState title={s.failedLoad} />
  }

  if (!frame.data) {
    return <PageLoader label={s.loading} />
  }

  const detail = frame.data

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-2">
        <Tip label={s.tabFrames}>
          <Button aria-label={s.tabFrames} onClick={onClose} size="icon-titlebar" variant="ghost">
            <ChevronLeft />
          </Button>
        </Tip>
        <h2 className="min-w-0 flex-1 truncate text-sm">{detail.title || detail.frame_id}</h2>
        <Button asChild size="sm" variant="ghost">
          <a href={frameExportUrl(frameId)}>
            <Download />
            {s.export}
          </a>
        </Button>
      </div>

      {detail.session_missing ? <p className="text-xs text-(--ui-text-tertiary)">{s.sessionMissing}</p> : null}

      <section className="flex flex-col gap-2">
        <h3 className="text-xs text-(--ui-text-tertiary)">{s.artifacts}</h3>
        {detail.artifacts.length === 0 ? (
          <EmptyState description={s.noArtifactsDesc} title={s.noArtifactsTitle} />
        ) : (
          <ul className="flex flex-col gap-1">
            {detail.artifacts.map(artifact => (
              <li key={artifact.artifact_id}>
                <button
                  className="flex w-full items-baseline justify-between gap-4 rounded-md px-2 py-1.5 text-left text-xs hover:bg-(--ui-chat-bubble-background)"
                  onClick={() => onOpenArtifact(artifact.artifact_id)}
                  type="button"
                >
                  <span className="min-w-0 truncate">{artifact.filename}</span>
                  <span className="shrink-0 text-(--ui-text-quaternary)">
                    {formatBytes(artifact.latest_size_bytes)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {detail.environments.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="text-xs text-(--ui-text-tertiary)">{s.environment}</h3>
          <ul className="flex flex-wrap gap-2 text-xs text-(--ui-text-secondary)">
            {detail.environments.map((environment, index) => (
              <li className="rounded-md border border-(--ui-stroke-tertiary) px-2 py-1" key={index}>
                {languageLabel(environment.language)}
                {environment.env_name ? ` · ${environment.env_name}` : ''}
                {` · ${environment.cell_count} ${s.cells}`}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="flex flex-col gap-2">
        <h3 className="text-xs text-(--ui-text-tertiary)">{s.trace}</h3>
        {!cells.data ? (
          <PageLoader label={s.loading} />
        ) : (
          <ol className="flex flex-col gap-2">
            {cells.data.cells.map(cell => (
              <li
                className="rounded-lg border border-(--ui-stroke-tertiary) px-3 py-2 text-xs"
                key={cell.cell_id}
              >
                <div className="flex items-baseline justify-between gap-4">
                  <span className="min-w-0 truncate">{cell.description || languageLabel(cell.language)}</span>
                  <span
                    className={cn(
                      'shrink-0',
                      cell.exit_status === 'ok' ? 'text-(--ui-text-quaternary)' : 'text-destructive'
                    )}
                  >
                    {cell.exit_status === 'ok' ? formatAge(cell.created_at) : s.failed}
                  </span>
                </div>
                {cell.stderr ? <LogView className="mt-2 max-h-32">{cell.stderr}</LogView> : null}
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  )
}
