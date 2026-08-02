import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { OpencodonConnection } from '@/global'
import type { ProfileInfo } from '@/types/opencodon'

// Keep profile.ts's side-effecting imports inert: the gateway socket layer and
// the REST query client must not run for real in a unit test.
const ensureGatewayForProfile = vi.fn(async () => undefined)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()

vi.mock('@/store/gateway', () => ({ $gateway, ensureGatewayForProfile, openGatewayForProfile }))
vi.mock('@/opencodon', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph }))

const {
  $activeGatewayProfile,
  $profileScope,
  $profiles,
  $showAllProfiles,
  ensureGatewayProfile,
  prewarmProfileBackend,
  refreshProfiles,
  resolveProfileTarget,
  setActiveProfile,
  $activeProfile
} = await import('./profile')

const { $connection } = await import('./session')
const { invalidateProfileScopedQueries } = await import('@/lib/query-client')
const { getProfiles } = await import('@/opencodon')

const profile = (name: string, isDefault = false): ProfileInfo => ({
  has_env: false,
  is_default: isDefault,
  model: null,
  name,
  path: `/tmp/opencodon/${name}`,
  provider: null,
  skill_count: 0
})

const remoteConn = (over: Partial<OpencodonConnection> = {}): OpencodonConnection =>
  ({ baseUrl: 'https://opencodon-roy.tail.ts.net', mode: 'remote', profile: 'vps-remote', ...over }) as OpencodonConnection

const localConn = (over: Partial<OpencodonConnection> = {}): OpencodonConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as OpencodonConnection

const getConnection = vi.fn<(profile?: string | null) => Promise<OpencodonConnection>>()

beforeEach(() => {
  getConnection.mockReset()
  ensureGatewayForProfile.mockClear()
  openGatewayForProfile.mockClear()
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  $connection.set(localConn())
  $profiles.set([])
  vi.stubGlobal('window', { opencodonDesktop: { getConnection } })
  vi.mocked(invalidateProfileScopedQueries).mockClear()
  resetStarmapGraph.mockClear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  $connection.set(null)
})

describe('ensureGatewayProfile → $connection sync (#46651)', () => {
  it('refreshes $connection to the remote descriptor when activating a remote pool profile', async () => {
    // Regression: the primary window backend is local, so $connection.mode is
    // "local". Activating the remote profile must flip it to "remote" — without
    // this, image attach uses path-based image.attach against the remote
    // gateway ("image not found: C:\\…") instead of image.attach_bytes.
    getConnection.mockResolvedValue(remoteConn())

    await ensureGatewayProfile('vps-remote')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('vps-remote')
    expect(getConnection).toHaveBeenCalledWith('vps-remote')
    expect($connection.get()?.mode).toBe('remote')
    expect($connection.get()?.profile).toBe('vps-remote')
  })

  it('resyncs $connection back to local when returning to the default profile', async () => {
    $activeGatewayProfile.set('vps-remote')
    $connection.set(remoteConn())
    getConnection.mockResolvedValue(localConn())

    await ensureGatewayProfile('default')

    expect(getConnection).toHaveBeenCalledWith('default')
    expect($connection.get()?.mode).toBe('local')
  })

  it('leaves the prior connection intact when the descriptor fetch fails', async () => {
    getConnection.mockRejectedValue(new Error('backend unreachable'))

    await ensureGatewayProfile('vps-remote')

    // Best-effort: boot/reconnect resyncs later; we must not null it out here.
    expect($connection.get()?.mode).toBe('local')
  })

  it('does not churn $connection when the target is already the active profile', async () => {
    $activeGatewayProfile.set('vps-remote')
    $connection.set(remoteConn())

    await ensureGatewayProfile('vps-remote')

    expect(getConnection).not.toHaveBeenCalled()
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($connection.get()?.mode).toBe('remote')
  })
})

describe('profile-scoped cache invalidation', () => {
  it('drops the memory graph cache when the active gateway profile changes', () => {
    $activeGatewayProfile.set('coder')

    expect(invalidateProfileScopedQueries).toHaveBeenCalled()
    expect(resetStarmapGraph).toHaveBeenCalledTimes(1)
  })
})

describe('prewarmProfileBackend (hover-intent pool spawn)', () => {
  it('opens the gateway (spawn + connect, no activation) for a non-active profile', () => {
    prewarmProfileBackend('warm-basic')

    expect(openGatewayForProfile).toHaveBeenCalledWith('warm-basic')
    // Pre-warm must never activate — that's the click's job.
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('skips the profile the gateway is already on', () => {
    $activeGatewayProfile.set('warm-active')

    prewarmProfileBackend('warm-active')

    expect(openGatewayForProfile).not.toHaveBeenCalled()
  })

  it('throttles repeat pre-warms for the same profile within the interval', () => {
    prewarmProfileBackend('warm-throttle-a')
    prewarmProfileBackend('warm-throttle-a')
    prewarmProfileBackend('warm-throttle-b')

    const calls = openGatewayForProfile.mock.calls.map(([name]) => name)
    expect(calls.filter(name => name === 'warm-throttle-a')).toHaveLength(1)
    expect(calls.filter(name => name === 'warm-throttle-b')).toHaveLength(1)
  })

  it('swallows spawn failures — error UX belongs to the real switch', () => {
    openGatewayForProfile.mockRejectedValueOnce(new Error('spawn failed'))

    expect(() => prewarmProfileBackend('warm-failing')).not.toThrow()
  })
})

describe('refreshProfiles shared rail list (#49289)', () => {
  it('removes a deleted profile from the shared $profiles cache after Manage Profiles refreshes', async () => {
    $profiles.set([profile('default', true), profile('test1')])
    vi.mocked(getProfiles).mockResolvedValueOnce({ profiles: [profile('default', true)] })

    await refreshProfiles()

    expect($profiles.get().map(profile => profile.name)).toEqual(['default'])
  })

  it('leaves the shared $profiles cache intact when the refresh fails', async () => {
    $profiles.set([profile('default', true), profile('test1')])
    vi.mocked(getProfiles).mockRejectedValueOnce(new Error('backend unavailable'))

    await expect(refreshProfiles()).rejects.toThrow('backend unavailable')

    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'test1'])
  })
})

// PROFILES_UI_ENABLED is false in the shipped build, so these assert the state
// the app actually runs in. Flipping the flag on is what re-enables profiles;
// these cases are then expected to change with it.
describe('profiles-off: the client is pinned to default', () => {
  it('hides named profiles the backend reports, keeping only default', async () => {
    vi.mocked(getProfiles).mockResolvedValueOnce({
      profiles: [profile('default', true), profile('opencodon-open'), profile('opencodon-internal')]
    })

    await expect(refreshProfiles()).resolves.toEqual([expect.objectContaining({ name: 'default' })])
    expect($profiles.get().map(entry => entry.name)).toEqual(['default'])
  })

  it('still yields one entry when no row is flagged default', async () => {
    vi.mocked(getProfiles).mockResolvedValueOnce({ profiles: [profile('opencodon-open')] })

    // Zero profiles would read as "no backend"; the list must never be emptied
    // by the clamp itself.
    expect((await refreshProfiles()).map(entry => entry.name)).toEqual(['opencodon-open'])
  })

  it('scopes the session list to default even when the gateway sits on a named profile', () => {
    // The regression: `opencodon profile use opencodon-open` moved the backend's
    // current profile, the scope followed it, and the sidebar rendered an empty
    // list with the switcher hidden — no way back from inside the app.
    $activeGatewayProfile.set('opencodon-open')

    expect($profileScope.get()).toBe('default')
  })

  it('ignores a persisted all-profiles view, whose only off switch is hidden', () => {
    expect($showAllProfiles.get()).toBe(false)
  })

  it('reports default as the active profile whatever the backend says', () => {
    setActiveProfile('opencodon-restricted')

    expect($activeProfile.get()).toBe('default')
  })

  it('collapses any adoption candidate to default', () => {
    expect(resolveProfileTarget('opencodon-open')).toBe('default')
    expect(resolveProfileTarget(null)).toBe('default')
  })
})
