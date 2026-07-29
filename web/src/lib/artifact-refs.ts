/**
 * `{{artifact:VERSION_ID}}` — one identifier that works everywhere.
 *
 * The same marker resolves to a file path inside the kernel, an inline image
 * in a rendered document, and a link in the dashboard. That is why bare
 * filenames are discouraged: a filename is ambiguous across versions and
 * frames, a version id is not.
 *
 * This module only parses and rewrites; resolution to a URL is the caller's,
 * so the same parser serves previews, exports, and tests.
 */

/** Matches `{{artifact:<id>}}`, tolerating whitespace inside the braces. */
const ARTIFACT_REF = /\{\{\s*artifact:([A-Za-z0-9_-]+)\s*\}\}/g;

export interface ArtifactRef {
  /** The full matched marker, e.g. "{{artifact:v_12}}". */
  marker: string;
  versionId: string;
  index: number;
}

/** Every reference in `text`, in order, with duplicates preserved. */
export function findArtifactRefs(text: string): ArtifactRef[] {
  const refs: ArtifactRef[] = [];
  for (const match of text.matchAll(ARTIFACT_REF)) {
    refs.push({
      marker: match[0],
      versionId: match[1],
      index: match.index ?? 0,
    });
  }
  return refs;
}

/**
 * Replace every reference using `resolve`.
 *
 * A resolver returning null leaves the marker untouched — an unresolvable
 * reference should stay visible as itself rather than silently vanishing or
 * turning into a broken link.
 */
export function replaceArtifactRefs(
  text: string,
  resolve: (versionId: string) => string | null,
): string {
  return text.replace(ARTIFACT_REF, (marker, versionId: string) => {
    const replacement = resolve(versionId);
    return replacement === null ? marker : replacement;
  });
}

/** Split `text` into literal segments and refs, for React rendering. */
export type RefSegment =
  | { type: "text"; value: string }
  | { type: "ref"; versionId: string; marker: string };

export function splitOnArtifactRefs(text: string): RefSegment[] {
  const segments: RefSegment[] = [];
  let cursor = 0;
  for (const ref of findArtifactRefs(text)) {
    if (ref.index > cursor) {
      segments.push({ type: "text", value: text.slice(cursor, ref.index) });
    }
    segments.push({
      type: "ref",
      versionId: ref.versionId,
      marker: ref.marker,
    });
    cursor = ref.index + ref.marker.length;
  }
  if (cursor < text.length) {
    segments.push({ type: "text", value: text.slice(cursor) });
  }
  return segments;
}
