import { beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionCommands } from '../app/slash/commands/session.js'
import type { SessionUsageResponse } from '../gatewayTypes.js'

const usageCommand = sessionCommands.find(cmd => cmd.name === 'usage')!


const guarded =
  <T>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r)
    }
  }

/** Build a ctx whose rpc routes by method name to a supplied map of results. */
const buildCtx = (results: Record<string, unknown>) => {
  const sys = vi.fn()
  const panel = vi.fn()

  const rpc = vi.fn((method: string, _params: unknown) => Promise.resolve(results[method]))

  const ctx = {
    gateway: { rpc },
    guarded,
    guardedErr: vi.fn(),
    sid: 'sid-1',
    stale: () => false,
    transcript: { page: vi.fn(), panel, sys }
  }

  const run = async (arg: string) => {
    usageCommand.run(arg, ctx as any, 'usage')
    await rpc.mock.results[0]?.value
    await Promise.resolve()
    await Promise.resolve()
  }

  return { ctx, panel, run, sys }
}

const baseUsage = (overrides: Partial<SessionUsageResponse> = {}): SessionUsageResponse =>
  ({ calls: 0, input: 0, output: 0, total: 0, ...overrides }) as SessionUsageResponse

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

const balancePanel = (panel: ReturnType<typeof vi.fn>) => {
  const sections = panel.mock.calls.find(c => c[0] === 'Balance')?.[1] as { text?: string }[] | undefined

  return (sections ?? []).map(s => s.text ?? '').join('\n')
}

describe('/usage slash command', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows "no API calls yet" only when there are no calls', async () => {
    const empty = buildCtx({ 'session.usage': baseUsage({ calls: 0 }) })
    await empty.run('')
    expect(printed(empty.sys)).toContain('no API calls yet')

    const withCalls = buildCtx({ 'session.usage': baseUsage({ calls: 3, total: 120 }) })
    await withCalls.run('')
    expect(printed(withCalls.sys)).not.toContain('no API calls yet')
  })


})
