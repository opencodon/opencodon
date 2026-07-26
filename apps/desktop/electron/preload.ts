import { contextBridge, ipcRenderer, webUtils } from 'electron'

contextBridge.exposeInMainWorld('opencodonDesktop', {
  getConnection: profile => ipcRenderer.invoke('opencodon:connection', profile),
  revalidateConnection: () => ipcRenderer.invoke('opencodon:connection:revalidate'),
  touchBackend: profile => ipcRenderer.invoke('opencodon:backend:touch', profile),
  getGatewayWsUrl: profile => ipcRenderer.invoke('opencodon:gateway:ws-url', profile),
  openSessionWindow: (sessionId, opts) => ipcRenderer.invoke('opencodon:window:openSession', sessionId, opts),
  openWindow: () => ipcRenderer.invoke('opencodon:window:openInstance'),
  claimAmbientCue: key => ipcRenderer.invoke('opencodon:ambient:claim', key),
  getBootProgress: () => ipcRenderer.invoke('opencodon:boot-progress:get'),
  getConnectionConfig: profile => ipcRenderer.invoke('opencodon:connection-config:get', profile),
  saveConnectionConfig: payload => ipcRenderer.invoke('opencodon:connection-config:save', payload),
  applyConnectionConfig: payload => ipcRenderer.invoke('opencodon:connection-config:apply', payload),
  testConnectionConfig: payload => ipcRenderer.invoke('opencodon:connection-config:test', payload),
  sshConfigHosts: () => ipcRenderer.invoke('opencodon:ssh-config:hosts'),
  sshResolveHost: host => ipcRenderer.invoke('opencodon:ssh-config:resolve', host),
  probeConnectionConfig: remoteUrl => ipcRenderer.invoke('opencodon:connection-config:probe', remoteUrl),
  oauthLoginConnectionConfig: remoteUrl => ipcRenderer.invoke('opencodon:connection-config:oauth-login', remoteUrl),
  oauthLogoutConnectionConfig: remoteUrl => ipcRenderer.invoke('opencodon:connection-config:oauth-logout', remoteUrl),
  // Opencodon Cloud: one portal login powers discovery + silent per-agent sign-in
  // (cloud-auto-discovery Phase 3).
  cloud: {
    status: () => ipcRenderer.invoke('opencodon:cloud:status'),
    login: () => ipcRenderer.invoke('opencodon:cloud:login'),
    logout: () => ipcRenderer.invoke('opencodon:cloud:logout'),
    discover: org => ipcRenderer.invoke('opencodon:cloud:discover', org),
    agentSignIn: dashboardUrl => ipcRenderer.invoke('opencodon:cloud:agent-sign-in', dashboardUrl)
  },
  profile: {
    get: () => ipcRenderer.invoke('opencodon:profile:get'),
    set: name => ipcRenderer.invoke('opencodon:profile:set', name)
  },
  api: request => ipcRenderer.invoke('opencodon:api', request),
  notify: payload => ipcRenderer.invoke('opencodon:notify', payload),
  requestMicrophoneAccess: () => ipcRenderer.invoke('opencodon:requestMicrophoneAccess'),
  readFileDataUrl: filePath => ipcRenderer.invoke('opencodon:readFileDataUrl', filePath),
  readFileText: filePath => ipcRenderer.invoke('opencodon:readFileText', filePath),
  selectPaths: options => ipcRenderer.invoke('opencodon:selectPaths', options),
  writeClipboard: text => ipcRenderer.invoke('opencodon:writeClipboard', text),
  saveImageFromUrl: url => ipcRenderer.invoke('opencodon:saveImageFromUrl', url),
  saveImageBuffer: (data, ext) => ipcRenderer.invoke('opencodon:saveImageBuffer', { data, ext }),
  saveClipboardImage: () => ipcRenderer.invoke('opencodon:saveClipboardImage'),
  getPathForFile: file => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  normalizePreviewTarget: (target, baseDir) => ipcRenderer.invoke('opencodon:normalizePreviewTarget', target, baseDir),
  watchPreviewFile: url => ipcRenderer.invoke('opencodon:watchPreviewFile', url),
  stopPreviewFileWatch: id => ipcRenderer.invoke('opencodon:stopPreviewFileWatch', id),
  setTitleBarTheme: payload => ipcRenderer.send('opencodon:titlebar-theme', payload),
  setNativeTheme: mode => ipcRenderer.send('opencodon:native-theme', mode),
  setTranslucency: payload => ipcRenderer.send('opencodon:translucency', payload),
  setKeepAwake: on => ipcRenderer.send('opencodon:keep-awake', on),
  setPreviewShortcutActive: active => ipcRenderer.send('opencodon:previewShortcutActive', Boolean(active)),
  openExternal: url => ipcRenderer.invoke('opencodon:openExternal', url),
  openPreviewInBrowser: url => ipcRenderer.invoke('opencodon:openPreviewInBrowser', url),
  fetchLinkTitle: url => ipcRenderer.invoke('opencodon:fetchLinkTitle', url),
  sanitizeWorkspaceCwd: cwd => ipcRenderer.invoke('opencodon:workspace:sanitize', cwd),
  settings: {
    getDefaultProjectDir: () => ipcRenderer.invoke('opencodon:setting:defaultProjectDir:get'),
    setDefaultProjectDir: dir => ipcRenderer.invoke('opencodon:setting:defaultProjectDir:set', dir),
    pickDefaultProjectDir: () => ipcRenderer.invoke('opencodon:setting:defaultProjectDir:pick')
  },
  zoom: {
    // Current zoom of this window, as { level, percent }.
    get: () => ipcRenderer.invoke('opencodon:zoom:get'),
    setPercent: percent => ipcRenderer.send('opencodon:zoom:set-percent', percent),
    // Fires on every zoom change, including the Ctrl/Cmd +/-/0 shortcuts,
    // so the settings UI can stay in sync with the keyboard.
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('opencodon:zoom:changed', listener)

      return () => ipcRenderer.removeListener('opencodon:zoom:changed', listener)
    }
  },
  revealLogs: () => ipcRenderer.invoke('opencodon:logs:reveal'),
  getRecentLogs: () => ipcRenderer.invoke('opencodon:logs:recent'),
  readDir: dirPath => ipcRenderer.invoke('opencodon:fs:readDir', dirPath),
  gitRoot: startPath => ipcRenderer.invoke('opencodon:fs:gitRoot', startPath),
  revealPath: targetPath => ipcRenderer.invoke('opencodon:fs:reveal', targetPath),
  openDir: dirPath => ipcRenderer.invoke('opencodon:fs:openDir', dirPath),
  renamePath: (targetPath, newName) => ipcRenderer.invoke('opencodon:fs:rename', targetPath, newName),
  writeTextFile: (filePath, content) => ipcRenderer.invoke('opencodon:fs:writeText', filePath, content),
  trashPath: targetPath => ipcRenderer.invoke('opencodon:fs:trash', targetPath),
  git: {
    worktreeList: repoPath => ipcRenderer.invoke('opencodon:git:worktreeList', repoPath),
    worktreeAdd: (repoPath, options) => ipcRenderer.invoke('opencodon:git:worktreeAdd', repoPath, options),
    worktreeRemove: (repoPath, worktreePath, options) =>
      ipcRenderer.invoke('opencodon:git:worktreeRemove', repoPath, worktreePath, options),
    branchSwitch: (repoPath, branch) => ipcRenderer.invoke('opencodon:git:branchSwitch', repoPath, branch),
    branchList: repoPath => ipcRenderer.invoke('opencodon:git:branchList', repoPath),
    baseBranchList: repoPath => ipcRenderer.invoke('opencodon:git:baseBranchList', repoPath),
    repoStatus: repoPath => ipcRenderer.invoke('opencodon:git:repoStatus', repoPath),
    fileDiff: (repoPath, filePath) => ipcRenderer.invoke('opencodon:git:fileDiff', repoPath, filePath),
    scanRepos: (roots, options) => ipcRenderer.invoke('opencodon:git:scanRepos', roots, options),
    review: {
      list: (repoPath, scope, baseRef) => ipcRenderer.invoke('opencodon:git:review:list', repoPath, scope, baseRef),
      diff: (repoPath, filePath, scope, baseRef, staged) =>
        ipcRenderer.invoke('opencodon:git:review:diff', repoPath, filePath, scope, baseRef, staged),
      stage: (repoPath, filePath) => ipcRenderer.invoke('opencodon:git:review:stage', repoPath, filePath),
      unstage: (repoPath, filePath) => ipcRenderer.invoke('opencodon:git:review:unstage', repoPath, filePath),
      revert: (repoPath, filePath) => ipcRenderer.invoke('opencodon:git:review:revert', repoPath, filePath),
      revParse: (repoPath, ref) => ipcRenderer.invoke('opencodon:git:review:revParse', repoPath, ref),
      commit: (repoPath, message, push) => ipcRenderer.invoke('opencodon:git:review:commit', repoPath, message, push),
      commitContext: repoPath => ipcRenderer.invoke('opencodon:git:review:commitContext', repoPath),
      push: repoPath => ipcRenderer.invoke('opencodon:git:review:push', repoPath),
      shipInfo: repoPath => ipcRenderer.invoke('opencodon:git:review:shipInfo', repoPath),
      createPr: repoPath => ipcRenderer.invoke('opencodon:git:review:createPr', repoPath)
    }
  },
  terminal: {
    cwd: id => ipcRenderer.invoke('opencodon:terminal:cwd', id),
    dispose: id => ipcRenderer.invoke('opencodon:terminal:dispose', id),
    resize: (id, size) => ipcRenderer.invoke('opencodon:terminal:resize', id, size),
    start: options => ipcRenderer.invoke('opencodon:terminal:start', options),
    write: (id, data) => ipcRenderer.invoke('opencodon:terminal:write', id, data),
    onData: (id, callback) => {
      const channel = `opencodon:terminal:${id}:data`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    },
    onExit: (id, callback) => {
      const channel = `opencodon:terminal:${id}:exit`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    }
  },
  onClosePreviewRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('opencodon:close-preview-requested', listener)

    return () => ipcRenderer.removeListener('opencodon:close-preview-requested', listener)
  },
  onOpenUpdatesRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('opencodon:open-updates', listener)

    return () => ipcRenderer.removeListener('opencodon:open-updates', listener)
  },
  onDeepLink: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('opencodon:deep-link', listener)

    return () => ipcRenderer.removeListener('opencodon:deep-link', listener)
  },
  signalDeepLinkReady: () => ipcRenderer.invoke('opencodon:deep-link-ready'),
  onWindowStateChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('opencodon:window-state-changed', listener)

    return () => ipcRenderer.removeListener('opencodon:window-state-changed', listener)
  },
  onFocusSession: callback => {
    const listener = (_event, sessionId) => callback(sessionId)
    ipcRenderer.on('opencodon:focus-session', listener)

    return () => ipcRenderer.removeListener('opencodon:focus-session', listener)
  },
  onNotificationAction: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('opencodon:notification-action', listener)

    return () => ipcRenderer.removeListener('opencodon:notification-action', listener)
  },
  onPreviewFileChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('opencodon:preview-file-changed', listener)

    return () => ipcRenderer.removeListener('opencodon:preview-file-changed', listener)
  },
  onBackendExit: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('opencodon:backend-exit', listener)

    return () => ipcRenderer.removeListener('opencodon:backend-exit', listener)
  },
  // Soft gateway-mode apply finished tearing down the primary backend. Renderer
  // should wipe session lists + re-dial without a window reload.
  onConnectionApplied: callback => {
    const listener = () => callback()
    ipcRenderer.on('opencodon:connection:applied', listener)

    return () => ipcRenderer.removeListener('opencodon:connection:applied', listener)
  },
  onPowerResume: callback => {
    const listener = () => callback()
    ipcRenderer.on('opencodon:power-resume', listener)

    return () => ipcRenderer.removeListener('opencodon:power-resume', listener)
  },
  onBootProgress: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('opencodon:boot-progress', listener)

    return () => ipcRenderer.removeListener('opencodon:boot-progress', listener)
  },
  // First-launch bootstrap progress -- emitted by the install.ps1 stage
  // runner in main.ts (apps/desktop/electron/bootstrap-runner.ts).
  // Renderer's install overlay subscribes to live events and queries the
  // current snapshot via getBootstrapState() to recover after a devtools
  // reload mid-bootstrap.
  getBootstrapState: () => ipcRenderer.invoke('opencodon:bootstrap:get'),
  resetBootstrap: () => ipcRenderer.invoke('opencodon:bootstrap:reset'),
  repairBootstrap: () => ipcRenderer.invoke('opencodon:bootstrap:repair'),
  cancelBootstrap: () => ipcRenderer.invoke('opencodon:bootstrap:cancel'),
  onBootstrapEvent: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('opencodon:bootstrap:event', listener)

    return () => ipcRenderer.removeListener('opencodon:bootstrap:event', listener)
  },
  getVersion: () => ipcRenderer.invoke('opencodon:version'),
  getRemoteDisplayReason: () => ipcRenderer.invoke('opencodon:get-remote-display-reason'),
  uninstall: {
    summary: () => ipcRenderer.invoke('opencodon:uninstall:summary'),
    run: mode => ipcRenderer.invoke('opencodon:uninstall:run', { mode })
  },
  updates: {
    check: () => ipcRenderer.invoke('opencodon:updates:check'),
    apply: opts => ipcRenderer.invoke('opencodon:updates:apply', opts),
    getBranch: () => ipcRenderer.invoke('opencodon:updates:branch:get'),
    setBranch: name => ipcRenderer.invoke('opencodon:updates:branch:set', name),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('opencodon:updates:progress', listener)

      return () => ipcRenderer.removeListener('opencodon:updates:progress', listener)
    }
  },
  themes: {
    fetchMarketplace: id => ipcRenderer.invoke('opencodon:vscode-theme:fetch', id),
    searchMarketplace: query => ipcRenderer.invoke('opencodon:vscode-theme:search', query)
  }
})
