/**
 * CellDetailPage — one cell at its own URL.
 *
 * Exists so a cell is citable: "the figure came from this cell" should be a
 * link someone can paste into a methods section, not a scroll position inside
 * a frame. Reuses CellTimeline so the rendering matches everywhere else.
 */

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { CellTimeline } from "@/components/science/CellTimeline";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import type { CellDetail } from "@/lib/api";
import {
  formatTimestamp,
  languageLabel,
} from "@/lib/science-format";

export default function CellDetailPage() {
  const { cellId = "" } = useParams();
  const [cell, setCell] = useState<CellDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { setTitle } = usePageHeader();

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .getCell(cellId)
      .then(setCell)
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [cellId]);

  useEffect(() => {
    setTitle(cell ? `Cell [${cell.cell_index}]` : "Cell");
    return () => setTitle(null);
  }, [cell, setTitle]);

  if (loading && !cell) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-text-secondary">
        <Spinner /> Loading cell…
      </div>
    );
  }

  if (error || !cell) {
    return (
      <div className="flex flex-col items-start gap-3 p-6">
        <p className="text-sm text-destructive">
          Could not load this cell{error ? `: ${error}` : "."}
        </p>
        <Link className="text-xs text-text-secondary underline" to="/frames">
          Back to frames
        </Link>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <Link
        to={`/frames/${encodeURIComponent(cell.session_id)}`}
        className="flex w-fit items-center gap-1.5 text-xs text-text-secondary hover:text-text-primary"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
        Back to the frame
      </Link>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-secondary">
        <Badge tone="secondary" className="normal-case">
          {languageLabel(cell.language)}
        </Badge>
        <span className="tabular-nums">index {cell.cell_index}</span>
        <span>{cell.exit_status}</span>
        {cell.env_name ? (
          <span className="font-mono text-text-tertiary">{cell.env_name}</span>
        ) : null}
        <span>{formatTimestamp(cell.created_at)}</span>
        <span className="font-mono text-text-tertiary">{cell.cell_id}</span>
      </div>

      <CellTimeline
        cells={[cell]}
        defaultOpenId={cell.cell_id}
        showPermalink={false}
      />
    </div>
  );
}
