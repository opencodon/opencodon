import type { Translations } from "@/i18n/types";

const BUILTIN: Record<string, keyof Translations["app"]["nav"]> = {
  "/chat": "chat",
  "/frames": "frames",
  "/artifacts": "artifacts",
  // No /cells index exists; the entry is here so the permalink route
  // ("/cells/<id>") resolves through the collection fallback below.
  "/cells": "cells",
  "/settings": "settings",
  "/sessions": "sessions",
  "/analytics": "analytics",
  "/models": "models",
  "/logs": "logs",
  "/cron": "cron",
  "/skills": "skills",
  "/plugins": "plugins",
  "/profiles": "profiles",
  "/config": "config",
  "/env": "keys",
  "/docs": "documentation",
};

export function resolvePageTitle(
  pathname: string,
  t: Translations,
  pluginTabs: { path: string; label: string }[],
): string {
  const normalized = pathname.replace(/\/$/, "") || "/";
  if (normalized === "/") {
    return t.app.nav.frames;
  }
  const plugin = pluginTabs.find((p) => p.path === normalized);
  if (plugin) {
    return plugin.label;
  }
  const key = BUILTIN[normalized];
  if (key) {
    return t.app.nav[key];
  }
  // Detail routes carry an opaque id ("/frames/6f2c…"), which would render as
  // a title nobody can read. Name them after the collection instead; the page
  // itself replaces this with the real title (artifact filename, frame title)
  // as soon as its data lands.
  const [collection] = normalized.slice(1).split("/");
  const collectionKey = BUILTIN[`/${collection}`];
  if (collectionKey) {
    return t.app.nav[collectionKey];
  }
  // Derive title from pathname: "/profiles" → "Profiles"
  const segment = normalized.slice(1);
  if (segment) {
    return segment.charAt(0).toUpperCase() + segment.slice(1);
  }
  return t.app.webUi;
}
