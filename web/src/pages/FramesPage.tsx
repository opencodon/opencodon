/**
 * FramesPage — the landing surface.
 *
 * A frame is a root session plus its compression-chain descendants: the unit
 * of scientific work. Rows lead with what came out of the frame (artifacts)
 * and how the run went, not with the conversation.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FlaskConical, Package, RefreshCw } from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type { FrameSummary } from "@/lib/api";
import { usePageHeader } from "@/contexts/usePageHeader";
import {
  RUN_HEALTH_LABEL,
  RUN_HEALTH_TONE,
  formatAge,
  languageLabel,
  runHealth,
} from "@/lib/science-format";
import { cn } from "@/lib/utils";

function FrameRow({ frame }: { frame: FrameSummary }) {
  const health = runHealth(frame.cell_count, frame.failed_cell_count);
  return (
    <li>
      <Link
        to={`/frames/${encodeURIComponent(frame.frame_id)}`}
        className="flex flex-col gap-2 rounded border border-border px-4 py-3 hover:bg-card focus-visible:outline-2 focus-visible:outline-ring"
      >
        <div className="flex items-baseline justify-between gap-4">
          <span className="min-w-0 truncate text-sm text-text-primary">
            {frame.title || frame.frame_id}
          </span>
          <span className="shrink-0 text-xs text-text-tertiary">
            {formatAge(frame.last_cell_at ?? frame.started_at)}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-secondary">
          <span className="flex items-center gap-1.5">
            <Package className="h-3.5 w-3.5" aria-hidden />
            {frame.artifact_count} artifact{frame.artifact_count === 1 ? "" : "s"}
          </span>
          <span>
            {frame.cell_count} cell{frame.cell_count === 1 ? "" : "s"}
          </span>
          <span className={cn("tabular-nums", RUN_HEALTH_TONE[health])}>
            {RUN_HEALTH_LABEL[health]}
          </span>
          {frame.languages.map((language) => (
            <Badge key={language} tone="secondary" className="normal-case">
              {languageLabel(language)}
            </Badge>
          ))}
          {frame.model ? (
            <span className="font-mono text-text-tertiary">{frame.model}</span>
          ) : null}
          {frame.session_missing ? (
            <span
              className="text-text-tertiary"
              title="The session was deleted; its execution record survives."
            >
              session pruned
            </span>
          ) : null}
        </div>
      </Link>
    </li>
  );
}

export default function FramesPage() {
  const [frames, setFrames] = useState<FrameSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { setEnd } = usePageHeader();

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getFrames()
      .then((resp) => setFrames(resp.frames))
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    setEnd(
      <Button ghost size="sm" onClick={load} aria-label="Refresh frames">
        <RefreshCw className="h-4 w-4" aria-hidden />
      </Button>,
    );
    return () => setEnd(null);
  }, [load, setEnd]);

  if (loading && frames.length === 0) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-text-secondary">
        <Spinner /> Loading frames…
      </div>
    );
  }

  if (error) {
    return (
      <p className="p-6 text-sm text-destructive">
        Could not load frames: {error}
      </p>
    );
  }

  if (frames.length === 0) {
    return (
      <div className="flex flex-col items-start gap-2 p-6">
        <FlaskConical className="h-6 w-6 text-text-tertiary" aria-hidden />
        <p className="text-sm text-text-primary">No frames yet.</p>
        <p className="max-w-prose text-xs text-text-secondary">
          A frame appears here once a session runs code or publishes an
          artifact. Start one from the terminal with{" "}
          <code className="font-mono">opencodon</code>.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 p-4">
      <ul className="flex flex-col gap-2">
        {frames.map((frame) => (
          <FrameRow key={frame.frame_id} frame={frame} />
        ))}
      </ul>
    </div>
  );
}
