/**
 * When a lone pane must keep its tab strip (name card + close).
 *
 * Default: a single pane isn't a "tab", so the header auto-hides. Exceptions
 * force it on so a closeable surface never becomes an unclosable dead zone:
 *  - session tiles (`session-tile:*`) — even before chrome registers
 *  - any closeable `placement: 'main'` contribution
 *  - a collapse tool panel dragged into its own zone
 *
 * The MAIN zone is exempt from all of them. It hosts the uncloseable workspace
 * alongside the session/page tiles, and the workspace tab hides itself the
 * moment a real session tab exists (syncWorkspaceTabVisibility) — so the
 * everyday one-session layout ended up with a permanent one-tab strip reading
 * "NEW SESSION" over an otherwise chrome-free window. A tile stranded there is
 * still closeable (its sidebar row, the row menu, ⌘W), and the strip comes
 * back the moment a second tab shows. A tile split into a zone of its OWN
 * keeps the forced header: there the tab really is the only handle.
 */

export interface LoneHeaderChrome {
  placement?: string
  uncloseable?: boolean
}

export function forceLoneHeaderForPanes(
  shown: readonly string[],
  /** Every pane IN the zone, including ones currently hidden — a hidden
   *  workspace still makes its zone the main zone. */
  zonePanes: readonly string[],
  chromeOf: (id: string) => LoneHeaderChrome,
  isCollapsePane: (id: string) => boolean
): boolean {
  if (zonePanes.some(id => chromeOf(id).uncloseable)) {
    return false
  }

  if (shown.some(id => id.startsWith('session-tile:'))) {
    return true
  }

  if (
    shown.some(id => {
      const chrome = chromeOf(id)

      return !chrome.uncloseable && chrome.placement === 'main'
    })
  ) {
    return true
  }

  return shown.length === 1 && isCollapsePane(shown[0])
}
