import { persistentAtom } from '@/lib/persisted'

/**
 * What the files pane is looking at.
 *
 * `workspace` is the cwd on disk — everything the session can touch, including
 * scratch files that will not outlive it. `artifacts` is the durable plane:
 * only what the session staged, with versions and provenance behind it. The
 * two planes are deliberately distinguishable rather than merged, because
 * "this file exists" and "this file is a result" are different claims.
 */
export type FilesScope = 'artifacts' | 'workspace'

export const $filesScope = persistentAtom<FilesScope>('opencodon.desktop.filesScope', 'workspace', {
  // Anything unrecognised in storage falls back to the workspace tree rather
  // than an empty artifacts list, so a stale value can't make the pane look
  // broken.
  decode: raw => (raw === 'artifacts' ? 'artifacts' : 'workspace'),
  encode: value => value
})
