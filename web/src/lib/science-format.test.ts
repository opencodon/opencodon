import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatAge,
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
