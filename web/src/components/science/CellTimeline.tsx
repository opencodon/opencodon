/**
 * CellTimeline — the execution trace, rendered as an append-only list.
 *
 * One component, three placements: a frame's full trace, an artifact's
 * provenance (filtered to producing cells), and the cell detail view. It is
 * strictly a view — the dashboard never submits or edits cells, so there is
 * no run control here and no in-place editing of recorded source.
 *
 * Collapsed rows carry the outcome on the right (host calls, artifacts,
 * output lines) so a reader scanning the trace sees what each cell did
 * without opening it. Detail is fetched lazily on expand.
 */

import { useCallback, useState } from "react";
import { ChevronDown, ChevronRight, CircleAlert, CircleCheck, User } from "lucide-react";
import { Link } from "react-router-dom";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type { CellDetail, CellSummary } from "@/lib/api";
import { formatAge, formatBytes, languageLabel } from "@/lib/science-format";
import { cn } from "@/lib/utils";

function outcomeLabel(cell: CellSummary): string {
  const parts: string[] = [];
  if (cell.version_count) {
    parts.push(`${cell.version_count} artifact${cell.version_count === 1 ? "" : "s"}`);
  }
  if (cell.host_call_count) {
    parts.push(`${cell.host_call_count} host call${cell.host_call_count === 1 ? "" : "s"}`);
  }
  const lines = cell.stdout ? cell.stdout.trimEnd().split("\n").length : 0;
  if (lines) parts.push(`${lines} line${lines === 1 ? "" : "s"} of output`);
  return parts.join(" · ");
}

function StatusIcon({ status }: { status: string }) {
  if (status === "ok") {
    return <CircleCheck className="h-3.5 w-3.5 shrink-0 text-success" aria-hidden />;
  }
  return (
    <CircleAlert className="h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden />
  );
}

function Stream({ label, text }: { label: string; text: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-display text-text-tertiary">{label}</span>
      <pre className="overflow-x-auto rounded border border-border bg-card p-2 font-mono text-xs text-text-secondary">
        {text}
      </pre>
    </div>
  );
}

function CellBody({ cell }: { cell: CellDetail }) {
  return (
    <div className="flex flex-col gap-3 border-t border-border px-3 py-3">
      <div className="flex flex-col gap-1">
        <span className="text-xs text-display text-text-tertiary">Source</span>
        <pre className="overflow-x-auto rounded border border-border bg-card p-2 font-mono text-xs text-text-primary">
          {cell.source ?? ""}
        </pre>
      </div>

      {cell.stdout ? <Stream label="stdout" text={cell.stdout} /> : null}
      {cell.stderr ? <Stream label="stderr" text={cell.stderr} /> : null}

      {cell.host_calls.length > 0 ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-display text-text-tertiary">
            Host calls
          </span>
          <ul className="flex flex-col gap-1">
            {cell.host_calls.map((call) => (
              <li
                key={call.seq}
                className="flex items-center justify-between gap-3 rounded border border-border px-2 py-1"
              >
                <span className="flex items-center gap-2 font-mono text-xs text-text-primary">
                  <span className="text-text-tertiary">{call.seq}</span>
                  {call.method}
                  {call.derivable ? (
                    <Badge tone="secondary" className="normal-case">
                      derivable
                    </Badge>
                  ) : null}
                </span>
                <span className="shrink-0 text-xs text-text-tertiary">
                  {call.error ? (
                    <span className="text-destructive">{call.error}</span>
                  ) : (
                    formatBytes(call.bytes)
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {cell.versions.length > 0 ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-display text-text-tertiary">
            Artifacts produced
          </span>
          <ul className="flex flex-col gap-1">
            {cell.versions.map((version) => (
              <li key={version.version_id}>
                <Link
                  className="flex items-center justify-between gap-3 rounded border border-border px-2 py-1 text-xs hover:bg-card"
                  to={`/artifacts/${version.artifact_id}?version=${version.version_id}`}
                >
                  <span className="font-mono text-text-primary">
                    v{version.version_number}
                  </span>
                  <span className="text-text-tertiary">
                    {formatBytes(version.size_bytes)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {cell.env_lock_hash || cell.kernel_location ? (
        <div className="flex flex-wrap gap-4 text-xs text-text-tertiary">
          {cell.kernel_location ? <span>ran on {cell.kernel_location}</span> : null}
          {cell.env_lock_hash ? (
            <span className="font-mono" title="Environment lock identity">
              lock {cell.env_lock_hash.slice(0, 12)}
            </span>
          ) : (
            <span title="Without a lock, a byte match can only be graded 'reproduced'">
              no environment lock recorded
            </span>
          )}
        </div>
      ) : null}

      {cell.files_read?.length || cell.files_written?.length ? (
        <div className="flex flex-wrap gap-4 text-xs text-text-tertiary">
          {cell.files_read?.length ? (
            <span>read: {cell.files_read.join(", ")}</span>
          ) : null}
          {cell.files_written?.length ? (
            <span>wrote: {cell.files_written.join(", ")}</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** True when a summary is already a full detail payload (no fetch needed). */
function isDetail(cell: CellSummary | CellDetail): cell is CellDetail {
  return Array.isArray((cell as CellDetail).host_calls);
}

export function CellTimeline({
  cells,
  defaultOpenId = null,
  showPermalink = true,
}: {
  cells: Array<CellSummary | CellDetail>;
  /** Expand this cell on mount — used by the single-cell permalink page. */
  defaultOpenId?: string | null;
  /** Off on the cell page itself, where the permalink is the current URL. */
  showPermalink?: boolean;
}) {
  const [openId, setOpenId] = useState<string | null>(defaultOpenId);
  // Callers that already hold a detail payload (the artifact's producing
  // cell, the cell permalink page) seed the cache so expanding costs nothing.
  const [details, setDetails] = useState<Record<string, CellDetail>>(() =>
    Object.fromEntries(
      cells.filter(isDetail).map((cell) => [cell.cell_id, cell]),
    ),
  );
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [errorId, setErrorId] = useState<string | null>(null);

  const toggle = useCallback(
    (cellId: string) => {
      if (openId === cellId) {
        setOpenId(null);
        return;
      }
      setOpenId(cellId);
      if (details[cellId]) return;
      setLoadingId(cellId);
      setErrorId(null);
      api
        .getCell(cellId)
        .then((detail) => {
          setDetails((prev) => ({ ...prev, [cellId]: detail }));
        })
        .catch(() => setErrorId(cellId))
        .finally(() => setLoadingId(null));
    },
    [details, openId],
  );

  if (cells.length === 0) {
    return (
      <p className="px-3 py-6 text-xs text-text-secondary">
        This frame has no recorded cells.
      </p>
    );
  }

  return (
    <ul className="flex flex-col gap-1.5">
      {cells.map((cell) => {
        const open = openId === cell.cell_id;
        const outcome = outcomeLabel(cell);
        return (
          <li
            key={cell.cell_id}
            className="overflow-hidden rounded border border-border"
          >
            <button
              type="button"
              onClick={() => toggle(cell.cell_id)}
              aria-expanded={open}
              className={cn(
                "flex w-full items-center gap-2 px-3 py-2 text-left",
                "hover:bg-card focus-visible:outline-2 focus-visible:outline-ring",
              )}
            >
              {open ? (
                <ChevronDown className="h-3.5 w-3.5 shrink-0 text-text-tertiary" aria-hidden />
              ) : (
                <ChevronRight className="h-3.5 w-3.5 shrink-0 text-text-tertiary" aria-hidden />
              )}
              <StatusIcon status={cell.exit_status} />
              <span className="shrink-0 font-mono text-xs text-text-tertiary">
                [{cell.cell_index}]
              </span>
              <span className="shrink-0 text-xs text-text-secondary">
                {languageLabel(cell.language)}
              </span>
              {cell.origin !== "agent" ? (
                <Badge tone="secondary" className="shrink-0 normal-case">
                  <User className="mr-1 h-3 w-3" aria-hidden />
                  {cell.origin}
                </Badge>
              ) : null}
              {cell.description ? (
                <span className="min-w-0 flex-1 truncate text-xs text-text-primary">
                  {cell.description}
                </span>
              ) : (
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-text-primary">
                  {(cell.source ?? "").split("\n")[0]}
                </span>
              )}
              <span className="shrink-0 text-xs text-text-tertiary">
                {outcome || formatAge(cell.created_at)}
              </span>
            </button>

            {open ? (
              loadingId === cell.cell_id ? (
                <div className="flex items-center gap-2 border-t border-border px-3 py-3 text-xs text-text-secondary">
                  <Spinner /> Loading cell…
                </div>
              ) : errorId === cell.cell_id ? (
                <p className="border-t border-border px-3 py-3 text-xs text-destructive">
                  Could not load this cell. Refresh to try again.
                </p>
              ) : details[cell.cell_id] ? (
                <>
                  <CellBody cell={details[cell.cell_id]} />
                  {showPermalink ? (
                    <div className="border-t border-border px-3 py-2">
                      <Link
                        className="text-xs text-text-secondary underline hover:text-text-primary"
                        to={`/cells/${encodeURIComponent(cell.cell_id)}`}
                      >
                        Link to this cell
                      </Link>
                    </div>
                  ) : null}
                </>
              ) : null
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export default CellTimeline;
