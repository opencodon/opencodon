/**
 * FrameDetailPage — results first, then the trace, then the environment.
 *
 * The layout encodes the product's claim: what came out of this frame is the
 * headline; how it was produced sits directly underneath and is always one
 * click away, never hidden behind a menu.
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { CellTimeline } from "@/components/science/CellTimeline";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import type { CellSummary, FrameDetail } from "@/lib/api";
import {
  RUN_HEALTH_LABEL,
  RUN_HEALTH_TONE,
  formatAge,
  formatBytes,
  formatTimestamp,
  languageLabel,
  runHealth,
} from "@/lib/science-format";
import { cn } from "@/lib/utils";

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="flex items-baseline gap-2 text-xs text-display text-text-tertiary">
        {title}
        {count === undefined ? null : (
          <span className="tabular-nums text-text-tertiary">{count}</span>
        )}
      </h2>
      {children}
    </section>
  );
}

export default function FrameDetailPage() {
  const { frameId = "" } = useParams();
  const [frame, setFrame] = useState<FrameDetail | null>(null);
  const [cells, setCells] = useState<CellSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { setTitle, setEnd } = usePageHeader();

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([api.getFrame(frameId), api.getFrameCells(frameId)])
      .then(([detail, trace]) => {
        setFrame(detail);
        setCells(trace.cells);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [frameId]);

  useEffect(load, [load]);

  useEffect(() => {
    setTitle(frame?.title || "Frame");
    return () => setTitle(null);
  }, [frame?.title, setTitle]);

  useEffect(() => {
    setEnd(
      <Button ghost size="sm" onClick={load} aria-label="Refresh frame">
        <RefreshCw className="h-4 w-4" aria-hidden />
      </Button>,
    );
    return () => setEnd(null);
  }, [load, setEnd]);

  if (loading && !frame) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-text-secondary">
        <Spinner /> Loading frame…
      </div>
    );
  }

  if (error || !frame) {
    return (
      <div className="flex flex-col items-start gap-3 p-6">
        <p className="text-sm text-destructive">
          Could not load this frame{error ? `: ${error}` : "."}
        </p>
        <Link className="text-xs text-text-secondary underline" to="/frames">
          Back to frames
        </Link>
      </div>
    );
  }

  const health = runHealth(frame.cell_count, frame.failed_cell_count);

  return (
    <div className="flex flex-col gap-6 p-4">
      <Link
        to="/frames"
        className="flex w-fit items-center gap-1.5 text-xs text-text-secondary hover:text-text-primary"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        All frames
      </Link>

      {/* Context strip — the facts a reader needs to judge the result. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-secondary">
        <span className={cn("tabular-nums", RUN_HEALTH_TONE[health])}>
          {RUN_HEALTH_LABEL[health]}
        </span>
        <span className="tabular-nums">
          {frame.cell_count} cell{frame.cell_count === 1 ? "" : "s"}
        </span>
        {frame.model ? (
          <span className="font-mono text-text-tertiary">{frame.model}</span>
        ) : null}
        {frame.cwd ? (
          <span className="truncate font-mono text-text-tertiary" title={frame.cwd}>
            {frame.cwd}
          </span>
        ) : null}
        <span title={formatTimestamp(frame.started_at)}>
          started {formatAge(frame.started_at)}
        </span>
        <span className="font-mono text-text-tertiary">{frame.frame_id}</span>
      </div>

      <Section title="Results" count={frame.artifacts.length}>
        {frame.artifacts.length === 0 ? (
          <p className="text-xs text-text-secondary">
            Nothing was published from this frame. Workspace files are
            ephemeral — only staged artifacts persist.
          </p>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {frame.artifacts.map((artifact) => (
              <li key={artifact.artifact_id}>
                <Link
                  to={`/artifacts/${artifact.artifact_id}`}
                  className="flex h-full flex-col gap-1 rounded border border-border px-3 py-2 hover:bg-card focus-visible:outline-2 focus-visible:outline-ring"
                >
                  <span className="truncate font-mono text-xs text-text-primary">
                    {artifact.filename}
                  </span>
                  <span className="flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
                    <span>v{artifact.latest_version_number ?? 1}</span>
                    <span>{formatBytes(artifact.latest_size_bytes)}</span>
                    {artifact.is_user_upload ? (
                      <Badge tone="secondary" className="normal-case">
                        input
                      </Badge>
                    ) : null}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Trace" count={cells.length}>
        <CellTimeline cells={cells} />
      </Section>

      <Section title="Environments" count={frame.environments.length}>
        {frame.environments.length === 0 ? (
          <p className="text-xs text-text-secondary">
            No environment was recorded for this frame.
          </p>
        ) : (
          <ul className="flex flex-col gap-1">
            {frame.environments.map((env) => (
              <li
                key={`${env.language}:${env.env_name}`}
                className="flex items-center justify-between gap-3 rounded border border-border px-3 py-2 text-xs"
              >
                <span className="flex items-center gap-2">
                  <Badge tone="secondary" className="normal-case">
                    {languageLabel(env.language)}
                  </Badge>
                  <span className="font-mono text-text-primary">
                    {env.env_name ?? "default"}
                  </span>
                </span>
                <span className="tabular-nums text-text-tertiary">
                  {env.cell_count} cell{env.cell_count === 1 ? "" : "s"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}
