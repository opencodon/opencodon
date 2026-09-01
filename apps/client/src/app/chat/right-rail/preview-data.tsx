/**
 * Structured views for `data`-class artifacts: a collapsible tree for JSON,
 * YAML and TOML, and a table for CSV/TSV.
 *
 * Both take already-parsed input: parsing happens in LocalFilePreview so a file
 * that doesn't parse simply doesn't offer the mode, instead of these components
 * rendering nothing.
 *
 * Rows are fixed-height and windowed through the same helper the source view
 * uses, so a 50k-row export costs the same as a 20-row one.
 */
import { loadAll as loadAllYaml, load as loadYaml } from 'js-yaml'
import { Fragment, useMemo, useState } from 'react'
import { parse as parseToml } from 'smol-toml'

import { chunkLines, useFixedRowWindow } from '@/components/chat/fixed-row-window'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

const ROW_PX = 20
const ROWS_PER_CHUNK = 200
const OVERSCAN_ROWS = 400
/** Containers at or below this depth start open; deeper ones start collapsed so
 *  a deeply nested document opens as an outline rather than a wall. */
const AUTO_EXPAND_DEPTH = 1

export type JsonValue = boolean | JsonValue[] | null | number | string | { [key: string]: JsonValue }

interface JsonRow {
  container: boolean
  /** Child count for containers — shown next to the collapsed summary. */
  count: number
  depth: number
  key: string
  path: string
  value: JsonValue
}

function isContainer(value: JsonValue): value is JsonValue[] | { [key: string]: JsonValue } {
  return typeof value === 'object' && value !== null
}

function childEntries(value: JsonValue[] | { [key: string]: JsonValue }): Array<[string, JsonValue]> {
  return Array.isArray(value) ? value.map((item, index) => [String(index), item]) : Object.entries(value)
}

/** Whether a container is open: `toggled` holds only the paths the user flipped
 *  away from the default, so "expand all" stays a single flag instead of a set
 *  the size of the document. */
function isOpen(path: string, depth: number, toggled: ReadonlySet<string>, expandAll: boolean): boolean {
  return (expandAll || depth < AUTO_EXPAND_DEPTH) !== toggled.has(path)
}

/** Depth-first flatten honoring the open state, so the window operates on a
 *  plain array and expand/collapse is a set toggle rather than a tree walk. */
function flattenJson(root: JsonValue, toggled: ReadonlySet<string>, expandAll: boolean): JsonRow[] {
  const rows: JsonRow[] = []

  const walk = (key: string, value: JsonValue, depth: number, path: string) => {
    const container = isContainer(value)
    const entries = container ? childEntries(value) : []

    rows.push({ container, count: entries.length, depth, key, path, value })

    if (!container) {
      return
    }

    if (!isOpen(path, depth, toggled, expandAll)) {
      return
    }

    for (const [childKey, childValue] of entries) {
      walk(childKey, childValue, depth + 1, `${path}/${childKey}`)
    }
  }

  walk('', root, 0, '')

  return rows
}

function scalarClass(value: JsonValue): string {
  if (value === null) {
    return 'text-muted-foreground/70'
  }

  switch (typeof value) {
    case 'boolean':
      return 'text-purple-600 dark:text-purple-300'

    case 'number':
      return 'text-amber-700 dark:text-amber-300'

    default:
      return 'text-emerald-700 dark:text-emerald-300'
  }
}

function scalarText(value: JsonValue): string {
  return typeof value === 'string' ? `"${value}"` : String(value)
}

function containerSummary(row: JsonRow): string {
  const brackets = Array.isArray(row.value) ? ['[', ']'] : ['{', '}']

  return `${brackets[0]}${row.count}${brackets[1]}`
}

/**
 * Cap on nodes materialized while normalizing a parsed document. YAML anchors
 * can expand a few hundred bytes into millions of nodes ("billion laughs"), and
 * a viewer that freezes the renderer is worse than one that shows source.
 */
const MAX_TREE_NODES = 200_000

class TreeBudgetError extends Error {}

/**
 * Coerce a parsed document into the plain JSON shape the tree renders.
 *
 * YAML and TOML produce values JSON never does — `Date` (TOML datetimes, YAML
 * timestamps), `undefined`, `Map`/`Set` (YAML complex keys) — and YAML aliases
 * can make the graph cyclic, which would send the flattener into infinite
 * recursion. `seen` breaks cycles along the current path; a shared node reached
 * twice by different paths is simply rendered twice, which is what the document
 * means. (`bigint` is defensive: neither parser emits one today — smol-toml
 * rejects integers it can't represent losslessly.)
 */
function toJsonValue(input: unknown, seen: Set<object>, budget: { nodes: number }): JsonValue {
  budget.nodes += 1

  if (budget.nodes > MAX_TREE_NODES) {
    throw new TreeBudgetError()
  }

  if (input === null || input === undefined) {
    return null
  }

  switch (typeof input) {
    case 'bigint':
      return input.toString()

    case 'boolean':

    case 'number':

    case 'string':
      return input

    case 'object':
      break

    // Functions and symbols can't come out of these parsers, but a `default`
    // keeps the return type honest.
    default:
      return String(input)
  }

  const node = input as object

  if (seen.has(node)) {
    return '[circular]'
  }

  if (node instanceof Date) {
    return Number.isNaN(node.getTime()) ? String(node) : node.toISOString()
  }

  seen.add(node)

  try {
    if (Array.isArray(node)) {
      return node.map(item => toJsonValue(item, seen, budget))
    }

    const entries = node instanceof Map ? [...node.entries()] : node instanceof Set ? [...node].entries() : null
    const out: { [key: string]: JsonValue } = {}

    for (const [key, value] of entries ?? Object.entries(node)) {
      out[String(key)] = toJsonValue(value, seen, budget)
    }

    return out
  } finally {
    seen.delete(node)
  }
}

function normalized(parse: () => unknown): { value: JsonValue } | null {
  try {
    const parsed = parse()

    // An empty or comment-only document (js-yaml yields `null`, JSON.parse
    // can't get here) has nothing to show; source view says more than a lone
    // `null` row.
    return parsed === undefined || parsed === null
      ? null
      : { value: toJsonValue(parsed, new Set(), { nodes: 0 }) }
  } catch {
    // Invalid syntax (or a document too large to walk). Either way the caller
    // drops the mode and shows source — a file the agent is still streaming is
    // invalid for a moment and must not blank the pane.
    return null
  }
}

export function parseJsonValue(text: string): { value: JsonValue } | null {
  return normalized(() => JSON.parse(text))
}

export function parseYamlValue(text: string): { value: JsonValue } | null {
  return normalized(() => {
    // A stream of `---`-separated documents is a list of documents; a single
    // one shouldn't gain a wrapper array it doesn't have on disk.
    const documents = loadAllYaml(text)

    return documents.length > 1 ? documents : loadYaml(text)
  })
}

export function parseTomlValue(text: string): { value: JsonValue } | null {
  return normalized(() => parseToml(text))
}

export function JsonTreeView({ value }: { value: JsonValue }) {
  const { t } = useI18n()
  const [toggled, setToggled] = useState<ReadonlySet<string>>(() => new Set())
  const [expandAll, setExpandAll] = useState(false)
  const rows = useMemo(() => flattenJson(value, toggled, expandAll), [expandAll, toggled, value])

  const chunks = useMemo(() => chunkLines(rows, ROWS_PER_CHUNK), [rows])

  const { afterRows, beforeRows, endChunk, onScroll, scrollerRef, startChunk } = useFixedRowWindow({
    overscanRows: OVERSCAN_ROWS,
    rowPx: ROW_PX,
    rowsPerChunk: ROWS_PER_CHUNK,
    totalRows: rows.length
  })

  const toggle = (path: string) => {
    setToggled(current => {
      const next = new Set(current)

      if (next.has(path)) {
        next.delete(path)
      } else {
        next.add(path)
      }

      return next
    })
  }

  return (
    <div className="h-full overflow-auto" onScroll={onScroll} ref={scrollerRef}>
      <div className="min-w-max py-1 font-mono text-[0.7rem] leading-5" data-selectable-text="true">
        {beforeRows > 0 && <div aria-hidden style={{ height: beforeRows * ROW_PX }} />}
        {chunks.slice(startChunk, endChunk + 1).map(chunk => (
          <Fragment key={chunk.start}>
            {chunk.lines.map(row => {
              const open = row.container && isOpen(row.path, row.depth, toggled, expandAll)

              return (
                <div
                  className={cn(
                    'flex h-5 items-center gap-1.5 pr-3 whitespace-nowrap',
                    row.container && 'cursor-pointer hover:bg-accent/40'
                  )}
                  key={row.path || 'root'}
                  onClick={row.container ? () => toggle(row.path) : undefined}
                  style={{ paddingLeft: `${0.75 + row.depth * 0.85}rem` }}
                >
                  {row.container ? (
                    <span className="w-2 select-none text-muted-foreground/70">{open ? '▾' : '▸'}</span>
                  ) : (
                    <span aria-hidden className="w-2" />
                  )}
                  {row.path !== '' && <span className="text-foreground/80">{row.key}</span>}
                  {row.path !== '' && <span className="text-muted-foreground/50">:</span>}
                  {row.container ? (
                    <span className="text-muted-foreground/70">{containerSummary(row)}</span>
                  ) : (
                    <span className={scalarClass(row.value)}>{scalarText(row.value)}</span>
                  )}
                </div>
              )
            })}
          </Fragment>
        ))}
        {afterRows > 0 && <div aria-hidden style={{ height: afterRows * ROW_PX }} />}
      </div>
      <button
        className="sticky bottom-0 left-0 w-full border-t border-border/40 bg-background/90 py-1 text-[0.625rem] font-bold text-muted-foreground transition-colors hover:text-foreground"
        onClick={() => {
          setToggled(new Set())
          setExpandAll(current => !current)
        }}
        type="button"
      >
        {expandAll ? t.preview.collapseAll : t.preview.expandAll}
      </button>
    </div>
  )
}

/** RFC-4180-ish split: honors quoted fields, doubled quotes, and newlines
 *  inside quotes. Enough for the exports an agent actually writes. */
export function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]

    if (quoted) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          field += '"'
          index += 1
        } else {
          quoted = false
        }
      } else {
        field += char
      }

      continue
    }

    if (char === '"') {
      quoted = true
    } else if (char === delimiter) {
      row.push(field)
      field = ''
    } else if (char === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else if (char !== '\r') {
      field += char
    }
  }

  if (field || row.length > 0) {
    row.push(field)
    rows.push(row)
  }

  return rows
}

export function CsvTableView({ rows }: { rows: string[][] }) {
  const header = rows[0] ?? []
  const body = useMemo(() => rows.slice(1), [rows])
  const chunks = useMemo(() => chunkLines(body, ROWS_PER_CHUNK), [body])

  const { afterRows, beforeRows, endChunk, onScroll, scrollerRef, startChunk } = useFixedRowWindow({
    overscanRows: OVERSCAN_ROWS,
    rowPx: ROW_PX,
    rowsPerChunk: ROWS_PER_CHUNK,
    totalRows: body.length
  })

  return (
    <div className="h-full overflow-auto" onScroll={onScroll} ref={scrollerRef}>
      <table className="min-w-full border-collapse font-mono text-[0.7rem]" data-selectable-text="true">
        <thead className="sticky top-0 z-10 bg-background">
          <tr>
            <th className="h-5 border-b border-border/60 px-2 text-right font-normal text-muted-foreground/55 tabular-nums">
              #
            </th>
            {header.map((cell, index) => (
              <th
                className="h-5 border-b border-border/60 px-2 text-left font-bold whitespace-nowrap text-foreground/90"
                key={`${cell}:${index}`}
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {beforeRows > 0 && (
            <tr aria-hidden>
              <td colSpan={header.length + 1} style={{ height: beforeRows * ROW_PX, padding: 0 }} />
            </tr>
          )}
          {chunks.slice(startChunk, endChunk + 1).map(chunk =>
            chunk.lines.map((cells, offset) => {
              const line = chunk.start + offset + 1

              return (
                <tr className="hover:bg-accent/30" key={line}>
                  <td className="h-5 px-2 text-right leading-5 text-muted-foreground/45 tabular-nums select-none">
                    {line}
                  </td>
                  {header.map((_column, column) => (
                    <td className="h-5 px-2 leading-5 whitespace-nowrap text-foreground/85" key={column}>
                      {cells[column] ?? ''}
                    </td>
                  ))}
                </tr>
              )
            })
          )}
          {afterRows > 0 && (
            <tr aria-hidden>
              <td colSpan={header.length + 1} style={{ height: afterRows * ROW_PX, padding: 0 }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
