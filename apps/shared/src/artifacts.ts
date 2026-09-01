/**
 * The single extension table behind every preview decision.
 *
 * Before this module the same map lived twice — once in the renderer's
 * `lib/local-preview.ts` fallback and once as `PREVIEW_LANGUAGE_BY_EXT` in the
 * Electron main process — so adding a file type meant editing two files that
 * silently disagreed in between. Both now import from here, which also gives
 * artifact detection (`artifactClass`) a definition it shares with the
 * normalizers instead of its own regex.
 */

/**
 * What a file *is*, for preview purposes — coarser than a language and
 * orthogonal to `PreviewTarget['previewKind']`, which describes how the pane
 * renders it.
 *
 * - `web`   — has a live rendering (HTML)
 * - `doc`   — prose meant to be read rendered (Markdown)
 * - `data`  — structured, worth a tree/table view (JSON, YAML, TOML, CSV)
 * - `image` — raster or vector art
 * - `code`  — source; useful to open, but not an artifact of a turn
 * - `other` — text with no known handling (`.log`, `.ext`, extensionless)
 * - `binary`— never previewable inline
 */
export type ArtifactClass = 'binary' | 'code' | 'data' | 'doc' | 'image' | 'other' | 'web'

export const HTML_EXTENSIONS: ReadonlySet<string> = new Set(['.htm', '.html'])

export const IMAGE_EXTENSIONS: ReadonlySet<string> = new Set([
  '.bmp',
  '.gif',
  '.jpeg',
  '.jpg',
  '.png',
  '.svg',
  '.webp'
])

const DOC_EXTENSIONS: ReadonlySet<string> = new Set(['.markdown', '.md', '.mdx'])

const DATA_EXTENSIONS: ReadonlySet<string> = new Set(['.csv', '.json', '.jsonc', '.toml', '.tsv', '.yaml', '.yml'])

const BINARY_EXTENSIONS: ReadonlySet<string> = new Set([
  '.7z',
  '.bin',
  '.class',
  '.dll',
  '.dmg',
  '.dylib',
  '.exe',
  '.gz',
  '.ico',
  '.jar',
  '.m4a',
  '.mkv',
  '.mov',
  '.mp3',
  '.mp4',
  '.o',
  '.ogg',
  '.opus',
  '.pdf',
  '.so',
  '.tar',
  '.wasm',
  '.wav',
  '.webm',
  '.woff',
  '.woff2',
  '.zip'
])

export const LANGUAGE_BY_EXTENSION: Readonly<Record<string, string>> = {
  '.c': 'c',
  '.conf': 'ini',
  '.cpp': 'cpp',
  '.css': 'css',
  '.csv': 'csv',
  '.go': 'go',
  '.graphql': 'graphql',
  '.h': 'c',
  '.hpp': 'cpp',
  '.html': 'html',
  '.ini': 'ini',
  '.java': 'java',
  '.js': 'javascript',
  '.json': 'json',
  '.jsonc': 'json',
  '.jsx': 'jsx',
  '.kt': 'kotlin',
  '.log': 'text',
  '.lua': 'lua',
  '.markdown': 'markdown',
  '.md': 'markdown',
  '.mdx': 'markdown',
  '.mjs': 'javascript',
  '.py': 'python',
  '.rb': 'ruby',
  '.rs': 'rust',
  '.sh': 'shell',
  '.sql': 'sql',
  '.svg': 'xml',
  '.toml': 'toml',
  '.ts': 'typescript',
  '.tsv': 'csv',
  '.tsx': 'tsx',
  '.txt': 'text',
  '.xml': 'xml',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.zsh': 'shell'
}

/** Lowercased extension including the dot, or `''`. Query/hash tails are dropped
 *  so a URL-ish target classifies the same as the bare path. */
export function fileExtension(value: string): string {
  const clean = String(value || '').split(/[?#]/, 1)[0] || ''
  const name = clean.split(/[\\/]/).filter(Boolean).pop() || ''
  const idx = name.lastIndexOf('.')

  return idx > 0 ? name.slice(idx).toLowerCase() : ''
}

export function languageForPath(value: string): string {
  return LANGUAGE_BY_EXTENSION[fileExtension(value)] || 'text'
}

export function artifactClass(value: string): ArtifactClass {
  const ext = fileExtension(value)

  if (HTML_EXTENSIONS.has(ext)) {
    return 'web'
  }

  if (IMAGE_EXTENSIONS.has(ext)) {
    return 'image'
  }

  if (DOC_EXTENSIONS.has(ext)) {
    return 'doc'
  }

  if (DATA_EXTENSIONS.has(ext)) {
    return 'data'
  }

  if (BINARY_EXTENSIONS.has(ext)) {
    return 'binary'
  }

  // A known language that isn't one of the classes above is source code. Plain
  // `.txt`/`.log` land in `other`: previewable, but never an auto-surfaced
  // artifact.
  const language = LANGUAGE_BY_EXTENSION[ext]

  if (language && language !== 'text') {
    return 'code'
  }

  return 'other'
}

/**
 * Classes worth surfacing unprompted as "the turn produced this". Deliberately
 * excludes `code` — during ordinary editing every touched source file would
 * otherwise become a chip. Source files stay reachable by clicking the path in
 * the tool row.
 */
const AUTO_SURFACED: ReadonlySet<ArtifactClass> = new Set<ArtifactClass>(['data', 'doc', 'image', 'web'])

export function isAutoSurfacedArtifact(value: string): boolean {
  return AUTO_SURFACED.has(artifactClass(value))
}

/** Anything the preview pane can show inline, source view included. */
export function isPreviewableClass(value: string): boolean {
  return artifactClass(value) !== 'binary'
}
