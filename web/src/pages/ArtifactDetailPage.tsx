/**
 * ArtifactDetailPage — one artifact, one selected version, four views.
 *
 * Preview / Provenance / Lineage / Versions. Provenance shows the *recorded*
 * cell, not a reconstruction of it: the source, streams, and host calls that
 * were logged when the version was staged. Nothing on this page is inferred.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Download } from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Tabs, TabsList, TabsTrigger } from "@nous-research/ui/ui/components/tabs";
import { CellTimeline } from "@/components/science/CellTimeline";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import type {
  ArtifactDetail,
  LineageDirection,
  LineageEntry,
  VersionContent,
  VersionDetail,
  VersionSummary,
} from "@/lib/api";
import {
  formatAge,
  formatBytes,
  formatTimestamp,
  shortHash,
} from "@/lib/science-format";

const TABS = ["preview", "provenance", "lineage", "versions"] as const;
type TabKey = (typeof TABS)[number];

const TAB_LABEL: Record<TabKey, string> = {
  preview: "Preview",
  provenance: "Provenance",
  lineage: "Lineage",
  versions: "Versions",
};

function PreviewPane({ versionId }: { versionId: string }) {
  const [content, setContent] = useState<VersionContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .getVersionContent(versionId)
      .then(setContent)
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [versionId]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-text-secondary">
        <Spinner /> Loading content…
      </div>
    );
  }
  if (error || !content) {
    return (
      <p className="text-xs text-destructive">
        Could not read this version{error ? `: ${error}` : "."}
      </p>
    );
  }
  if (content.binary) {
    return (
      <div className="flex flex-col items-start gap-2">
        <p className="text-xs text-text-secondary">
          {content.content_type ?? "Binary"} · {formatBytes(content.size_bytes)}{" "}
          — not previewable as text.
        </p>
        <a
          className="text-xs text-text-primary underline"
          href={api.versionDownloadUrl(versionId)}
        >
          Download the file
        </a>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {content.truncated ? (
        <p className="text-xs text-warning">
          Showing the first part of this file. Download it for the whole
          content.
        </p>
      ) : null}
      <pre className="overflow-x-auto rounded border border-border bg-card p-3 font-mono text-xs text-text-primary">
        {content.text}
      </pre>
    </div>
  );
}

function LineagePane({ versionId }: { versionId: string }) {
  const [direction, setDirection] = useState<LineageDirection>("upstream");
  const [entries, setEntries] = useState<LineageEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .getVersionLineage(versionId, direction)
      .then((resp) => setEntries(resp.lineage))
      .catch(() => setEntries([]))
      .finally(() => setLoading(false));
  }, [direction, versionId]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        {(["upstream", "downstream"] as const).map((value) => (
          <Button
            key={value}
            size="sm"
            ghost={direction !== value}
            aria-pressed={direction === value}
            onClick={() => setDirection(value)}
          >
            {value}
          </Button>
        ))}
        <span className="text-xs text-text-tertiary">
          {direction === "upstream"
            ? "what this was derived from"
            : "what was derived from this"}
        </span>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-xs text-text-secondary">
          <Spinner /> Walking the graph…
        </div>
      ) : entries.length === 0 ? (
        <p className="text-xs text-text-secondary">
          {direction === "upstream"
            ? "No recorded inputs — this version was produced from scratch."
            : "Nothing has been derived from this version."}
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {entries.map((entry) => (
            <li
              key={entry.version_id}
              style={{ marginLeft: `${(entry.depth - 1) * 16}px` }}
            >
              <Link
                to={`/artifacts/${entry.artifact_id}?version=${entry.version_id}`}
                className="flex items-center justify-between gap-3 rounded border border-border px-3 py-2 text-xs hover:bg-card"
              >
                <span className="flex items-center gap-2">
                  <span className="text-text-tertiary">depth {entry.depth}</span>
                  <span className="font-mono text-text-primary">
                    {entry.filename ?? entry.artifact_id}
                  </span>
                  <span className="tabular-nums text-text-tertiary">
                    v{entry.version_number}
                  </span>
                </span>
                <span className="font-mono text-text-tertiary">
                  {shortHash(entry.sha256)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function VersionsPane({
  versions,
  activeId,
  onSelect,
}: {
  versions: VersionSummary[];
  activeId: string;
  onSelect: (versionId: string) => void;
}) {
  return (
    <ul className="flex flex-col gap-1">
      {[...versions].reverse().map((version) => (
        <li key={version.version_id}>
          <button
            type="button"
            onClick={() => onSelect(version.version_id)}
            aria-current={version.version_id === activeId}
            className={
              "flex w-full items-center justify-between gap-3 rounded border px-3 py-2 text-left text-xs hover:bg-card " +
              (version.version_id === activeId
                ? "border-midground/40"
                : "border-border")
            }
          >
            <span className="flex items-center gap-3">
              <span className="tabular-nums text-text-primary">
                v{version.version_number}
              </span>
              <span className="tabular-nums text-text-tertiary">
                {formatBytes(version.size_bytes)}
              </span>
              <span className="font-mono text-text-tertiary">
                {shortHash(version.sha256)}
              </span>
              {version.is_intermediate ? (
                <Badge tone="secondary" className="normal-case">
                  intermediate
                </Badge>
              ) : null}
            </span>
            <span
              className="text-text-tertiary"
              title={formatTimestamp(version.created_at)}
            >
              {formatAge(version.created_at)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function ArtifactDetailPage() {
  const { artifactId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const [artifact, setArtifact] = useState<ArtifactDetail | null>(null);
  const [version, setVersion] = useState<VersionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { setTitle } = usePageHeader();

  const requestedVersion = params.get("version");

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .getArtifact(artifactId)
      .then(setArtifact)
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [artifactId]);

  const activeVersionId = useMemo(() => {
    if (requestedVersion) return requestedVersion;
    return artifact?.latest_version_id ?? null;
  }, [artifact?.latest_version_id, requestedVersion]);

  useEffect(() => {
    if (!activeVersionId) return;
    api
      .getVersion(activeVersionId)
      .then(setVersion)
      .catch(() => setVersion(null));
  }, [activeVersionId]);

  useEffect(() => {
    setTitle(artifact?.filename || "Artifact");
    return () => setTitle(null);
  }, [artifact?.filename, setTitle]);

  const selectVersion = useCallback(
    (versionId: string) => {
      const next = new URLSearchParams(params);
      next.set("version", versionId);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  if (loading && !artifact) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-text-secondary">
        <Spinner /> Loading artifact…
      </div>
    );
  }

  if (error || !artifact || !activeVersionId) {
    return (
      <div className="flex flex-col items-start gap-3 p-6">
        <p className="text-sm text-destructive">
          Could not load this artifact{error ? `: ${error}` : "."}
        </p>
        <Link className="text-xs text-text-secondary underline" to="/artifacts">
          Back to artifacts
        </Link>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link
          to="/artifacts"
          className="flex w-fit items-center gap-1.5 text-xs text-text-secondary hover:text-text-primary"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All artifacts
        </Link>
        {/* A real anchor, not a Button — the browser's own download path
            keeps the auth cookie and avoids a fetch/blob round trip. */}
        <a
          href={api.versionDownloadUrl(activeVersionId)}
          className="flex items-center gap-1.5 rounded border border-border px-2.5 py-1.5 text-xs text-text-primary hover:bg-card focus-visible:outline-2 focus-visible:outline-ring"
        >
          <Download className="h-3.5 w-3.5" aria-hidden />
          Download
        </a>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-secondary">
        <span className="tabular-nums">
          v{version?.version_number ?? artifact.latest_version_number ?? 1} of{" "}
          {artifact.versions.length}
        </span>
        <span className="tabular-nums">
          {formatBytes(version?.size_bytes ?? artifact.latest_size_bytes)}
        </span>
        <span className="font-mono text-text-tertiary">
          sha256 {shortHash(version?.sha256 ?? artifact.latest_sha256)}
        </span>
        <span>{version?.content_type ?? artifact.latest_content_type}</span>
        {artifact.frame_id ? (
          <Link
            className="underline hover:text-text-primary"
            to={`/frames/${encodeURIComponent(artifact.frame_id)}`}
          >
            in frame
          </Link>
        ) : null}
      </div>

      <Tabs defaultValue="preview">
        {(active, setActive) => (
          <>
            <TabsList>
              {TABS.map((tab) => (
                <TabsTrigger
                  key={tab}
                  value={tab}
                  active={active === tab}
                  onClick={() => setActive(tab)}
                >
                  {TAB_LABEL[tab]}
                </TabsTrigger>
              ))}
            </TabsList>

            {active === "preview" ? (
              <PreviewPane versionId={activeVersionId} />
            ) : null}

            {active === "provenance" ? (
              version?.producing_cell ? (
                <div className="flex flex-col gap-2">
                  <p className="text-xs text-text-secondary">
                    The recorded cell that staged this version — source,
                    streams, and host calls as logged. Not a reconstruction.
                  </p>
                  <CellTimeline cells={[version.producing_cell]} />
                </div>
              ) : (
                <p className="text-xs text-text-secondary">
                  No producing cell was recorded for this version — it was
                  uploaded rather than computed.
                </p>
              )
            ) : null}

            {active === "lineage" ? (
              <LineagePane versionId={activeVersionId} />
            ) : null}

            {active === "versions" ? (
              <VersionsPane
                versions={artifact.versions}
                activeId={activeVersionId}
                onSelect={selectVersion}
              />
            ) : null}
          </>
        )}
      </Tabs>
    </div>
  );
}
