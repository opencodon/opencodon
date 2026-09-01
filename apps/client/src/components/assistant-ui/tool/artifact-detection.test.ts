import { artifactClass, isAutoSurfacedArtifact } from '@opencodon/shared'
import { describe, expect, it } from 'vitest'

import { buildToolView, isPreviewableTarget, type ToolPart } from './fallback-model'

function writeFilePart(path: string, result: Record<string, unknown> = {}): ToolPart {
  return {
    args: { path },
    result: { ok: true, ...result },
    toolCallId: 'call-1',
    toolName: 'write_file',
    type: 'tool-call'
  }
}

describe('artifactClass', () => {
  it('separates artifacts from source code', () => {
    expect(artifactClass('report.md')).toBe('doc')
    expect(artifactClass('/tmp/data.json')).toBe('data')
    expect(artifactClass('out/index.html')).toBe('web')
    expect(artifactClass('chart.svg')).toBe('image')
    expect(artifactClass('src/store/preview.ts')).toBe('code')
    expect(artifactClass('build.log')).toBe('other')
    expect(artifactClass('app.zip')).toBe('binary')
  })

  it('ignores query and hash tails', () => {
    expect(artifactClass('notes.md?v=2#top')).toBe('doc')
  })

  it('treats a dotfile as extensionless', () => {
    expect(artifactClass('.gitignore')).toBe('other')
  })
})

describe('isPreviewableTarget', () => {
  it('surfaces documents, data, images and html', () => {
    for (const target of ['notes.md', './out/report.json', '/tmp/plot.png', 'site/index.html']) {
      expect(isAutoSurfacedArtifact(target)).toBe(true)
      expect(isPreviewableTarget(target)).toBe(true)
    }
  })

  it('does not surface source files or binaries', () => {
    expect(isPreviewableTarget('src/app.tsx')).toBe(false)
    expect(isPreviewableTarget('dist/app.wasm')).toBe(false)
    expect(isPreviewableTarget('')).toBe(false)
  })

  it('surfaces only local dev urls', () => {
    expect(isPreviewableTarget('http://localhost:5173/')).toBe(true)
    expect(isPreviewableTarget('http://127.0.0.1:8080/report.html')).toBe(true)
    expect(isPreviewableTarget('https://example.com/index.html')).toBe(false)
  })
})

describe('buildToolView preview targets', () => {
  it('reports a relative artifact path written by an edit tool', () => {
    const view = buildToolView(writeFilePart('reports/summary.md'), '')

    expect(view.previewTarget).toBe('reports/summary.md')
    expect(isPreviewableTarget(view.previewTarget ?? '')).toBe(true)
  })

  it('reports source-file writes but leaves them unsurfaced', () => {
    const view = buildToolView(writeFilePart('src/lib/thing.ts'), '')

    expect(view.previewTarget).toBe('src/lib/thing.ts')
    expect(isPreviewableTarget(view.previewTarget ?? '')).toBe(false)
  })

  it('falls back to the first artifact path in an inline diff', () => {
    const view = buildToolView(
      { result: { inline_diff: '--- a/src/main.ts\n+++ b/docs/guide.md\n+hello' }, toolName: 'patch', type: 'tool-call' },
      ''
    )

    expect(view.previewTarget).toBe('docs/guide.md')
  })
})

describe('buildToolView open paths', () => {
  it('offers the written file, source code included', () => {
    expect(buildToolView(writeFilePart('src/lib/thing.ts'), '').openPath).toBe('src/lib/thing.ts')
    expect(buildToolView(writeFilePart('reports/summary.md'), '').openPath).toBe('reports/summary.md')
  })

  it('offers the file a read acted on without surfacing it as an artifact', () => {
    const view = buildToolView(
      { args: { path: 'docs/guide.md' }, result: { text: 'hi' }, toolName: 'read_file', type: 'tool-call' },
      ''
    )

    expect(view.openPath).toBe('docs/guide.md')
    // A read produced nothing; only writes surface artifact chips.
    expect(view.previewTarget).toBe('')
  })

  it('leaves openPath unset for tools with no file', () => {
    expect(
      buildToolView({ args: { command: 'ls' }, result: { stdout: '' }, toolName: 'terminal', type: 'tool-call' }, '')
        .openPath
    ).toBeUndefined()
  })
})
