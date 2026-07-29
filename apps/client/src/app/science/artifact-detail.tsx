/**
 * One artifact: what it is, every version of it, and how each was produced.
 *
 * The version timeline is the spine. Selecting a version drives the preview,
 * the producing cell, and the lineage — because "which version are we talking
 * about" is the question every other answer depends on.
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { ChevronLeft, Download } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { type LineageDirection, scienceApi, versionDownloadUrl } from './api'
import { formatBytes, formatTimestamp, shortHash } from './format'
import { VersionPreview } from './version-preview'

interface ArtifactDetailProps {
  artifactId: string
  onClose: () => void
}

export function ArtifactDetail({ artifactId, onClose }: ArtifactDetailProps) {
  const { t } = useI18n()
  const s = t.science
  const [selectedVersion, setSelectedVersion] = useState<null | string>(null)
  const [direction, setDirection] = useState<LineageDirection>('upstream')

  const artifact = useQuery({
    queryKey: ['science-artifact', artifactId],
    queryFn: () => scienceApi.artifact(artifactId)
  })

  // Default to the newest version rather than the first: that is the one the
  // user means by "the file" unless they say otherwise.
  const versionId = selectedVersion ?? artifact.data?.latest_version_id ?? null

  const version = useQuery({
    queryKey: ['science-version', versionId],
    queryFn: () => scienceApi.version(versionId!),
    enabled: Boolean(versionId)
  })

  const lineage = useQuery({
    queryKey: ['science-lineage', versionId, direction],
    queryFn: () => scienceApi.lineage(versionId!, direction),
    enabled: Boolean(versionId)
  })

  if (artifact.isError) {
    return <ErrorState title={s.failedLoad} />
  }

  if (!artifact.data) {
    return <PageLoader label={s.loading} />
  }

  const detail = artifact.data

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center gap-2">
        <Tip label={s.tabArtifacts}>
          <Button aria-label={s.tabArtifacts} onClick={onClose} size="icon-titlebar" variant="ghost">
            <ChevronLeft />
          </Button>
        </Tip>
        <h2 className="min-w-0 flex-1 truncate text-sm">{detail.filename}</h2>
        {versionId ? (
          <Button asChild size="sm" variant="ghost">
            <a download={detail.filename} href={versionDownloadUrl(versionId)}>
              <Download />
              {s.download}
            </a>
          </Button>
        ) : null}
      </div>

      <section className="flex flex-col gap-2">
        <h3 className="text-xs text-(--ui-text-tertiary)">{s.versions}</h3>
        <ul className="flex flex-wrap gap-2">
          {detail.versions.map(entry => (
            <li key={entry.version_id}>
              <button
                className={cn(
                  'rounded-md border px-3 py-1.5 text-xs',
                  entry.version_id === versionId
                    ? 'border-(--ui-stroke-primary) text-(--ui-text-primary)'
                    : 'border-(--ui-stroke-tertiary) text-(--ui-text-secondary)'
                )}
                onClick={() => setSelectedVersion(entry.version_id)}
                type="button"
              >
                {s.version} {entry.version_number}
                <span className="ml-2 text-(--ui-text-quaternary)">{formatBytes(entry.size_bytes)}</span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      {versionId ? <VersionPreview versionId={versionId} /> : null}

      {version.data ? (
        <section className="grid gap-x-8 gap-y-2 text-xs sm:grid-cols-2">
          <Fact label={s.checksum} value={shortHash(version.data.sha256)} />
          <Fact label={s.size} value={formatBytes(version.data.size_bytes)} />
          <Fact label={s.created} value={formatTimestamp(version.data.created_at)} />
          {version.data.producing_cell ? (
            <Fact
              label={s.producedBy}
              value={version.data.producing_cell.description || version.data.producing_cell.cell_id}
            />
          ) : null}
          {version.data.producing_cell?.env_name ? (
            <Fact label={s.environment} value={version.data.producing_cell.env_name} />
          ) : null}
          {version.data.producing_cell?.kernel_location ? (
            <Fact label={s.ranIn} value={version.data.producing_cell.kernel_location} />
          ) : null}
        </section>
      ) : null}

      <section className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <h3 className="flex-1 text-xs text-(--ui-text-tertiary)">{s.lineage}</h3>
          {(['upstream', 'downstream'] as const).map(option => (
            <Button
              key={option}
              onClick={() => setDirection(option)}
              size="sm"
              variant={direction === option ? 'secondary' : 'ghost'}
            >
              {option === 'upstream' ? s.upstream : s.downstream}
            </Button>
          ))}
        </div>

        {lineage.data && lineage.data.lineage.length > 0 ? (
          <ul className="flex flex-col gap-1 text-xs">
            {lineage.data.lineage.map(entry => (
              <li
                key={entry.version_id}
                // Depth as indentation: a chain reads at a glance, which is
                // most of what lineage is in practice. A wide DAG deserves a
                // real graph and does not get one here.
                style={{ paddingLeft: `${Math.min(entry.depth, 6) * 14}px` }}
              >
                <button
                  className="text-(--ui-text-secondary) hover:text-(--ui-text-primary)"
                  onClick={() => setSelectedVersion(entry.version_id)}
                  type="button"
                >
                  {entry.filename ?? entry.version_id}
                  <span className="ml-2 text-(--ui-text-quaternary)">
                    {s.version} {entry.version_number}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState description={s.noLineage} title={s.lineage} />
        )}
      </section>
    </div>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-(--ui-stroke-tertiary) py-1.5">
      <span className="text-(--ui-text-tertiary)">{label}</span>
      <span className="min-w-0 truncate text-right text-(--ui-text-secondary)">{value}</span>
    </div>
  )
}
