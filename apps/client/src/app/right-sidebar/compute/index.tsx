/**
 * Compute — what is running on this machine right now.
 *
 * Kernel state is a *fact the reader needs*, not a control: whether the
 * namespace that produced their artifacts is still alive decides whether a
 * follow-up cell can build on it. So this pane reports and never interrupts,
 * restarts, or kills — those belong where the code runs.
 *
 * Deliberately scoped to the backend's own process. Kernels live in a
 * module-level manager, so a session running under a separate CLI or cron
 * process keeps its own and is not visible here; the pane says so rather than
 * implying the machine is idle.
 */

import { useQuery } from '@tanstack/react-query'

import { EmptyState } from '@/components/ui/empty-state'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { scienceApi } from '../../science/api'
import { formatBytes } from '../../science/format'
import { SidebarPanelLabel } from '../../shell/sidebar-label'
import { RightSidebarSectionHeader } from '../index'

const POLL_MS = 4000

export function ComputePane() {
  const { t } = useI18n()
  const r = t.rightSidebar

  const { data, isError } = useQuery({
    queryKey: ['science-kernels'],
    queryFn: () => scienceApi.kernels(),
    // Host load is only meaningful while someone is looking at it, and a live
    // kernel can die between polls, so this refreshes rather than caches.
    refetchInterval: POLL_MS
  })

  const host = data?.host ?? {}
  const kernels = data?.kernels ?? []
  const running = kernels.filter(kernel => kernel.alive).length

  return (
    <div className="flex h-full min-h-0 flex-col">
      <RightSidebarSectionHeader>
        <div className="flex min-w-0 flex-1">
          <SidebarPanelLabel>{r.computeTitle}</SidebarPanelLabel>
        </div>
        <span className="shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)">
          {running === 0 ? r.computeNoneRunning : r.computeRunning(running)}
        </span>
      </RightSidebarSectionHeader>

      <div className="flex flex-col gap-3 overflow-y-auto px-2.5 py-2">
        <div className="flex flex-col gap-2">
          <Meter
            label={r.computeCpu}
            percent={host.cpu_percent ?? null}
            detail={host.cpu_count ? r.computeCores(host.cpu_count) : ''}
          />
          <Meter
            label={r.computeMemory}
            percent={host.memory_percent ?? null}
            detail={
              host.memory_used_bytes && host.memory_total_bytes
                ? `${formatBytes(host.memory_used_bytes)} / ${formatBytes(host.memory_total_bytes)}`
                : ''
            }
          />
        </div>

        {isError ? (
          <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{r.computeUnavailable}</p>
        ) : data?.unavailable_reason ? (
          <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{data.unavailable_reason}</p>
        ) : kernels.length === 0 ? (
          <EmptyState description={r.computeEmptyDesc} title={r.computeEmptyTitle} />
        ) : (
          <ul className="flex flex-col gap-1.5">
            {kernels.map(kernel => (
              <li
                className="rounded-md border border-(--ui-stroke-tertiary) px-2 py-1.5 text-[0.6875rem]"
                key={kernel.kernel_id ?? `${kernel.session_id}-${kernel.language}`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="min-w-0 truncate">
                    {kernel.language}
                    {kernel.env_name ? ` · ${kernel.env_name}` : ''}
                  </span>
                  <span className={cn('shrink-0', kernel.alive ? 'text-success' : 'text-destructive')}>
                    {kernel.alive ? r.computeAlive : r.computeDead}
                  </span>
                </div>
                <div className="mt-0.5 truncate text-(--ui-text-quaternary)">
                  {kernel.location}
                  {kernel.runtime_identity ? ` · ${kernel.runtime_identity}` : ''}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function Meter({ detail, label, percent }: { detail: string; label: string; percent: null | number }) {
  const value = percent === null ? 0 : Math.max(0, Math.min(100, percent))

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between gap-2 text-[0.6875rem]">
        <span className="text-(--ui-text-secondary)">{label}</span>
        <span className="text-(--ui-text-quaternary)">
          {percent === null ? '—' : `${Math.round(value)}%`}
          {detail ? ` · ${detail}` : ''}
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-(--ui-stroke-tertiary)">
        <div className="h-full rounded-full bg-(--ui-text-tertiary)" style={{ width: `${value}%` }} />
      </div>
    </div>
  )
}
