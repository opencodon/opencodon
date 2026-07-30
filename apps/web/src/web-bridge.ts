/**
 * Browser bridge — `window.opencodonDesktop` without Electron.
 *
 * The renderer talks to its host through one object. Under Electron that
 * object is a preload script bridging to the main process over IPC. In a
 * browser there is no main process, but there IS a host with the same
 * authority: the dashboard's own FastAPI server, which runs on the user's
 * machine and already exposes the same operations over REST
 * (`/api/fs/*`, `/api/git/*`, `/api/files*`). So the browser bridge is
 * mostly a transport swap — IPC becomes HTTP to the same host.
 *
 * Three kinds of member, and the distinction matters when adding one:
 *
 *   1. **Backed** — a real REST/WS endpoint does the work. File reads, git,
 *      the gateway socket, terminals.
 *   2. **Emulated** — the browser has its own primitive. Clipboard, external
 *      links, notifications, downloads, extra windows.
 *   3. **Absent** — the capability is genuinely Electron-only (OS trash,
 *      Finder reveal, in-place app updates, native uninstall). These reject
 *      or return an inert value; they never pretend to have worked, because
 *      a silent success here reads to the UI as "the file was trashed."
 *
 * The real preload always wins: `installWebBridge` is a no-op when
 * `window.opencodonDesktop` already exists.
 */

import type {
  DesktopBootProgress,
  DesktopBootstrapState,
  DesktopVersionInfo,
  OpencodonApiRequest,
  OpencodonConnection,
  OpencodonNotification,
  OpencodonReadDirResult,
  OpencodonReadFileTextResult,
  OpencodonTerminalExit,
  OpencodonTerminalSession
} from '@/global'

type Unsubscribe = () => void

/** Server-injected bootstrap globals (see `_serve_index` in web_server.py). */
declare global {
  interface Window {
    __OPENCODON_SESSION_TOKEN__?: string
    __OPENCODON_BASE_PATH__?: string
    __OPENCODON_AUTH_REQUIRED__?: boolean
  }
}

const basePath = (): string => (window.__OPENCODON_BASE_PATH__ || '').replace(/\/$/, '')
const sessionToken = (): string => window.__OPENCODON_SESSION_TOKEN__ || ''
const oauthGated = (): boolean => window.__OPENCODON_AUTH_REQUIRED__ === true

const httpBase = (): string => `${window.location.origin}${basePath()}`

const wsBase = (): string =>
  `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}${basePath()}`

/**
 * Authenticated fetch against the dashboard.
 *
 * Two auth schemes, mirroring the server: a bearer token injected into the
 * page on a loopback bind, or a cookie session under the OAuth gate. Under
 * the gate there is no token to send, so the cookie must ride along —
 * `credentials: 'include'` is what makes cross-port dev setups work.
 */
async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  const token = sessionToken()

  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  return fetch(`${httpBase()}${path}`, { ...init, headers, credentials: 'include' })
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authedFetch(path, init)

  if (!response.ok) {
    // Surface the server's own message: FastAPI puts it in `detail`, and the
    // UI's error states are far more useful with "File too large" than with
    // a bare status code.
    let detail = `${response.status} ${response.statusText}`

    try {
      const payload = (await response.json()) as { detail?: string; error?: string }
      detail = payload.detail || payload.error || detail
    } catch {
      // Non-JSON error body — keep the status line.
    }

    throw new Error(detail)
  }

  return (await response.json()) as T
}

const qs = (params: Record<string, null | string | undefined>): string => {
  const search = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value != null && value !== '') {
      search.set(key, value)
    }
  }

  const encoded = search.toString()

  return encoded ? `?${encoded}` : ''
}

/** A subscription the browser can't source. Returns a valid unsubscribe so
 *  effect cleanups stay symmetric. */
const noSubscription = (): Unsubscribe => () => {}

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

/**
 * Mint a gateway WebSocket URL.
 *
 * Under the OAuth gate the socket can't carry a bearer header (browsers don't
 * let you set headers on an upgrade), and the long-lived session token is
 * deliberately not honoured there — so the server issues single-use tickets.
 * Mint immediately before dialing; a ticket held across a reconnect is spent.
 */
async function gatewayWsUrl(profile?: null | string): Promise<{ ok: true; wsUrl: string } | { ok: false; error: string; needsOauthLogin?: boolean }> {
  const scope = qs({ profile: profile ?? null })

  if (!oauthGated()) {
    const auth = qs({ token: sessionToken(), profile: profile ?? null })

    return { ok: true, wsUrl: `${wsBase()}/api/ws${auth}` }
  }

  try {
    const ticket = await json<{ ticket: string }>(`/api/auth/ws-ticket${scope}`, { method: 'POST' })
    const auth = qs({ ticket: ticket.ticket, profile: profile ?? null })

    return { ok: true, wsUrl: `${wsBase()}/api/ws${auth}` }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : 'Could not authorize the gateway socket.',
      needsOauthLogin: true
    }
  }
}

async function getConnection(profile?: null | string): Promise<OpencodonConnection> {
  const minted = await gatewayWsUrl(profile)

  return {
    baseUrl: httpBase(),
    // Browser chrome owns the window; the renderer must not draw its own
    // traffic lights or reserve inset for them.
    isFullscreen: false,
    // 'remote' is the honest answer, and it is load-bearing. The renderer
    // treats mode as "whose filesystem is this?" — and in a browser the
    // answer is the server's, not the renderer's. Declaring 'remote' routes
    // every file, git, media, and project-tree call through the dashboard's
    // REST API via the existing `desktop-fs` / `desktop-git` facades, which
    // is exactly right and saves reimplementing them here. It also correctly
    // disables in-place app updates, which a browser cannot perform.
    mode: 'remote',
    remoteKind: 'url',
    remoteHost: window.location.host,
    authMode: oauthGated() ? 'oauth' : 'token',
    nativeOverlayWidth: 0,
    source: 'local',
    token: sessionToken(),
    wsUrl: minted.ok ? minted.wsUrl : '',
    logs: [],
    profile: profile ?? undefined,
    windowButtonPosition: null
  }
}

// ---------------------------------------------------------------------------
// REST passthrough
// ---------------------------------------------------------------------------

async function api<T>(request: OpencodonApiRequest): Promise<T> {
  const { path, method = 'GET', body, upload, timeoutMs, profile } = request
  const scoped = profile ? `${path}${path.includes('?') ? '&' : '?'}profile=${encodeURIComponent(profile)}` : path

  const controller = new AbortController()
  const timer = timeoutMs ? window.setTimeout(() => controller.abort(), timeoutMs) : null

  try {
    if (upload) {
      const form = new FormData()

      form.append('file', new Blob([upload.bytes], { type: upload.contentType || 'application/octet-stream' }), upload.filename)

      return await json<T>(scoped, { method: method === 'GET' ? 'POST' : method, body: form, signal: controller.signal })
    }

    return await json<T>(scoped, {
      method,
      signal: controller.signal,
      ...(body === undefined
        ? {}
        : { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } })
    })
  } finally {
    if (timer) {
      window.clearTimeout(timer)
    }
  }
}

// ---------------------------------------------------------------------------
// Filesystem — the server's `/api/fs/*` contract is field-for-field identical
// to the Electron IPC one, so these are near pass-throughs.
// ---------------------------------------------------------------------------

const readDir = (path: string): Promise<OpencodonReadDirResult> =>
  json<OpencodonReadDirResult>(`/api/fs/list${qs({ path })}`).catch(error => ({
    entries: [],
    error: error instanceof Error ? error.message : 'read-error'
  }))

const readFileText = (path: string): Promise<OpencodonReadFileTextResult> =>
  json<OpencodonReadFileTextResult>(`/api/fs/read-text${qs({ path })}`)

const readFileDataUrl = async (filePath: string): Promise<string> => {
  const { dataUrl } = await json<{ dataUrl: string }>(`/api/fs/read-data-url${qs({ path: filePath })}`)

  return dataUrl
}

const writeTextFile = (path: string, content: string): Promise<{ path: string }> =>
  json<{ path: string }>('/api/fs/write-text', {
    method: 'POST',
    body: JSON.stringify({ path, content }),
    headers: { 'Content-Type': 'application/json' }
  })

const gitRoot = async (path: string): Promise<null | string> => {
  const result = await json<{ root: null | string }>(`/api/fs/git-root${qs({ path })}`).catch(() => null)

  return result?.root ?? null
}

const sanitizeWorkspaceCwd = async (cwd?: null | string): Promise<{ cwd: string; sanitized: boolean }> => {
  const fallback = await json<{ cwd: string }>('/api/fs/default-cwd').catch(() => ({ cwd: '' }))

  if (!cwd) {
    return { cwd: fallback.cwd, sanitized: true }
  }

  const listing = await readDir(cwd)

  return listing.error ? { cwd: fallback.cwd, sanitized: true } : { cwd, sanitized: false }
}

// Git is deliberately absent. `desktopGit()` in `lib/desktop-git.ts` already
// swaps in a REST implementation whenever the connection reports 'remote' —
// which the browser always does — so a bridge-side `git` here would be dead
// code that could silently drift from the one the app actually calls. `git`
// is optional in the bridge contract for exactly this reason.

// ---------------------------------------------------------------------------
// Terminal — one WebSocket per terminal against `/api/shell`.
// ---------------------------------------------------------------------------

interface TerminalHandle {
  socket: WebSocket
  cwd: string
  onData: Set<(payload: string) => void>
  onExit: Set<(payload: OpencodonTerminalExit) => void>
}

const terminals = new Map<string, TerminalHandle>()
let terminalSeq = 0

const terminal = {
  start: async (options?: { cols?: number; cwd?: string; rows?: number }): Promise<OpencodonTerminalSession> => {
    const id = `web-term-${++terminalSeq}`
    const auth = oauthGated() ? (await json<{ ticket: string }>('/api/auth/ws-ticket', { method: 'POST' })).ticket : null
    const socket = new WebSocket(
      `${wsBase()}/api/shell${qs({
        token: auth ? null : sessionToken(),
        ticket: auth,
        cwd: options?.cwd ?? null,
        cols: options?.cols ? String(options.cols) : null,
        rows: options?.rows ? String(options.rows) : null
      })}`
    )

    const handle: TerminalHandle = { socket, cwd: options?.cwd || '', onData: new Set(), onExit: new Set() }

    terminals.set(id, handle)

    // The PTY sends raw bytes, which a WebSocket surfaces as Blob by default —
    // and `String(blob)` is the literal "[object Blob]", not the terminal
    // output. Take ArrayBuffers and decode them. UTF-8 is stateful across
    // frames (a multi-byte character can straddle a chunk boundary), so one
    // decoder with `stream: true` is reused for the terminal's lifetime.
    socket.binaryType = 'arraybuffer'

    const decoder = new TextDecoder()

    socket.onmessage = event => {
      const text =
        typeof event.data === 'string' ? event.data : decoder.decode(event.data as ArrayBuffer, { stream: true })

      for (const listener of handle.onData) {
        listener(text)
      }
    }

    socket.onclose = event => {
      for (const listener of handle.onExit) {
        listener({ code: event.code === 1000 ? 0 : event.code, signal: null })
      }

      terminals.delete(id)
    }

    return { cwd: handle.cwd, id, shell: 'shell' }
  },
  write: async (id: string, data: string) => {
    const handle = terminals.get(id)

    if (!handle || handle.socket.readyState !== WebSocket.OPEN) {
      return false
    }

    handle.socket.send(data)

    return true
  },
  // The PTY bridge reads resize as an in-band escape rather than a control
  // frame, so a resize is just bytes on the same socket.
  resize: async (id: string, size: { cols: number; rows: number }) => {
    const handle = terminals.get(id)

    if (!handle || handle.socket.readyState !== WebSocket.OPEN) {
      return false
    }

    handle.socket.send(`\x1b[RESIZE:${size.cols};${size.rows}]`)

    return true
  },
  dispose: async (id: string) => {
    terminals.get(id)?.socket.close()
    terminals.delete(id)

    return true
  },
  cwd: async (id: string) => terminals.get(id)?.cwd ?? null,
  onData: (id: string, callback: (payload: string) => void): Unsubscribe => {
    const handle = terminals.get(id)

    handle?.onData.add(callback)

    return () => handle?.onData.delete(callback)
  },
  onExit: (id: string, callback: (payload: OpencodonTerminalExit) => void): Unsubscribe => {
    const handle = terminals.get(id)

    handle?.onExit.add(callback)

    return () => handle?.onExit.delete(callback)
  }
}

// ---------------------------------------------------------------------------
// Browser-native emulations
// ---------------------------------------------------------------------------

const downloadBlob = (blob: Blob, filename: string): void => {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')

  anchor.href = url
  anchor.download = filename
  anchor.click()

  URL.revokeObjectURL(url)
}

const writeClipboard = async (text: string): Promise<boolean> => {
  try {
    await navigator.clipboard.writeText(text)

    return true
  } catch {
    return false
  }
}

const notify = async (payload: OpencodonNotification): Promise<boolean> => {
  if (!('Notification' in window)) {
    return false
  }

  // Never prompt for permission off a background event — an unrequested
  // permission dialog mid-turn is worse than a missed notification.
  if (Notification.permission !== 'granted') {
    return false
  }

  new Notification(payload.title || 'Opencodon', { body: payload.body, silent: payload.silent })

  return true
}

/** Open a route in a new browser tab. The renderer routes on the hash, so a
 *  deep link is just `#/path` — no window-management IPC needed. */
const openHashRoute = (hash: string): boolean => Boolean(window.open(`${window.location.pathname}#${hash}`, '_blank'))

// ---------------------------------------------------------------------------

function createWebBridge(): Window['opencodonDesktop'] {
  const bridge = {
    // --- connection ---
    getConnection,
    getGatewayWsUrl: gatewayWsUrl,
    revalidateConnection: async () => ({ ok: true, rebuilt: false }),
    touchBackend: async () => ({ ok: true }),
    api,

    // --- windows ---
    // A second view is a second browser tab. `watch` opens the spectator
    // route so a running subagent can be streamed side by side.
    openSessionWindow: async (sessionId: string, opts?: { watch?: boolean }) => {
      if (!sessionId) {
        return { ok: false, error: 'missing-session' }
      }

      return { ok: openHashRoute(`/chat?session=${encodeURIComponent(sessionId)}${opts?.watch ? '&watch=1' : ''}`) }
    },
    openWindow: async () => ({ ok: openHashRoute('/') }),
    openExternal: async (url: string) => {
      window.open(url, '_blank', 'noopener,noreferrer')
    },
    openPreviewInBrowser: async (url: string) => {
      window.open(url, '_blank', 'noopener,noreferrer')
    },

    // --- filesystem ---
    readDir,
    readFileText,
    readFileDataUrl,
    writeTextFile,
    gitRoot,
    sanitizeWorkspaceCwd,
    terminal,

    // --- clipboard / media ---
    writeClipboard,
    notify,
    // A browser File already carries its bytes; the renderer's upload paths
    // fall back to reading the File when there's no host path.
    getPathForFile: () => '',
    saveImageFromUrl: async (url: string) => {
      try {
        const response = await fetch(url)

        downloadBlob(await response.blob(), url.split('/').pop() || 'image')

        return true
      } catch {
        return false
      }
    },
    saveImageBuffer: async (data: ArrayBuffer | Uint8Array, ext: string) => {
      const name = `opencodon-${Date.now()}.${ext.replace(/^\./, '')}`

      downloadBlob(new Blob([data as BlobPart]), name)

      return name
    },
    saveClipboardImage: async () => '',
    requestMicrophoneAccess: async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })

        for (const track of stream.getTracks()) {
          track.stop()
        }

        return true
      } catch {
        return false
      }
    },

    // --- host capabilities the browser genuinely lacks ---
    // These reject rather than resolve: the UI must be able to tell the user
    // the file was NOT trashed, the folder was NOT revealed.
    selectPaths: async () => [],
    revealPath: async () => false,
    openDir: async () => ({ ok: false, error: 'unavailable-in-browser' }),
    trashPath: async () => false,
    renamePath: async () => {
      throw new Error('Renaming files is not available in the browser UI.')
    },
    revealLogs: async () => ({ ok: false, path: '', error: 'unavailable-in-browser' }),
    getRecentLogs: async () => ({ path: '', lines: [] }),

    // --- preview / link helpers ---
    normalizePreviewTarget: async () => null,
    watchPreviewFile: async () => ({ id: '', url: '' }) as never,
    stopPreviewFileWatch: async () => false,
    fetchLinkTitle: async (url: string) => url,

    // --- settings / profile ---
    settings: {
      getDefaultProjectDir: async () => {
        const { cwd } = await json<{ cwd: string }>('/api/fs/default-cwd').catch(() => ({ cwd: '' }))

        return { defaultLabel: 'Home', dir: null, resolvedCwd: cwd }
      },
      pickDefaultProjectDir: async () => ({ canceled: true, dir: null }),
      setDefaultProjectDir: async (dir: null | string) => ({ dir })
    },
    profile: {
      get: async () => json<never>('/api/profiles/active'),
      // Profile switching rebinds the backend's OPENCODON_HOME. In the browser
      // the server owns that, so post the choice and reload into it.
      set: async (name: null | string) => {
        const result = await json<never>('/api/profiles/active', {
          method: 'POST',
          body: JSON.stringify({ name }),
          headers: { 'Content-Type': 'application/json' }
        })

        window.location.reload()

        return result
      }
    },

    // --- connection config: the browser is served BY the backend, so there
    //     is no remote/SSH descriptor to edit. Report the fixed local one. ---
    getConnectionConfig: async () => ({ mode: 'local' }) as never,
    saveConnectionConfig: async () => ({ mode: 'local' }) as never,
    applyConnectionConfig: async () => ({ mode: 'local' }) as never,
    testConnectionConfig: async () => ({ ok: true }) as never,
    probeConnectionConfig: async () => ({ ok: true }) as never,
    sshConfigHosts: async () => ({ hosts: [] }) as never,
    sshResolveHost: async () => ({ ok: false }) as never,
    oauthLoginConnectionConfig: async () => ({ ok: false }) as never,
    oauthLogoutConnectionConfig: async () => ({ ok: false }) as never,
    getRemoteDisplayReason: async () => null,
    cloud: {
      status: async () => ({ signedIn: false }) as never,
      login: async () => ({ ok: false, signedIn: false }) as never,
      logout: async () => ({ ok: true, signedIn: false }) as never,
      discover: async () => ({ agents: [] }) as never,
      agentSignIn: async () => ({ ok: false }) as never
    },

    // --- boot / bootstrap ---
    // The page is served BY the backend, so by the time this code runs the
    // backend is up and the first-launch installer has nothing to do. Both
    // must be fully-shaped: the renderer reads nested fields (`log.length`,
    // `progress`) without guarding, so a partial object crashes the boot
    // overlay rather than skipping it.
    getBootProgress: async (): Promise<DesktopBootProgress> => ({
      error: null,
      fakeMode: false,
      message: '',
      phase: 'ready',
      progress: 1,
      running: false,
      timestamp: Date.now()
    }),
    getBootstrapState: async (): Promise<DesktopBootstrapState> => ({
      active: false,
      manifest: null,
      stages: {},
      error: null,
      log: [],
      startedAt: null,
      completedAt: null,
      unsupportedPlatform: null
    }),
    resetBootstrap: async () => ({ ok: false }),
    repairBootstrap: async () => ({ ok: false }),
    cancelBootstrap: async () => ({ ok: false, cancelled: false }),
    getVersion: async () => {
      const status = await json<{ version?: string }>('/api/status').catch(() => ({ version: '' }))

      return {
        appVersion: status.version || 'web',
        electronVersion: '',
        nodeVersion: '',
        platform: 'web',
        opencodonRoot: ''
      } as DesktopVersionInfo
    },

    // --- updates / uninstall: managed by whoever runs the server ---
    updates: {
      check: async () => ({ supported: false, reason: 'Managed by the host running the dashboard.' }),
      apply: async () => ({ ok: false, manual: true, command: 'opencodon update' }),
      getBranch: async () => ({ branch: '' }),
      setBranch: async (name: string) => ({ branch: name }),
      onProgress: noSubscription
    },
    uninstall: {
      summary: async () => ({}) as never,
      run: async () => ({ ok: false, error: 'unavailable-in-browser' })
    },
    themes: {
      // Proxied through the backend: the Marketplace blocks cross-origin
      // browser fetches, and the server has no such restriction.
      fetchMarketplace: (id: string) => json<never>(`/api/dashboard/themes/marketplace${qs({ id })}`),
      searchMarketplace: (query: string) => json<never[]>(`/api/dashboard/themes/marketplace/search${qs({ q: query })}`)
    },

    // --- chrome hooks: the browser owns the window chrome ---
    setTitleBarTheme: () => {},
    setNativeTheme: () => {},
    setTranslucency: () => {},
    setKeepAwake: () => {},
    setPreviewShortcutActive: () => {},
    zoom: {
      get: async () => ({ level: 0, percent: 100 }),
      setPercent: () => {},
      onChanged: noSubscription
    },

    // --- main-process events with no browser source ---
    onClosePreviewRequested: noSubscription,
    onOpenUpdatesRequested: noSubscription,
    onDeepLink: noSubscription,
    signalDeepLinkReady: async () => ({ ok: true }),
    onWindowStateChanged: noSubscription,
    onFocusSession: noSubscription,
    onNotificationAction: noSubscription,
    onPreviewFileChanged: noSubscription,
    onBackendExit: noSubscription,
    onConnectionApplied: noSubscription,
    onPowerResume: noSubscription,
    onBootProgress: noSubscription,
    onBootstrapEvent: noSubscription,

    // Cross-tab cue arbitration. Electron claims these in the main process so
    // N windows don't all chime; `localStorage` is the browser equivalent that
    // works across tabs of the same origin.
    claimAmbientCue: async (key: string) => {
      const storageKey = `opencodon:cue:${key}`
      const now = Date.now()
      const previous = Number(window.localStorage.getItem(storageKey) || 0)

      if (now - previous < 5_000) {
        return false
      }

      window.localStorage.setItem(storageKey, String(now))

      return true
    }
  }

  return bridge as unknown as Window['opencodonDesktop']
}

/**
 * Install the browser bridge. No-op under Electron, where the preload script
 * has already provided the real one.
 */
export function installWebBridge(): void {
  if (typeof window === 'undefined' || window.opencodonDesktop) {
    return
  }

  window.opencodonDesktop = createWebBridge()
}
