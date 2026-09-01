import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $rightRailActiveTabId, PREVIEW_PANE_ID, RIGHT_RAIL_PREVIEW_TAB_ID } from './layout'
import { $paneOpen } from './panes'
import {
  $filePreviewTabs,
  $filePreviewTarget,
  $previewServerRestart,
  $previewServerRestartStatus,
  $previewTarget,
  $sessionPreviewRegistry,
  beginPreviewServerRestart,
  clearSessionPreviewRegistry,
  closeActiveRightRailTab,
  dismissPreviewTarget,
  getSessionPreviewRecord,
  type PreviewTarget,
  progressPreviewServerRestart,
  setCurrentSessionPreviewTarget,
  setPreviewRenderMode
} from './preview'
import { $activeSessionId, $selectedStoredSessionId } from './session'

function previewTarget(source: string): PreviewTarget {
  return {
    kind: 'file',
    label: source,
    path: source,
    previewKind: 'html',
    source,
    url: `file://${source}`
  }
}

function withRenderMode(target: PreviewTarget, renderMode: PreviewTarget['renderMode']): PreviewTarget {
  return { ...target, renderMode }
}

describe('preview store', () => {
  beforeEach(() => {
    $previewServerRestart.set(null)
    $activeSessionId.set('session-1')
    $selectedStoredSessionId.set(null)
    window.localStorage.clear()
    clearSessionPreviewRegistry()
  })

  afterEach(() => {
    $previewServerRestart.set(null)
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    clearSessionPreviewRegistry()
    window.localStorage.clear()
  })

  it('opens a tool-row click as a source file tab, not the live preview', () => {
    const target: PreviewTarget = {
      kind: 'file',
      label: 'thing.ts',
      language: 'typescript',
      path: '/work/src/thing.ts',
      previewKind: 'text',
      source: 'src/thing.ts',
      url: 'file:///work/src/thing.ts'
    }

    setCurrentSessionPreviewTarget(target, 'tool-row')

    expect($filePreviewTabs.get()).toHaveLength(1)
    expect($filePreviewTabs.get()[0]?.target.path).toBe('/work/src/thing.ts')
    // The live-preview tab is for runnable artifacts; a source peek is a file tab.
    expect($previewTarget.get()).toBeNull()
  })

  it('flips an html target between rendered and source in place', () => {
    const target = previewTarget('/work/demo.html')

    setCurrentSessionPreviewTarget(target, 'file-browser')

    const tabId = $filePreviewTabs.get()[0]?.id

    expect($filePreviewTabs.get()[0]?.target.renderMode).toBe('source')

    setPreviewRenderMode($filePreviewTabs.get()[0]!.target, 'preview')

    // Same tab, different renderer — never a second tab for the same file.
    expect($filePreviewTabs.get()).toHaveLength(1)
    expect($filePreviewTabs.get()[0]?.id).toBe(tabId)
    expect($filePreviewTabs.get()[0]?.target.renderMode).toBe('preview')

    setPreviewRenderMode($filePreviewTabs.get()[0]!.target, 'source')

    expect($filePreviewTabs.get()[0]?.target.renderMode).toBe('source')
  })

  it('keeps the render mode when the session preview record is restored', () => {
    const target = previewTarget('/work/demo.html')

    setCurrentSessionPreviewTarget(target, 'tool-result')
    setPreviewRenderMode($previewTarget.get()!, 'source')

    expect($previewTarget.get()?.renderMode).toBe('source')
    expect(getSessionPreviewRecord('session-1')?.normalized.renderMode).toBe('source')
  })

  it('does not notify status subscribers for restart progress text', () => {
    const statuses: string[] = []
    const unsubscribe = $previewServerRestartStatus.subscribe(status => statuses.push(status))

    beginPreviewServerRestart('task-1', 'http://localhost:5174')
    progressPreviewServerRestart('task-1', 'first line')
    progressPreviewServerRestart('task-1', 'second line')
    unsubscribe()

    expect(statuses).toEqual(['idle', 'running'])
  })

  it('persists registered previews and dismissal per session', () => {
    const target = previewTarget('/work/demo.html')

    setCurrentSessionPreviewTarget(target, 'tool-result')

    expect($previewTarget.get()).toEqual(withRenderMode(target, 'preview'))
    expect($paneOpen(PREVIEW_PANE_ID).get()).toBe(true)
    expect(getSessionPreviewRecord('session-1')?.normalized).toEqual(withRenderMode(target, 'preview'))
    expect(window.localStorage.getItem('opencodon.desktop.sessionPreviews.v1')).toContain('/work/demo.html')

    dismissPreviewTarget()

    expect($previewTarget.get()).toBeNull()
    expect($paneOpen(PREVIEW_PANE_ID).get()).toBe(false)
    expect(getSessionPreviewRecord('session-1')).toBeNull()
    expect($sessionPreviewRegistry.get()['session-1']?.[0]?.dismissedAt).toEqual(expect.any(Number))

    setCurrentSessionPreviewTarget(target, 'tool-result')

    expect(getSessionPreviewRecord('session-1')?.dismissedAt).toBeUndefined()
  })

  it('replaces the session preview instead of keeping a back stack', () => {
    const first = previewTarget('/work/first.html')
    const second = previewTarget('/work/second.html')

    setCurrentSessionPreviewTarget(first, 'tool-result')
    setCurrentSessionPreviewTarget(second, 'tool-result')

    expect($sessionPreviewRegistry.get()['session-1']).toHaveLength(1)
    expect(getSessionPreviewRecord('session-1')?.normalized).toEqual(withRenderMode(second, 'preview'))

    dismissPreviewTarget()

    expect($previewTarget.get()).toBeNull()
    expect(getSessionPreviewRecord('session-1')).toBeNull()
    expect($sessionPreviewRegistry.get()['session-1']?.map(record => record.normalized.url)).toEqual([
      'file:///work/second.html'
    ])
  })

  it('keeps file inspection separate from live preview', () => {
    const target = previewTarget('/work/demo.html')
    const preview = previewTarget('/work/live.html')

    setCurrentSessionPreviewTarget(preview, 'tool-result')

    setCurrentSessionPreviewTarget(target, 'manual')

    expect($filePreviewTarget.get()).toEqual(withRenderMode(target, 'source'))
    expect($previewTarget.get()).toEqual(withRenderMode(preview, 'preview'))
    expect(getSessionPreviewRecord('session-1')?.normalized).toEqual(withRenderMode(preview, 'preview'))

    closeActiveRightRailTab()

    expect($filePreviewTarget.get()).toBeNull()
    expect($previewTarget.get()).toEqual(withRenderMode(preview, 'preview'))
  })

  it('keeps file tabs when a live preview opens', () => {
    const file = previewTarget('/work/file.html')
    const live = previewTarget('/work/live.html')

    setCurrentSessionPreviewTarget(file, 'manual')
    setCurrentSessionPreviewTarget(live, 'tool-result')

    expect($filePreviewTabs.get().map(tab => tab.target)).toEqual([withRenderMode(file, 'source')])
    expect($filePreviewTarget.get()).toBeNull()
    expect($rightRailActiveTabId.get()).toBe(RIGHT_RAIL_PREVIEW_TAB_ID)
    expect($previewTarget.get()).toEqual(withRenderMode(live, 'preview'))
  })
})
