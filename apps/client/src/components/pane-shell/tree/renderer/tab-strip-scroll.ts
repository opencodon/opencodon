/**
 * Horizontal scrolling for a zone's tab strip.
 *
 * The strip has always been an `overflow-x-auto` track with its scrollbar
 * styled away, so it technically scrolled — invisibly, and only by trackpad.
 * Once every session opens as its own tab that stops being a corner case: the
 * strip overflows in normal use, and three things have to hold for it to feel
 * like a tab bar rather than a clipped row.
 *
 *   1. The ACTIVE tab is always brought into view. Clicking a session in the
 *      sidebar opens its tab at the far end of a full strip; without this the
 *      pane switches to a session whose tab the user cannot see.
 *   2. A vertical wheel scrolls the strip horizontally. A mouse (no horizontal
 *      axis) could otherwise never reach the overflow at all.
 *   3. The edges fade while there is more in that direction — the only signal
 *      that anything is off-screen, since the scrollbar is hidden by design.
 */

import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

/** Which ends have content scrolled past them. */
export interface TabStripOverflow {
  end: boolean
  start: boolean
}

// Sub-pixel slack: a track scrolled fully to one end can land a fraction short
// on fractional-DPI displays, which would otherwise leave a fade stuck on at a
// hard stop.
const EDGE_EPSILON_PX = 1

/** Mask that fades whichever edges have content beyond them. Applied inline —
 *  the fade is data-driven (both/start/end/neither), not a static class. */
export function tabStripFadeStyle({ end, start }: TabStripOverflow): string | undefined {
  if (!start && !end) {
    return undefined
  }

  const from = start ? 'transparent 0, black 1.25rem' : 'black 0'
  const to = end ? 'black calc(100% - 1.25rem), transparent 100%' : 'black 100%'

  return `linear-gradient(to right, ${from}, ${to})`
}

/**
 * @param activeId  the pane whose tab must stay in view
 * @param tabsKey   changes whenever the set of rendered tabs does. A
 *   ResizeObserver on the track sees the CONTAINER resize but not content
 *   growth — opening a background tab (⌘-click) widens `scrollWidth` without
 *   touching the border box, so the fades would go stale without this.
 */
export function useTabStripScroll(
  activeId: string,
  tabsKey: string
): {
  overflow: TabStripOverflow
  ref: RefObject<HTMLDivElement | null>
} {
  const ref = useRef<HTMLDivElement>(null)
  const [overflow, setOverflow] = useState<TabStripOverflow>({ end: false, start: false })

  const measure = useCallback(() => {
    const el = ref.current

    if (!el) {
      return
    }

    const max = el.scrollWidth - el.clientWidth

    setOverflow(prev => {
      const next = {
        end: max > EDGE_EPSILON_PX && el.scrollLeft < max - EDGE_EPSILON_PX,
        start: el.scrollLeft > EDGE_EPSILON_PX
      }

      // Preserve reference identity on a no-op so the strip doesn't re-render
      // on every scroll frame.
      return prev.end === next.end && prev.start === next.start ? prev : next
    })
  }, [])

  // Keep the active tab in view. Layout effect so the correction lands in the
  // same frame the tab becomes active — a post-paint scroll reads as a jump.
  // Manual scrollLeft rather than scrollIntoView: the latter walks ancestors
  // and can scroll the pane body (or the window) as a side effect.
  useLayoutEffect(() => {
    const el = ref.current
    const tab = el?.querySelector<HTMLElement>(`[data-tree-tab="${CSS.escape(activeId)}"]`)

    if (!el || !tab) {
      return
    }

    const left = tab.offsetLeft
    const right = left + tab.offsetWidth

    if (left < el.scrollLeft) {
      el.scrollLeft = left
    } else if (right > el.scrollLeft + el.clientWidth) {
      el.scrollLeft = right - el.clientWidth
    }

    measure()
  }, [activeId, tabsKey, measure])

  useEffect(() => {
    const el = ref.current

    if (!el) {
      return
    }

    // Non-passive so the page can't also scroll: a vertical wheel over the
    // strip means "move the tabs", never "move something behind them".
    const onWheel = (event: WheelEvent) => {
      // A real horizontal gesture (trackpad two-finger sideways, shift+wheel)
      // is already going the right way — leave it to the browser.
      if (event.deltaY === 0 || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        return
      }

      if (el.scrollWidth <= el.clientWidth) {
        return
      }

      event.preventDefault()
      el.scrollLeft += event.deltaY
    }

    const observer = new ResizeObserver(measure)

    el.addEventListener('wheel', onWheel, { passive: false })
    el.addEventListener('scroll', measure, { passive: true })
    observer.observe(el)
    measure()

    return () => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('scroll', measure)
      observer.disconnect()
    }
  }, [measure])

  return { overflow, ref }
}
