import { describe, expect, it } from "vitest";

import {
  findArtifactRefs,
  replaceArtifactRefs,
  splitOnArtifactRefs,
} from "./artifact-refs";

describe("findArtifactRefs", () => {
  it("finds every marker in order, duplicates included", () => {
    const text = "see {{artifact:v1}} and {{artifact:v2}} and {{artifact:v1}}";
    expect(findArtifactRefs(text).map((r) => r.versionId)).toEqual([
      "v1",
      "v2",
      "v1",
    ]);
  });

  it("tolerates inner whitespace", () => {
    expect(findArtifactRefs("{{ artifact:v9 }}")[0].versionId).toBe("v9");
  });

  it("ignores malformed markers", () => {
    expect(findArtifactRefs("{artifact:v1} {{artifact}} {{artifact:}}")).toEqual(
      [],
    );
  });
});

describe("replaceArtifactRefs", () => {
  it("substitutes resolved references", () => {
    const out = replaceArtifactRefs("fig {{artifact:v1}}", (id) => `/a/${id}`);
    expect(out).toBe("fig /a/v1");
  });

  it("leaves unresolvable references visible", () => {
    const out = replaceArtifactRefs("fig {{artifact:v1}}", () => null);
    expect(out).toBe("fig {{artifact:v1}}");
  });
});

describe("splitOnArtifactRefs", () => {
  it("splits into literal and reference segments", () => {
    expect(splitOnArtifactRefs("a {{artifact:v1}} b")).toEqual([
      { type: "text", value: "a " },
      { type: "ref", versionId: "v1", marker: "{{artifact:v1}}" },
      { type: "text", value: " b" },
    ]);
  });

  it("returns a single segment when there are no references", () => {
    expect(splitOnArtifactRefs("plain")).toEqual([
      { type: "text", value: "plain" },
    ]);
  });

  it("handles a reference at each boundary", () => {
    expect(splitOnArtifactRefs("{{artifact:v1}}{{artifact:v2}}")).toEqual([
      { type: "ref", versionId: "v1", marker: "{{artifact:v1}}" },
      { type: "ref", versionId: "v2", marker: "{{artifact:v2}}" },
    ]);
  });
});
