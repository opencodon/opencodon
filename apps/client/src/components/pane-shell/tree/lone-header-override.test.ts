import { describe, expect, it } from 'vitest'

import {
  clearLoneGroupHeaderOverrides,
  findGroupOfPane,
  group,
  insertAtGroup,
  normalize,
  removePane,
  split
} from './model'

describe('insertAtGroup header pinning', () => {
  it('clears an explicit hide when a pane stacks onto an occupied zone', () => {
    // The bar is what makes the newcomer reachable, so an explicit hide gives
    // way — but the zone gets no override of its own (2+ tabs already show it).
    const tree = group(['workspace'], { headerHidden: true, id: 'g1' })
    const next = insertAtGroup(tree, 'g1', 'logs', 'center')

    expect(findGroupOfPane(next!, 'logs')?.headerHidden).toBeUndefined()
  })

  it('leaves a pane that lands alone in its own zone on the auto-hide default', () => {
    // The sidebar's adoption gesture: dock to the LEFT of main, which carves a
    // fresh single-pane zone. Pinning here grew a permanent one-tab strip.
    const tree = group(['workspace'], { id: 'g1' })
    const next = insertAtGroup(tree, 'g1', 'sessions', 'left')

    expect(findGroupOfPane(next!, 'sessions')?.headerHidden).toBeUndefined()
  })
})

describe('normalize', () => {
  it('drops a shown override once a stack closes back to one pane', () => {
    // The regression: open a session tab, close it, and the main zone kept a
    // permanent one-tab strip because the shown override outlived the stack.
    const stacked = group(['workspace', 'session-tile:abc'], { headerHidden: false, id: 'g1' })

    expect(findGroupOfPane(removePane(stacked, 'session-tile:abc')!, 'workspace')?.headerHidden).toBeUndefined()
  })

  it('keeps an override on a real stack', () => {
    const tree = group(['terminal', 'logs'], { headerHidden: false })

    expect(normalize(tree)).toBe(tree)
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
