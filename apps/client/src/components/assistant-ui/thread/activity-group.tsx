// One activity line per assistant turn.
//
// Reasoning and tool calls interleave, so the library's per-run grouping
// (ReasoningGroup / ToolGroup) can only ever merge *adjacent* parts of the
// same kind — a turn that thinks, calls a tool, thinks again lands three
// stacked rows, and a long research turn lands twenty. That stack buries the
// answer under its own scaffolding.
//
// This collapses every activity run in a message behind a single shared
// header. The first renderable activity group owns the header; the rest render
// nothing but their children, gated on the same open state. Expanding shows
// the original rows, in order, unchanged.
import { useAuiState } from '@assistant-ui/react'
import { createContext, type FC, type PropsWithChildren, useContext, useEffect, useState } from 'react'

import { formatElapsed, useElapsedSeconds } from '@/components/chat/activity-timer'
import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { DisclosureRow } from '@/components/chat/disclosure-row'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

interface ActivityState {
  /** Frozen once the turn has finished; drives "Worked for 1:42". */
  elapsed: number
  open: boolean
  running: boolean
  steps: number
  /** False for history loaded after the fact — we never timed those. */
  timed: boolean
  toggle: () => void
}

const ActivityContext = createContext<ActivityState | null>(null)

// `todo` parts are hoisted to the composer status stack and render nothing
// here, so they must not count as activity — otherwise a turn whose only
// tool call is a todo update grows a header over an empty body.
const isRenderableActivity = (part: { text?: unknown; toolName?: string; type?: string } | undefined): boolean => {
  if (!part) {
    return false
  }

  if (part.type === 'tool-call') {
    return part.toolName !== 'todo'
  }

  return part.type === 'reasoning' && typeof part.text === 'string' && part.text.trim().length > 0
}

const isStep = (part: { toolName?: string; type?: string } | undefined): boolean =>
  part?.type === 'tool-call' && part.toolName !== 'todo'

/**
 * Hosts the shared open state for one message. Mounted around
 * `MessagePrimitive.Parts`, inside `MessagePrimitive.Root` so the aui state
 * for *this* message is in scope.
 */
export const ActivityProvider: FC<PropsWithChildren> = ({ children }) => {
  const messageId = useAuiState(s => s.message.id)
  const running = useAuiState(s => s.thread.isRunning && s.message.status?.type === 'running')
  const steps = useAuiState(s => s.message.parts.filter(isStep).length)

  // `null` = no explicit toggle yet, so follow the streaming default: open
  // while the turn works (the trace is the progress indicator), collapsed
  // once it lands. The first explicit toggle wins from then on. Same contract
  // as ThinkingDisclosure, so the two read as one system.
  const [userOpen, setUserOpen] = useState<boolean | null>(null)
  const [timed, setTimed] = useState(false)

  const open = userOpen ?? running
  const elapsed = useElapsedSeconds(running, `activity:${messageId}`)

  useEffect(() => {
    if (running) {
      setTimed(true)
    }
  }, [running])

  return (
    <ActivityContext.Provider value={{ elapsed, open, running, steps, timed, toggle: () => setUserOpen(!open) }}>
      {children}
    </ActivityContext.Provider>
  )
}

const ActivityHeader: FC = () => {
  const { t } = useI18n()
  const state = useContext(ActivityContext)

  if (!state) {
    return null
  }

  const { elapsed, open, running, steps, timed, toggle } = state
  const stepLabel = steps > 0 ? t.assistant.thread.activitySteps(steps) : null

  // Only claim a duration we actually measured. A turn rehydrated from
  // history has no start time, and "Worked for 0s" would be a lie.
  const lead = running
    ? t.assistant.thread.activityWorking
    : timed
      ? t.assistant.thread.activityWorked(formatElapsed(elapsed))
      : null

  return (
    <div data-slot="aui_activity-header">
      <DisclosureRow onToggle={toggle} open={open}>
        <span className="flex min-w-0 items-baseline gap-1.5">
          {lead && (
            <span
              className={cn(
                'text-[length:var(--conversation-tool-font-size)] font-medium leading-(--conversation-line-height) text-(--ui-text-secondary)',
                running && 'shimmer text-foreground/55'
              )}
            >
              {lead}
            </span>
          )}
          {stepLabel && (
            <span className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
              {lead ? `· ${stepLabel}` : stepLabel}
            </span>
          )}
          {running && (
            <ActivityTimerText
              className="text-[length:var(--conversation-caption-font-size)] tabular-nums text-(--ui-text-tertiary)"
              seconds={elapsed}
            />
          )}
        </span>
      </DisclosureRow>
    </div>
  )
}

/**
 * Wraps one reasoning or tool run. The run that starts at the message's first
 * renderable activity part draws the shared header; every later run is body
 * only, so the whole turn reads as a single line when collapsed.
 */
export const ActivitySection: FC<PropsWithChildren<{ startIndex: number }>> = ({ children, startIndex }) => {
  const state = useContext(ActivityContext)
  const firstActivityIndex = useAuiState(s => s.message.parts.findIndex(isRenderableActivity))

  // No provider (e.g. a surface that renders parts outside AssistantMessage):
  // fall back to the old always-visible behaviour rather than hiding output.
  if (!state) {
    return <>{children}</>
  }

  const ownsHeader = startIndex === firstActivityIndex

  return (
    <div className="min-w-0 max-w-full" data-slot="aui_activity-section">
      {ownsHeader && <ActivityHeader />}
      {state.open && <div className={cn(ownsHeader && 'mt-0.5')}>{children}</div>}
    </div>
  )
}
