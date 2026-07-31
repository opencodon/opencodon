/**
 * Router navigation from outside React.
 *
 * The app runs on a HashRouter (see `root.tsx`), so the address bar's hash IS
 * the router's location — writing it navigates. Stores and other non-component
 * code use this instead of taking a `navigate` callback through five layers of
 * props; components should keep using react-router's `useNavigate`, which is
 * cheaper and participates in the render pass.
 */

const toHash = (path: string): string => (path.startsWith('#') ? path : `#${path}`)

export function navigateTo(path: string, options: { replace?: boolean } = {}): void {
  const hash = toHash(path)

  if (window.location.hash === hash) {
    return
  }

  if (options.replace) {
    // replaceState alone wouldn't notify the router — it doesn't fire
    // hashchange. `location.replace` does, and still leaves no history entry.
    window.location.replace(`${window.location.pathname}${window.location.search}${hash}`)

    return
  }

  window.location.hash = hash
}

/** The router path currently in the address bar (no leading `#`). */
export function currentRoutePath(): string {
  return window.location.hash.replace(/^#/, '') || '/'
}
