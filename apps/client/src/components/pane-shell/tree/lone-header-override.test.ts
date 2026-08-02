import { describe, expect, it } from 'vitest'

import { clearLoneGroupHeaderOverrides, findGroupOfPane, group, insertAtGroup, split } from './model'

describe('insertAtGroup header pinning', () => {
  it('pins the header shown when a pane stacks onto an occupied zone', () => {
    const tree = group(['workspace'], { id: 'g1' })
    const next = insertAtGroup(tree, 'g1', 'logs', 'center')

    expect(findGroupOfPane(next!, 'logs')?.headerHidden).toBe(false)
  })

  it('leaves a pane that lands alone in its own zone on the auto-hide default', () => {
    // The sidebar's adoption gesture: dock to the LEFT of main, which carves a
    // fresh single-pane zone. Pinning here grew a permanent one-tab strip.
    const tree = group(['workspace'], { id: 'g1' })
    const next = insertAtGroup(tree, 'g1', 'sessions', 'left')

    expect(findGroupOfPane(next!, 'sessions')?.headerHidden).toBeUndefined()
  })
})

describe('clearLoneGroupHeaderOverrides', () => {
  it('drops a stale override persisted against a lone pane', () => {
    const tree = split('row', [
      group(['sessions'], { headerHidden: false }),
      group(['workspace'], { headerHidden: false })
    ])

    const healed = clearLoneGroupHeaderOverrides(tree)

    expect(findGroupOfPane(healed, 'sessions')?.headerHidden).toBeUndefined()
    expect(findGroupOfPane(healed, 'workspace')?.headerHidden).toBeUndefined()
  })

  it('keeps the override on a real stack, where the bar is the only handle', () => {
    const tree = group(['terminal', 'logs'], { headerHidden: false })

    expect(clearLoneGroupHeaderOverrides(tree)).toBe(tree)
  })
})
