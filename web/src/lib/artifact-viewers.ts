/**
 * Viewer registry — which renderer handles an artifact version.
 *
 * Keyed on content type first, filename extension second, because the store
 * records whatever content type the producing cell declared and that is
 * frequently `application/octet-stream` for a file the reader would still
 * like to see.
 *
 * Built-ins cover the generic cases (text, markdown, json, tabular, image).
 * Domain viewers — structures, sequences, spectra — belong in dashboard
 * plugins rather than here: they carry heavy dependencies and their audiences
 * are disjoint. `registerArtifactViewer` is the seam a plugin bundle calls.
 */

export type ViewerKind =
  | "markdown"
  | "table"
  | "json"
  | "image"
  | "text"
  | "binary";

export interface ViewerRegistration {
  /** Stable id — a plugin re-registering the same id replaces its entry. */
  id: string;
  kind: ViewerKind;
  /** Lower-cased content types this viewer claims. */
  contentTypes?: string[];
  /** Lower-cased extensions, without the dot. */
  extensions?: string[];
}

const BUILTIN: ViewerRegistration[] = [
  {
    id: "builtin:markdown",
    kind: "markdown",
    contentTypes: ["text/markdown", "text/x-markdown"],
    extensions: ["md", "markdown"],
  },
  {
    id: "builtin:table",
    kind: "table",
    contentTypes: ["text/csv", "text/tab-separated-values"],
    extensions: ["csv", "tsv"],
  },
  {
    id: "builtin:json",
    kind: "json",
    contentTypes: ["application/json", "application/x-ndjson"],
    extensions: ["json", "jsonl", "ndjson"],
  },
  {
    id: "builtin:image",
    kind: "image",
    contentTypes: [
      "image/png",
      "image/jpeg",
      "image/gif",
      "image/webp",
      "image/svg+xml",
    ],
    extensions: ["png", "jpg", "jpeg", "gif", "webp", "svg"],
  },
];

const registry: ViewerRegistration[] = [...BUILTIN];

/**
 * Register a viewer, or replace one with the same id.
 *
 * Plugins are consulted before built-ins, so a plugin may claim a type the
 * dashboard already handles generically — a CSV plugin that draws a chart
 * should win over the plain table.
 */
export function registerArtifactViewer(entry: ViewerRegistration): void {
  const existing = registry.findIndex((v) => v.id === entry.id);
  if (existing >= 0) registry.splice(existing, 1);
  registry.unshift(entry);
}

/** Test seam: drop plugin registrations and restore the built-in set. */
export function resetArtifactViewers(): void {
  registry.length = 0;
  registry.push(...BUILTIN);
}

export function extensionOf(filename: string | null | undefined): string {
  if (!filename) return "";
  const dot = filename.lastIndexOf(".");
  if (dot < 0 || dot === filename.length - 1) return "";
  return filename.slice(dot + 1).toLowerCase();
}

/**
 * Pick a viewer for a version.
 *
 * `binary` wins outright — the content endpoint has already told us the bytes
 * are not valid UTF-8, and a viewer that assumes text would render mojibake.
 * An image is the exception: it renders from its own URL, not from decoded
 * text, so it survives the binary flag.
 */
export function resolveViewerKind({
  contentType,
  filename,
  binary = false,
}: {
  contentType: string | null | undefined;
  filename: string | null | undefined;
  binary?: boolean;
}): ViewerKind {
  const type = (contentType ?? "").toLowerCase().split(";")[0].trim();
  const ext = extensionOf(filename);

  const match = registry.find(
    (viewer) =>
      (type && viewer.contentTypes?.includes(type)) ||
      (ext && viewer.extensions?.includes(ext)),
  );

  if (match?.kind === "image") return "image";
  if (binary) return "binary";
  if (match) return match.kind;
  if (type.startsWith("text/")) return "text";
  return "text";
}
