import { describe, expect, it } from "vitest";

import {
  CLAIM_LABEL,
  CLAIM_MEANING,
  CLAIM_TONE,
  formatBytes,
  formatAge,
  isReproduceClaim,
  runHealth,
  shortHash,
  languageLabel,
} from "./science-format";

describe("formatBytes", () => {
  it("renders each magnitude", () => {
    expect(formatBytes(812)).toBe("812 B");
    expect(formatBytes(15155)).toBe("14.8 KB");
    expect(formatBytes(1_258_291)).toBe("1.2 MB");
  });

  it("renders a dash for missing sizes", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(undefined)).toBe("—");
  });
});

describe("formatAge", () => {
  it("uses relative units inside a week", () => {
    const now = Date.now() / 1000;
    expect(formatAge(now - 30)).toBe("just now");
    expect(formatAge(now - 600)).toBe("10m ago");
    expect(formatAge(now - 7200)).toBe("2h ago");
    expect(formatAge(now - 3 * 86400)).toBe("3d ago");
  });

  it("falls back to a date past a week", () => {
    const old = Date.now() / 1000 - 30 * 86400;
    expect(formatAge(old)).toBe(new Date(old * 1000).toLocaleDateString());
  });
});

describe("runHealth", () => {
  it("classifies a recorded run", () => {
    expect(runHealth(0, 0)).toBe("empty");
    expect(runHealth(5, 0)).toBe("clean");
    expect(runHealth(5, 2)).toBe("partial");
    expect(runHealth(3, 3)).toBe("failed");
  });
});

describe("shortHash", () => {
  it("truncates to twelve characters", () => {
    expect(shortHash("a".repeat(64))).toBe("aaaaaaaaaaaa");
    expect(shortHash(null)).toBe("—");
  });
});

describe("languageLabel", () => {
  it("capitalises R and passes others through", () => {
    expect(languageLabel("r")).toBe("R");
    expect(languageLabel("python")).toBe("python");
    expect(languageLabel(null)).toBe("unknown");
  });
});

describe("reproduce claims", () => {
  it("labels every claim the backend can return", () => {
    for (const claim of [
      "reproduced",
      "diverged",
      "failed",
      "indeterminate",
      "ineligible",
    ] as const) {
      expect(CLAIM_LABEL[claim]).toBeTruthy();
      expect(CLAIM_MEANING[claim]).toBeTruthy();
      expect(CLAIM_TONE[claim]).toBeTruthy();
    }
  });

  it("never claims more than a byte match", () => {
    // The UI must not upgrade "reproduced" into "verified" or "correct".
    const wording = Object.values(CLAIM_MEANING).join(" ").toLowerCase();
    expect(wording).not.toContain("verified");
    expect(wording).not.toContain("correct");
    expect(CLAIM_MEANING.reproduced).toContain("identical bytes");
  });

  it("recognises only the five known claims", () => {
    expect(isReproduceClaim("reproduced")).toBe(true);
    expect(isReproduceClaim("verified")).toBe(false);
    expect(isReproduceClaim(null)).toBe(false);
  });
});
