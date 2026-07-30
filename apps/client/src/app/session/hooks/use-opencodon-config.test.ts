// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getOpencodonConfig } from '@/opencodon'
import { persistString } from '@/lib/storage'
import {
  $currentCwd,
  $currentFastMode,
  $currentReasoningEffort,
  markComposerSelectionManual,
  setCurrentCwd,
  setCurrentFastMode,
  setCurrentModelSource,
  setCurrentReasoningEffort
} from '@/store/session'

import { useOpencodonConfig } from './use-opencodon-config'

vi.mock('@/opencodon', () => ({
  getOpencodonConfig: vi.fn(),
  getOpencodonConfigDefaults: vi.fn().mockResolvedValue({})
}))

const WORKSPACE_CWD_KEY = 'opencodon.desktop.workspace-cwd'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

const mockConfig = (config: Record<string, unknown>) =>
  vi.mocked(getOpencodonConfig).mockResolvedValue(config as Awaited<ReturnType<typeof getOpencodonConfig>>)

describe('useOpencodonConfig refreshOpencodonConfig', () => {
  beforeEach(() => {
    // Reset atoms and localStorage between tests
    setCurrentCwd('')
    setCurrentFastMode(false)
    setCurrentModelSource('')
    setCurrentReasoningEffort('')
    persistString(WORKSPACE_CWD_KEY, null)
  })

  it('does not let terminal.cwd replace an inactive selected workspace', async () => {
    setCurrentCwd('/Users/example/repo/.worktrees/feature')

    mockConfig({ terminal: { cwd: '/Users/example/new-workspace' } })
    const { result } = renderHook(() => useOpencodonConfig({ activeSessionIdRef: { current: null } }))

    await act(async () => {
      await result.current.refreshOpencodonConfig()
    })

    expect($currentCwd.get()).toBe('/Users/example/repo/.worktrees/feature')
  })

  it('does not let terminal.cwd replace an active session workspace', async () => {
    setCurrentCwd('/Users/example/repo/.worktrees/attached')

    mockConfig({ terminal: { cwd: '/Users/example/new-workspace' } })
    const { result } = renderHook(() => useOpencodonConfig({ activeSessionIdRef: { current: 'session-1' } }))

    await act(async () => {
      await result.current.refreshOpencodonConfig()
    })

    expect($currentCwd.get()).toBe('/Users/example/repo/.worktrees/attached')
  })

  it('does not let a stale forced config refresh overwrite newer draft selector intent', async () => {
    const profileConfig = deferred<Awaited<ReturnType<typeof getOpencodonConfig>>>()
    vi.mocked(getOpencodonConfig).mockReturnValueOnce(profileConfig.promise)

    const { result } = renderHook(() => useOpencodonConfig({ activeSessionIdRef: { current: null } }))

    let pendingRefresh!: Promise<void>
    act(() => {
      pendingRefresh = result.current.refreshOpencodonConfig(true)
    })
    expect(getOpencodonConfig).toHaveBeenCalled()

    // The user turns Fast off and chooses a different effort while the profile
    // defaults are still loading. That newer picker intent owns the composer.
    markComposerSelectionManual()
    setCurrentReasoningEffort('high')
    setCurrentFastMode(false)
    profileConfig.resolve({
      agent: { reasoning_effort: 'low', service_tier: 'priority' }
    } as Awaited<ReturnType<typeof getOpencodonConfig>>)

    await act(async () => {
      await pendingRefresh
    })

    expect($currentReasoningEffort.get()).toBe('high')
    expect($currentFastMode.get()).toBe(false)
  })

  it('does not let an older profile config overwrite a newer profile', async () => {
    const profileB = deferred<Awaited<ReturnType<typeof getOpencodonConfig>>>()
    const profileC = deferred<Awaited<ReturnType<typeof getOpencodonConfig>>>()
    vi.mocked(getOpencodonConfig).mockReturnValueOnce(profileB.promise).mockReturnValueOnce(profileC.promise)

    const { result } = renderHook(() => useOpencodonConfig({ activeSessionIdRef: { current: null } }))

    let refreshB!: Promise<void>
    let refreshC!: Promise<void>
    act(() => {
      refreshB = result.current.refreshOpencodonConfig(true)
      refreshC = result.current.refreshOpencodonConfig(true)
    })

    profileC.resolve({ agent: { reasoning_effort: 'low', service_tier: 'normal' } })
    await act(async () => {
      await refreshC
    })
    profileB.resolve({ agent: { reasoning_effort: 'high', service_tier: 'priority' } })
    await act(async () => {
      await refreshB
    })

    expect($currentReasoningEffort.get()).toBe('low')
    expect($currentFastMode.get()).toBe(false)
  })
})
