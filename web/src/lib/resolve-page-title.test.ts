import { describe, expect, it } from "vitest";

import { resolvePageTitle } from "./resolve-page-title";
import { en } from "@/i18n/en";

const t = en;

describe("resolvePageTitle", () => {
  it("names the science surfaces", () => {
    expect(resolvePageTitle("/frames", t, [])).toBe("Frames");
    expect(resolvePageTitle("/artifacts", t, [])).toBe("Artifacts");
    expect(resolvePageTitle("/settings", t, [])).toBe("Settings");
  });

  it("names detail routes after their collection, not the raw id", () => {
    expect(resolvePageTitle("/frames/6f2c9ab1", t, [])).toBe("Frames");
    expect(resolvePageTitle("/artifacts/art_991", t, [])).toBe("Artifacts");
    expect(resolvePageTitle("/cells/cell_42", t, [])).toBe("Cell");
  });

  it("lands on frames at the root", () => {
    expect(resolvePageTitle("/", t, [])).toBe("Frames");
  });

  it("still prefers a plugin's own label", () => {
    const tabs = [{ path: "/kanban", label: "Kanban" }];
    expect(resolvePageTitle("/kanban", t, tabs)).toBe("Kanban");
  });

  it("falls back to a capitalised segment for unknown routes", () => {
    expect(resolvePageTitle("/webhooks", t, [])).toBe("Webhooks");
  });
});
