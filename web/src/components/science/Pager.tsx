/**
 * Pager — offset/limit paging for the science indexes.
 *
 * States the window explicitly ("1–50 of 214") rather than showing bare
 * arrows: a reader looking at a truncated list of results needs to know it is
 * truncated, and by how much.
 */

import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@nous-research/ui/ui/components/button";

export function Pager({
  total,
  limit,
  offset,
  onOffsetChange,
  noun,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
  /** Plural noun for the summary line, e.g. "frames". */
  noun: string;
}) {
  if (total <= limit) return null;

  const first = offset + 1;
  const last = Math.min(offset + limit, total);
  const atStart = offset === 0;
  const atEnd = last >= total;

  return (
    <nav
      className="flex items-center justify-between gap-3"
      aria-label={`${noun} pagination`}
    >
      <span className="tabular-nums text-xs text-text-secondary">
        {first}–{last} of {total} {noun}
      </span>
      <span className="flex items-center gap-1.5">
        <Button
          ghost
          size="sm"
          disabled={atStart}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
          aria-label={`Previous ${noun}`}
        >
          <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
        </Button>
        <Button
          ghost
          size="sm"
          disabled={atEnd}
          onClick={() => onOffsetChange(offset + limit)}
          aria-label={`Next ${noun}`}
        >
          <ChevronRight className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </span>
    </nav>
  );
}

export default Pager;
