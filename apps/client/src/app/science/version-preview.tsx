/**
 * Render one artifact version's content, dispatched on its content type.
 *
 * This deliberately delegates to the app's existing renderers rather than
 * introducing new ones — there is one markdown renderer and one image viewer
 * in this codebase and forking either is how they drift apart.
 */

import { useQuery } from '@tanstack/react-query'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { ZoomableImage } from '@/components/chat/zoomable-image'
import { PageLoader } from '@/components/page-loader'
import { EmptyState } from '@/components/ui/empty-state'
import { LogView } from '@/components/ui/log-view'
import { useI18n } from '@/i18n'

import { scienceApi, versionDownloadUrl } from './api'

const isImage = (contentType: null | string): boolean => Boolean(contentType?.startsWith('image/'))

const isMarkdown = (contentType: null | string): boolean =>
  contentType === 'text/markdown' || contentType === 'text/x-markdown'

export function VersionPreview({ versionId }: { versionId: string }) {
  const { t } = useI18n()
  const s = t.science

  const content = useQuery({
    queryKey: ['science-version-content', versionId],
    queryFn: () => scienceApi.content(versionId)
  })

  if (content.isError) {
    return <EmptyState description={s.failedLoad} title={s.tabArtifacts} />
  }

  if (!content.data) {
    return <PageLoader label={s.loading} />
  }

  const { binary, content_type: contentType, text, truncated } = content.data

  // Images never come back as inline text, so they are fetched by URL rather
  // than from the content payload.
  if (isImage(contentType)) {
    return (
      <ZoomableImage
        alt={versionId}
        className="max-h-96 max-w-full rounded-md object-contain"
        src={versionDownloadUrl(versionId)}
      />
    )
  }

  if (binary || text === null) {
    return <EmptyState description={s.binaryContent} title={contentType ?? ''} />
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="max-h-96 overflow-auto rounded-lg border border-(--ui-stroke-tertiary) p-3">
        {isMarkdown(contentType) ? <MarkdownTextContent isRunning={false} text={text} /> : <LogView>{text}</LogView>}
      </div>
      {truncated ? <p className="text-xs text-(--ui-text-tertiary)">{s.truncated}</p> : null}
    </div>
  )
}
