import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { findGroupOfPane, group, split } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import type { SessionTile } from '@/store/session-states'
import {
  $sessionTiles,
  openSessionTabFocused,
  orderTilesByTree,
  selectionHomesToWorkspace,
  workspaceTabHides
} from '@/store/session-states'

const tile = (storedSessionId: string): SessionTile => ({ storedSessionId })
const tilePane = (id: string) => `session-tile:${id}`

describe('orderTilesByTree', () => {
  it('no-ops (null) without a tree or below two tiles', () => {
    expect(orderTilesByTree(null, [tile('a'), tile('b')])).toBeNull()
    expect(orderTilesByTree(group([tilePane('a')]), [tile('a')])).toBeNull()
  })

  it('reorders tiles to layout-tree encounter order across a split', () => {
    const tree = split('row', [group(['workspace', tilePane('b')]), group([tilePane('a')])])

    expect(orderTilesByTree(tree, [tile('a'), tile('b')])).toEqual([tile('b'), tile('a')])
  })

  it('returns null when the array already matches strip order (skip persist)', () => {
    const tree = split('row', [group([tilePane('b')]), group([tilePane('a')])])

    expect(orderTilesByTree(tree, [tile('b'), tile('a')])).toBeNull()
  })

  it('sorts not-yet-adopted tiles after placed ones, stably', () => {
    const tree = group(['workspace', tilePane('b')])

    expect(orderTilesByTree(tree, [tile('a'), tile('b'), tile('c')])).toEqual([tile('b'), tile('a'), tile('c')])
  })
})

describe('selectionHomesToWorkspace', () => {
  const tiles = [tile('a'), tile('b')]

  it('homes for a null selection or a non-tile session', () => {
    expect(selectionHomesToWorkspace(null, tiles)).toBe(true)
    expect(selectionHomesToWorkspace('c', tiles)).toBe(true)
  })

  it('skips homing when the selected id is already an open tile', () => {
    expect(selectionHomesToWorkspace('a', tiles)).toBe(false)
  })
})

// Plain click now opens a session as its own TAB rather than replacing the
// main pane. Two things have to hold for that to feel right, and both were
// wrong in the first cut: the tab must come to the FRONT (a background tab
// reads as a dead click), and clicking an already-open session must FOCUS it
// rather than stacking a second copy of the same conversation.
describe('openSessionTabFocused', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $sessionTiles.set([])
    $layoutTree.set(null)
  })

  afterEach(() => {
    $layoutTree.set(null)
    $sessionTiles.set([])
  })

  it('opens the session as a tile', () => {
    openSessionTabFocused('a')

    expect($sessionTiles.get().map(t => t.storedSessionId)).toEqual(['a'])
  })

  it('does not stack a second tile for a session already open', () => {
    openSessionTabFocused('a')
    openSessionTabFocused('a')

    expect($sessionTiles.get().filter(t => t.storedSessionId === 'a')).toHaveLength(1)
  })

  it('fronts the tab once the pane is adopted, not before', () => {
    // Adoption is async: at open time the pane is not in the tree yet, so a
    // reveal issued immediately resolves no group and silently does nothing.
    openSessionTabFocused('a')
    expect($layoutTree.get()).toBeNull()

    // The pane lands in a group whose active tab is something else...
    $layoutTree.set(group(['workspace', tilePane('a')], { active: 'workspace' }))

    // ...and the deferred front switches it to the new tab.
    expect(findGroupOfPane($layoutTree.get()!, tilePane('a'))?.active).toBe(tilePane('a'))
  })

  it('fronts immediately when the pane is already in the tree', () => {
    // Reopening a tile that survived in the layout (e.g. a persisted tab).
    $layoutTree.set(group(['workspace', tilePane('a')], { active: 'workspace' }))
    openSessionTabFocused('a')

    expect(findGroupOfPane($layoutTree.get()!, tilePane('a'))?.active).toBe(tilePane('a'))
  })
})

// The workspace tab hides itself while it holds an untouched draft and real
// session tabs are open. `isActive` is the condition that makes the rule safe
// AND makes it latch — both properties are load-bearing, so both are pinned.
describe('workspaceTabHides', () => {
  const state = (over: Partial<Parameters<typeof workspaceTabHides>[0]> = {}) => ({
    hasSessionTabs: true,
    holdsDraft: true,
    isActive: false,
    ...over
  })

  it('hides an idle draft while real session tabs are open', () => {
    expect(workspaceTabHides(state())).toBe(true)
  })

  it('never hides the tab being looked at', () => {
    expect(workspaceTabHides(state({ isActive: true }))).toBe(false)
  })

  it('never hides a loaded session — only a draft', () => {
    expect(workspaceTabHides(state({ holdsDraft: false }))).toBe(false)
  })

  it('never hides the last tab standing', () => {
    // With no session tabs the workspace is the only thing to show; hiding it
    // would leave the zone blank.
    expect(workspaceTabHides(state({ hasSessionTabs: false }))).toBe(false)
  })

  it('latches: a hidden tab stays hidden until something reveals it', () => {
    // A hidden pane can never become active on its own, so `isActive` stays
    // false and the rule keeps returning true. This is why New session must
    // call revealTreePane explicitly rather than nudging some atom.
    const hidden = state({ isActive: false })

    expect(workspaceTabHides(hidden)).toBe(true)
    expect(workspaceTabHides(hidden)).toBe(true)
  })
})
