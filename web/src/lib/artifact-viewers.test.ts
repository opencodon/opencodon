import { afterEach, describe, expect, it } from "vitest";

import {
  extensionOf,
  registerArtifactViewer,
  resetArtifactViewers,
  resolveViewerKind,
} from "./artifact-viewers";

afterEach(resetArtifactViewers);

describe("extensionOf", () => {
  it("reads the last extension, lower-cased", () => {
    expect(extensionOf("results.CSV")).toBe("csv");
    expect(extensionOf("archive.tar.gz")).toBe("gz");
  });

  it("returns empty for names without one", () => {
    expect(extensionOf("Makefile")).toBe("");
    expect(extensionOf("trailing.")).toBe("");
    expect(extensionOf(null)).toBe("");
  });
});

describe("resolveViewerKind", () => {
  it("matches on content type", () => {
    expect(
      resolveViewerKind({ contentType: "text/markdown", filename: null }),
    ).toBe("markdown");
    expect(
      resolveViewerKind({ contentType: "application/json", filename: null }),
    ).toBe("json");
  });

  it("ignores content-type parameters", () => {
    expect(
      resolveViewerKind({
        contentType: "text/csv; charset=utf-8",
        filename: null,
      }),
    ).toBe("table");
  });

  it("falls back to the filename extension", () => {
    expect(
      resolveViewerKind({
        contentType: "application/octet-stream",
        filename: "notes.md",
      }),
    ).toBe("markdown");
  });

  it("treats unknown text as plain text", () => {
    expect(
      resolveViewerKind({ contentType: "text/x-python", filename: "run.py" }),
    ).toBe("text");
  });

  it("reports binary payloads as binary", () => {
    expect(
      resolveViewerKind({
        contentType: "application/octet-stream",
        filename: "model.bin",
        binary: true,
      }),
    ).toBe("binary");
  });

  it("still renders images when the bytes are not text", () => {
    expect(
      resolveViewerKind({
        contentType: "image/png",
        filename: "figure.png",
        binary: true,
      }),
    ).toBe("image");
  });

  it("lets a plugin outrank a built-in for the same type", () => {
    registerArtifactViewer({
      id: "plugin:chart",
      kind: "json",
      contentTypes: ["text/csv"],
    });
    expect(
      resolveViewerKind({ contentType: "text/csv", filename: "d.csv" }),
    ).toBe("json");
    resetArtifactViewers();
    expect(
      resolveViewerKind({ contentType: "text/csv", filename: "d.csv" }),
    ).toBe("table");
  });

  it("replaces a registration with the same id rather than stacking", () => {
    registerArtifactViewer({
      id: "plugin:x",
      kind: "json",
      extensions: ["dat"],
    });
    registerArtifactViewer({
      id: "plugin:x",
      kind: "table",
      extensions: ["dat"],
    });
    expect(
      resolveViewerKind({ contentType: null, filename: "series.dat" }),
    ).toBe("table");
  });
});
