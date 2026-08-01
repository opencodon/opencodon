import type { SessionInfo } from '@/opencodon'

import { sessionRecency, type SidebarProjectTree, type SidebarSessionGroup } from './workspace-groups'

/**
 * Recency buckets for the session list inside a project.
 *
 * A project's sessions used to be grouped by repo → branch → worktree, which
 * answers "where does this code live". Inside a project that question is
 * already settled, and the one people actually ask of a session list is "what
 * was I just doing" — so the lanes give way to time. Git structure is still
 * reachable where it belongs: the files pane and the worktree actions.
 *
 * Buckets are calendar-relative, not elapsed-time: 11pm yesterday reads as
 * "Yesterday" at 1am, not "2 hours ago under Today".
 */
export type RecencyBucketId = 'earlier' | 'month' | 'today' | 'week' | 'yesterday'

export interface RecencyBucketLabels {
  today: string
  yesterday: string
  week: string
  month: string
  earlier: string
}

const DAY_MS = 86_400_000

/** Start-of-day for `atMs`, in local time. Exported for the tests. */
export function startOfDay(atMs: number): number {
  const date = new Date(atMs)

  date.setHours(0, 0, 0, 0)

  return date.getTime()
}

/**
 * `atSeconds` is EPOCH SECONDS — that is what session rows carry (see
 * `sessionRecency` and `formatAge`), while `nowMs` is a `Date.now()`. Mixing
 * the two silently sorts every session into `earlier`, because a seconds value
 * compared against milliseconds looks like 1970.
 */
export function recencyBucket(atSeconds: number, nowMs: number): RecencyBucketId {
  const at = atSeconds * 1000
  const today = startOfDay(nowMs)

  if (at >= today) {
    return 'today'
  }

  if (at >= today - DAY_MS) {
    return 'yesterday'
  }

  if (at >= today - 7 * DAY_MS) {
    return 'week'
  }

  return at >= today - 30 * DAY_MS ? 'month' : 'earlier'
}

const BUCKET_ORDER: readonly RecencyBucketId[] = ['today', 'yesterday', 'week', 'month', 'earlier']

/** Every session in a project, across its repos and worktree lanes. */
export function projectSessions(project: SidebarProjectTree): SessionInfo[] {
  return project.repos.flatMap(repo => repo.groups.flatMap(group => group.sessions))
}

/**
 * Bucket sessions into recency groups, newest bucket first and newest session
 * first within each. Empty buckets are dropped — a heading with nothing under
 * it is noise, not structure.
 *
 * `live` is the optimistic overlay: sessions the backend snapshot hasn't folded
 * in yet. Deduped by id with the live copy winning, since it is the fresher of
 * the two.
 */
export function recencySessionGroups(
  sessions: readonly SessionInfo[],
  labels: RecencyBucketLabels,
  nowMs: number,
  live: readonly SessionInfo[] = []
): SidebarSessionGroup[] {
  const byId = new Map<string, SessionInfo>()

  for (const session of sessions) {
    byId.set(session.id, session)
  }

  for (const session of live) {
    byId.set(session.id, session)
  }

  const buckets = new Map<RecencyBucketId, SessionInfo[]>()

  for (const session of byId.values()) {
    const id = recencyBucket(sessionRecency(session), nowMs)

    buckets.set(id, [...(buckets.get(id) ?? []), session])
  }

  return BUCKET_ORDER.filter(id => buckets.get(id)?.length).map(id => ({
    id: `recency:${id}`,
    label: labels[id],
    mode: 'source' as const,
    path: null,
    sessions: (buckets.get(id) ?? []).sort((a, b) => sessionRecency(b) - sessionRecency(a))
  }))
}
