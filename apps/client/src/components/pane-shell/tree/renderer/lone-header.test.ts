import { describe, expect, it } from 'vitest'

import { forceLoneHeaderForPanes } from './lone-header'

describe('forceLoneHeaderForPanes', () => {
  const chrome =
    (placement?: string, uncloseable = false) =>
    () => ({ placement, uncloseable })

  const noCollapse = () => false

  it('forces a header for session-tile ids even without registered chrome', () => {
    expect(forceLoneHeaderForPanes(['session-tile:abc'], ['session-tile:abc'], () => ({}), noCollapse)).toBe(true)
  })

  it('forces a header for closeable placement:main panes', () => {
    expect(forceLoneHeaderForPanes(['some-page'], ['some-page'], chrome('main', false), noCollapse)).toBe(true)
  })

  it('forces a header for a lone collapse tool pane', () => {
    expect(
      forceLoneHeaderForPanes(
        ['terminal'],
        ['terminal'],
        () => ({}),
        id => id === 'terminal'
      )
    ).toBe(true)
  })

  it('leaves a lone uncloseable workspace headerless', () => {
    expect(forceLoneHeaderForPanes(['workspace'], ['workspace'], chrome('main', true), noCollapse)).toBe(false)
  })

  it('leaves a lone session tile in the MAIN zone headerless', () => {
    // The everyday layout: one session tab, workspace tab auto-hidden beside
    // it. Forcing here is what kept a permanent "NEW SESSION" strip on screen.
    const zonePanes = ['workspace', 'session-tile:abc']
    const chromeOf = (id: string) => (id === 'workspace' ? { placement: 'main', uncloseable: true } : {})

    expect(forceLoneHeaderForPanes(['session-tile:abc'], zonePanes, chromeOf, noCollapse)).toBe(false)
  })

  it('still forces a header for a tile split into a zone of its own', () => {
    expect(forceLoneHeaderForPanes(['session-tile:abc'], ['session-tile:abc'], () => ({}), noCollapse)).toBe(true)
  })
})
