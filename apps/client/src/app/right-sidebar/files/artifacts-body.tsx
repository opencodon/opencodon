/**
 * The durable plane, in the files pane: what this session actually staged.
 *
 * A flat list, not a tree — artifacts are identified by name and version, not
 * by where they happen to sit on disk. Selecting one opens it in Provenance,
 * which is where versions, checksums, and the producing cell live; duplicating
 * any of that here would be a second answer to the same question.
 */

import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'

import { EmptyState } from '@/components/ui/empty-state'
import { useI18n } from '@/i18n'

import { scienceApi } from '../../science/api'
import { formatAge, formatBytes } from '../../science/format'
import { SCIENCE_ROUTE } from '../../routes'

export function ArtifactsBody({ sessionId }: { sessionId: null | string }) {
  const { t } = useI18n()
  const r = t.rightSidebar
  const navigate = useNavigate()

  const { data, isError } = useQuery({
    queryKey: ['science-artifacts-pane', sessionId],
    // A frame is a root session plus its compression descendants, so the
    // session id is the right key: the artifacts of a compacted conversation
    // stay attached to it.
    queryFn: () => scienceApi.artifacts({ frameId: sessionId ?? undefined }),
    enabled: Boolean(sessionId)
  })

  if (!sessionId) {
    return <EmptyState description={r.artifactsNoSessionDesc} title={r.artifactsNoSessionTitle} />
  }

  if (isError) {
    return <EmptyState description={r.artifactsFailedDesc} title={r.artifactsFailedTitle} />
  }

  const artifacts = data?.artifacts ?? []

  if (artifacts.length === 0) {
    return <EmptyState description={r.artifactsEmptyDesc} title={r.artifactsEmptyTitle} />
  }

  return (
    <ul className="flex min-h-0 flex-1 flex-col overflow-y-auto py-1">
      {artifacts.map(artifact => (
        <li key={artifact.artifact_id}>
          <button
            className="flex w-full items-baseline justify-between gap-2 px-2.5 py-1 text-left text-[0.6875rem] hover:bg-(--ui-chat-bubble-background)"
            onClick={() => navigate(`${SCIENCE_ROUTE}?tab=artifacts&artifact=${encodeURIComponent(artifact.artifact_id)}`)}
            type="button"
          >
            <span className="min-w-0 truncate">{artifact.filename}</span>
            <span className="shrink-0 text-(--ui-text-quaternary)">
              {formatBytes(artifact.latest_size_bytes)} · {formatAge(artifact.created_at)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  )
}
