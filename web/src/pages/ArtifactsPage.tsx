/**
 * ArtifactsPage — the durable plane, across every frame.
 *
 * Search hits the same index the agent's ``list_artifacts`` reads, so a
 * filename resolves identically for the reader and for the agent.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Package, RefreshCw, Search } from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import type { ArtifactSummary } from "@/lib/api";
import { formatAge, formatBytes } from "@/lib/science-format";

export default function ArtifactsPage() {
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { setEnd } = usePageHeader();

  const load = useCallback((term: string) => {
    setLoading(true);
    setError(null);
    api
      .getArtifacts({ search: term || undefined })
      .then((resp) => {
        setArtifacts(resp.artifacts);
        setTotal(resp.total);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  // Debounced so typing doesn't fire a request per keystroke.
  useEffect(() => {
    const handle = setTimeout(() => load(search), 200);
    return () => clearTimeout(handle);
  }, [load, search]);

  useEffect(() => {
    setEnd(
      <Button
        ghost
        size="sm"
        onClick={() => load(search)}
        aria-label="Refresh artifacts"
      >
        <RefreshCw className="h-4 w-4" aria-hidden />
      </Button>,
    );
    return () => setEnd(null);
  }, [load, search, setEnd]);

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center gap-3">
        <div className="relative min-w-0 flex-1 sm:max-w-sm">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text-tertiary"
            aria-hidden
          />
          <Input
            className="pl-7"
            placeholder="Search artifacts…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search artifacts"
          />
        </div>
        <span className="shrink-0 tabular-nums text-xs text-text-tertiary">
          {total} artifact{total === 1 ? "" : "s"}
        </span>
      </div>

      {error ? (
        <p className="text-sm text-destructive">
          Could not load artifacts: {error}
        </p>
      ) : loading && artifacts.length === 0 ? (
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <Spinner /> Loading artifacts…
        </div>
      ) : artifacts.length === 0 ? (
        <div className="flex flex-col items-start gap-2">
          <Package className="h-6 w-6 text-text-tertiary" aria-hidden />
          <p className="text-sm text-text-primary">
            {search ? "No artifacts match that name." : "No artifacts yet."}
          </p>
          {!search ? (
            <p className="max-w-prose text-xs text-text-secondary">
              Artifacts appear when a cell stages a file. Files left in the
              workspace stay ephemeral and never reach this list.
            </p>
          ) : null}
        </div>
      ) : (
        <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {artifacts.map((artifact) => (
            <li key={artifact.artifact_id}>
              <Link
                to={`/artifacts/${artifact.artifact_id}`}
                className="flex h-full flex-col gap-1.5 rounded border border-border px-3 py-2.5 hover:bg-card focus-visible:outline-2 focus-visible:outline-ring"
              >
                <span className="truncate font-mono text-xs text-text-primary">
                  {artifact.filename}
                </span>
                <span className="flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
                  <span className="tabular-nums">
                    v{artifact.latest_version_number ?? 1}
                  </span>
                  <span className="tabular-nums">
                    {formatBytes(artifact.latest_size_bytes)}
                  </span>
                  <span>{formatAge(artifact.created_at)}</span>
                  {artifact.is_user_upload ? (
                    <Badge tone="secondary" className="normal-case">
                      input
                    </Badge>
                  ) : null}
                </span>
                <span className="truncate text-xs text-text-tertiary">
                  {artifact.latest_content_type ?? "unknown type"}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
