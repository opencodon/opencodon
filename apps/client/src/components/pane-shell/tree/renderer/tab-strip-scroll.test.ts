import { describe, expect, it } from 'vitest'

import { tabStripFadeStyle } from './tab-strip-scroll'

// The tab strip hides its scrollbar by design, so this fade is the ONLY signal
// that tabs continue past an edge. A wrong mask is not cosmetic: it either
// claims there is more when there isn't, or silently clips the strip when the
// user has everything in view.
describe('tabStripFadeStyle', () => {
  it('is absent when nothing overflows — no mask, no clipping', () => {
    expect(tabStripFadeStyle({ end: false, start: false })).toBeUndefined()
  })

  it('fades only the end when scrolled to the start', () => {
    const mask = tabStripFadeStyle({ end: true, start: false })

    expect(mask).toContain('black 0')
    expect(mask).toContain('transparent 100%')
  })

  it('fades only the start when scrolled to the end', () => {
    const mask = tabStripFadeStyle({ end: false, start: true })

    expect(mask).toContain('transparent 0')
    expect(mask).toContain('black 100%')
  })

  it('fades both edges mid-scroll', () => {
    const mask = tabStripFadeStyle({ end: true, start: true })

    expect(mask).toContain('transparent 0')
    expect(mask).toContain('transparent 100%')
  })
})
