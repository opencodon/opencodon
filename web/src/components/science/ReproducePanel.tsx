/**
 * ReproducePanel — replay a version's producing cells and report the claim.
 *
 * The wording is the point. `reproduced` means the replayed bytes matched;
 * it does not mean the result is correct, and the panel never says
 * "verified". Every claim is shown with what it actually asserts, so a reader
 * cannot mistake a byte match for a scientific one.
 *
 * Reproduction is unavailable on a public bind — it re-runs recorded code, so
 * the server only opens it on a loopback dashboard. A 403 is a normal state
 * here, not an error to apologise for.
 */

import { useCallback, useEffect, useState } from "react";
import { RotateCw } from "lucide-react";

import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type { ReproductionReport } from "@/lib/api";
import {
  CLAIM_LABEL,
  CLAIM_MEANING,
  CLAIM_TONE,
  isReproduceClaim,
} from "@/lib/science-format";
import { cn } from "@/lib/utils";

const POLL_MS = 1500;

export function ClaimBadge({ claim }: { claim: string }) {
  if (!isReproduceClaim(claim)) {
    return <span className="text-xs text-text-tertiary">{claim}</span>;
  }
  return (
    <span className={cn("text-xs text-display", CLAIM_TONE[claim])}>
      {CLAIM_LABEL[claim]}
    </span>
  );
}

export function ReproducePanel({ versionId }: { versionId: string }) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<ReproductionReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  // A new version invalidates any previous verdict.
  useEffect(() => {
    setJobId(null);
    setReport(null);
    setError(null);
    setUnavailable(false);
    setRunning(false);
  }, [versionId]);

  // Poll while a job is outstanding. Keyed on the job id so switching
  // versions or unmounting cancels the chain instead of leaking timers.
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let handle = 0;

    const tick = () => {
      api
        .getReproduction(jobId)
        .then((job) => {
          if (cancelled) return;
          if (job.state === "running") {
            handle = window.setTimeout(tick, POLL_MS);
            return;
          }
          setReport(job.report);
          setRunning(false);
          setJobId(null);
        })
        .catch((err: Error) => {
          if (cancelled) return;
          setError(err.message);
          setRunning(false);
          setJobId(null);
        });
    };

    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [jobId]);

  const start = useCallback(() => {
    setRunning(true);
    setError(null);
    setReport(null);
    api
      .startReproduction(versionId)
      .then((job) => setJobId(job.job_id))
      .catch((err: Error) => {
        // The server refuses on a public bind; that is a state, not a fault.
        if (err.message.includes("403") || /local dashboard/i.test(err.message)) {
          setUnavailable(true);
        } else {
          setError(err.message);
        }
        setRunning(false);
      });
  }, [versionId]);

  return (
    <div className="flex flex-col gap-3">
      <p className="max-w-prose text-xs text-text-secondary">
        Re-runs the cells that produced this version in a fresh kernel and
        compares checksums. A match means the bytes were reproduced — not that
        the result is correct.
      </p>

      <div className="flex items-center gap-3">
        <Button size="sm" onClick={start} disabled={running}>
          {running ? (
            <Spinner />
          ) : (
            <RotateCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          )}
          {running ? "Replaying…" : "Reproduce"}
        </Button>
        {report ? <ClaimBadge claim={report.claim} /> : null}
      </div>

      {unavailable ? (
        <p className="max-w-prose text-xs text-text-secondary">
          Reproduction is available only on a local dashboard, because it
          re-runs recorded code on this machine. From a terminal:{" "}
          <code className="font-mono">reproduce_artifact</code>.
        </p>
      ) : null}

      {error ? (
        <p className="text-xs text-destructive">
          The replay could not be started: {error}
        </p>
      ) : null}

      {report ? (
        <div className="flex flex-col gap-2 rounded border border-border px-3 py-2.5">
          <p className="text-xs text-text-primary">
            {isReproduceClaim(report.claim)
              ? CLAIM_MEANING[report.claim]
              : report.claim}
          </p>
          {report.reason ? (
            <p className="text-xs text-text-secondary">{report.reason}</p>
          ) : null}
          {Array.isArray(report.caveats) && report.caveats.length > 0 ? (
            <ul className="flex flex-col gap-1">
              {report.caveats.map((caveat) => (
                <li key={caveat} className="text-xs text-warning">
                  {caveat}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default ReproducePanel;
