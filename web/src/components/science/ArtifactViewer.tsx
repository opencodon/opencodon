/**
 * ArtifactViewer — renders a version's bytes through the resolved viewer.
 *
 * Images stream from the download URL rather than the JSON preview, so a
 * figure never round-trips through base64. Everything else renders from the
 * decoded text the content endpoint already returned.
 */

import { useMemo } from "react";
import { Link } from "react-router-dom";

import { Markdown } from "@/components/Markdown";
import { api } from "@/lib/api";
import type { VersionContent } from "@/lib/api";
import { splitOnArtifactRefs } from "@/lib/artifact-refs";
import { resolveViewerKind } from "@/lib/artifact-viewers";
import { formatBytes } from "@/lib/science-format";

/** Renders `{{artifact:…}}` markers as links to the referenced version. */
function WithArtifactRefs({ text }: { text: string }) {
  const segments = useMemo(() => splitOnArtifactRefs(text), [text]);
  if (segments.length === 1 && segments[0].type === "text") {
    return <>{text}</>;
  }
  return (
    <>
      {segments.map((segment, i) =>
        segment.type === "text" ? (
          <span key={i}>{segment.value}</span>
        ) : (
          <Link
            key={i}
            className="underline"
            to={`/artifacts/resolve/${encodeURIComponent(segment.versionId)}`}
          >
            {segment.marker}
          </Link>
        ),
      )}
    </>
  );
}

/** Minimal delimited-table renderer — no parser dependency, no guessing. */
function TableView({ text, delimiter }: { text: string; delimiter: string }) {
  const rows = useMemo(
    () =>
      text
        .trimEnd()
        .split("\n")
        .slice(0, 500)
        .map((line) => line.split(delimiter)),
    [delimiter, text],
  );
  if (rows.length === 0) return null;
  const [header, ...body] = rows;
  return (
    <div className="overflow-x-auto rounded border border-border">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr>
            {header.map((cell, i) => (
              <th
                key={i}
                className="border-b border-border px-2 py-1.5 text-left text-display text-text-tertiary"
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, r) => (
            <tr key={r}>
              {row.map((cell, c) => (
                <td
                  key={c}
                  className="border-b border-border px-2 py-1 font-mono tabular-nums text-text-secondary"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ArtifactViewer({
  versionId,
  filename,
  content,
}: {
  versionId: string;
  filename: string | null;
  content: VersionContent;
}) {
  const kind = resolveViewerKind({
    contentType: content.content_type,
    filename,
    binary: content.binary,
  });

  if (kind === "image") {
    return (
      <img
        src={api.versionDownloadUrl(versionId)}
        alt={filename ?? "artifact"}
        className="max-w-full rounded border border-border"
      />
    );
  }

  if (kind === "binary" || content.text === null) {
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

  const body =
    kind === "markdown" ? (
      <Markdown content={content.text} />
    ) : kind === "table" ? (
      <TableView
        text={content.text}
        delimiter={filename?.toLowerCase().endsWith(".tsv") ? "\t" : ","}
      />
    ) : (
      <pre className="overflow-x-auto rounded border border-border bg-card p-3 font-mono text-xs text-text-primary">
        <WithArtifactRefs text={content.text} />
      </pre>
    );

  return (
    <div className="flex flex-col gap-2">
      {content.truncated ? (
        <p className="text-xs text-warning">
          Showing the first part of this file. Download it for the whole
          content.
        </p>
      ) : null}
      {body}
    </div>
  );
}

export default ArtifactViewer;
