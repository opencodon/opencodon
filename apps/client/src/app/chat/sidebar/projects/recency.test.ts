import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/opencodon'

import { recencyBucket, recencySessionGroups, startOfDay } from './recency'

const LABELS = {
  today: 'Today',
  yesterday: 'Yesterday',
  week: 'This week',
  month: 'This month',
  earlier: 'Earlier'
}

// A fixed "now" at midday, so a test can step backwards without accidentally
// crossing a day boundary and asserting the wrong bucket.
const NOW = new Date(2026, 6, 30, 12, 0, 0).getTime()
const DAY = 86_400_000

// Session rows carry epoch SECONDS; `now` is a millisecond Date.now(). Keeping
// the conversion visible in the fixtures is the point — the first version of
// this module compared the two directly and swept every session into "Earlier".
const secs = (ms: number): number => Math.floor(ms / 1000)

const session = (id: string, lastActiveMs: number): SessionInfo =>
  ({ id, last_active: secs(lastActiveMs), started_at: secs(lastActiveMs) }) as SessionInfo

describe('recencyBucket', () => {
  it('reads its timestamp as epoch seconds, not milliseconds', () => {
    // The regression guard: a millisecond value here is ~55,000 years in the
    // future, and a seconds value read as ms is 1970.
    expect(recencyBucket(secs(NOW), NOW)).toBe('today')
    expect(recencyBucket(NOW, NOW)).not.toBe('earlier')
  })

  it('buckets by calendar day, not elapsed time', () => {
    // 11pm yesterday is 13 hours ago, which an elapsed-time rule would call
    // "today". Someone reading the list at 1am does not agree.
    expect(recencyBucket(secs(startOfDay(NOW)), NOW)).toBe('today')
    expect(recencyBucket(secs(startOfDay(NOW) - 1000), NOW)).toBe('yesterday')
  })

  it('widens through week, month, and earlier', () => {
    expect(recencyBucket(secs(NOW - 3 * DAY), NOW)).toBe('week')
    expect(recencyBucket(secs(NOW - 14 * DAY), NOW)).toBe('month')
    expect(recencyBucket(secs(NOW - 200 * DAY), NOW)).toBe('earlier')
  })

  it('puts a session with no timestamp at the bottom, not the top', () => {
    expect(recencyBucket(0, NOW)).toBe('earlier')
  })
})

describe('recencySessionGroups', () => {
  it('orders buckets newest-first and sessions newest-first within them', () => {
    const groups = recencySessionGroups(
      [
        session('old', NOW - 200 * DAY),
        session('today-older', NOW - 3600_000),
        session('today-newest', NOW),
        session('yesterday', startOfDay(NOW) - 3_600_000)
      ],
      LABELS,
      NOW
    )

    expect(groups.map(group => group.label)).toEqual(['Today', 'Yesterday', 'Earlier'])
    expect(groups[0].sessions.map(s => s.id)).toEqual(['today-newest', 'today-older'])
  })

  it('drops empty buckets rather than rendering a heading with nothing under it', () => {
    const groups = recencySessionGroups([session('only', NOW)], LABELS, NOW)

    expect(groups).toHaveLength(1)
    expect(groups[0].label).toBe('Today')
  })

  it('lets a live session override the snapshot copy of the same id', () => {
    // The optimistic overlay is the fresher of the two — a stale snapshot must
    // not drag a just-active session back into an older bucket.
    const groups = recencySessionGroups(
      [session('s1', NOW - 200 * DAY)],
      LABELS,
      NOW,
      [session('s1', NOW)]
    )

    expect(groups).toHaveLength(1)
    expect(groups[0].label).toBe('Today')
  })

  it('is empty for a project with no sessions', () => {
    expect(recencySessionGroups([], LABELS, NOW)).toEqual([])
  })
})
