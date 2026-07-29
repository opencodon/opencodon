/** Shared formatting for the science surfaces (frames, cells, artifacts). */

/** Bytes as a compact human string: 812 B, 14.8 KB, 1.2 MB. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/** Epoch seconds as "2h ago" / "3d ago"; absolute once past a week. */
export function formatAge(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  const seconds = Date.now() / 1000 - epochSeconds;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString();
}

/** Full local timestamp for hover titles and detail rows. */
export function formatTimestamp(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

/** First 12 characters of a checksum — enough to recognise, short enough to scan. */
export function shortHash(sha256: string | null | undefined): string {
  if (!sha256) return "—";
  return sha256.slice(0, 12);
}

/**
 * A frame's reproducibility posture at a glance.
 *
 * Deliberately *not* called a reproduce claim: nothing here has been
 * replayed. It reports whether the recorded run completed cleanly, which is
 * a precondition for reproduction, not evidence of it. The real claim
 * vocabulary (reproduced / diverged / failed / indeterminate / ineligible)
 * arrives with the reproduce action in Phase 4.
 */
export type RunHealth = "clean" | "partial" | "failed" | "empty";

export function runHealth(cellCount: number, failedCount: number): RunHealth {
  if (cellCount === 0) return "empty";
  if (failedCount === 0) return "clean";
  if (failedCount === cellCount) return "failed";
  return "partial";
}

/** i18n keys rather than literals — the UI never ships bare English. */
export const RUN_HEALTH_LABEL_KEY: Record<RunHealth, "clean" | "failed" | "partial" | "unknown"> = {
  clean: "clean",
  partial: "partial",
  failed: "failed",
  empty: "unknown",
};

/** Maps to DS semantic tokens — kept separate from the theme accent. */
export const RUN_HEALTH_TONE: Record<RunHealth, string> = {
  clean: "text-success",
  partial: "text-warning",
  failed: "text-destructive",
  empty: "text-(--ui-text-tertiary)",
};

/** Language label for a cell or environment row. */
export function languageLabel(language: string | null | undefined): string {
  if (!language) return "unknown";
  return language === "r" ? "R" : language;
}

/**
 * The six claims `reproduce()` can return, strongest first.
 *
 * The ladder is the point, and `science/reproduce.py` earns each rung rather
 * than asserting it. `verified` requires the bytes to match *and* the
 * producing cell's environment lock to still match, so someone else could
 * recreate the result. A byte match without that lock is only `reproduced`:
 * the replay agreed here, but nobody can promise the same environment
 * elsewhere. The UI must preserve that distinction and never round one up
 * into the other.
 */
export type ReproduceClaim =
  | "verified"
  | "reproduced"
  | "diverged"
  | "failed"
  | "indeterminate"
  | "ineligible";

export const CLAIM_LABEL: Record<ReproduceClaim, string> = {
  verified: "Verified",
  reproduced: "Reproduced",
  diverged: "Diverged",
  failed: "Replay failed",
  indeterminate: "Indeterminate",
  ineligible: "Not reproducible",
};

/** What each claim actually asserts, in the reader's terms. */
export const CLAIM_MEANING: Record<ReproduceClaim, string> = {
  verified:
    "Identical bytes, and the recorded environment lock still matches — " +
    "this result can be recreated elsewhere.",
  reproduced:
    "Identical bytes, but the environment was an observation rather than a " +
    "recreatable lock, so the match holds here and not necessarily elsewhere.",
  diverged: "The replay succeeded but produced different bytes.",
  failed: "The replay could not be completed.",
  indeterminate: "The replay ran but the result could not be compared.",
  ineligible: "This version has no producing cell to replay.",
};

export const CLAIM_TONE: Record<ReproduceClaim, string> = {
  verified: "text-success",
  reproduced: "text-success",
  diverged: "text-warning",
  failed: "text-destructive",
  indeterminate: "text-text-secondary",
  ineligible: "text-text-tertiary",
};

export function isReproduceClaim(value: unknown): value is ReproduceClaim {
  return (
    typeof value === "string" && value in CLAIM_LABEL
  );
}
