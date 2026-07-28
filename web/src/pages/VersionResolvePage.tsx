/**
 * VersionResolvePage — turns a bare version id into its artifact page.
 *
 * `{{artifact:VERSION_ID}}` markers name a *version*, but the artifact page
 * is keyed by artifact with the version as a query parameter. This route is
 * the redirect between the two, so a marker can be linked without the writer
 * knowing which artifact the version belongs to.
 */

import { useEffect, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";

export default function VersionResolvePage() {
  const { versionId = "" } = useParams();
  const [artifactId, setArtifactId] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getVersion(versionId)
      .then((version) => {
        if (!cancelled) setArtifactId(version.artifact_id);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [versionId]);

  if (artifactId) {
    return (
      <Navigate
        replace
        to={`/artifacts/${encodeURIComponent(artifactId)}?version=${encodeURIComponent(versionId)}`}
      />
    );
  }

  if (failed) {
    return (
      <div className="flex flex-col items-start gap-3 p-6">
        <p className="text-sm text-destructive">
          No artifact version with id <code className="font-mono">{versionId}</code>.
        </p>
        <p className="max-w-prose text-xs text-text-secondary">
          The reference may point at a version from another profile, or one
          removed by retention.
        </p>
        <Link className="text-xs text-text-secondary underline" to="/artifacts">
          Browse artifacts
        </Link>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 p-6 text-sm text-text-secondary">
      <Spinner /> Resolving reference…
    </div>
  );
}
