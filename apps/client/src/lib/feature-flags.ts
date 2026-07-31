// Build-time feature flags for surfaces we keep in the codebase but don't ship
// in the UI yet (or any more).
//
// A flag here is deliberately a plain `const`, not a runtime setting: flipping
// it is a code change reviewed like any other, and `false` lets the bundler
// drop the gated branches. Nothing reads these from config or localStorage.

/**
 * Profiles — multiple isolated agent instances, each with its own
 * OPENCODON_HOME (config, .env, memory, sessions, skills, gateway).
 *
 * Turned OFF: the dashboard runs against the `default` profile only. The
 * backend is untouched — `opencodon -p <name>`, the profile wrapper aliases,
 * gateway multiplexing, and the kanban decomposer's profile routing all keep
 * working from the CLI. This flag hides only the *client* surfaces:
 *
 *   - the `/profiles` management page (the route stays reserved so the path is
 *     never mistaken for a session id; it just renders nothing)
 *   - the sidebar profile rail / switcher and ALL-profiles session grouping
 *   - the command palette's "Profiles" navigation entry
 *   - the "Profiles" category in keybind settings
 *
 * Set to `true` to bring all of it back — no other change should be needed.
 *
 * Note: several surfaces (the appearance per-profile theme note, the gateway
 * settings per-profile remote scopes) already self-hide when only the default
 * profile exists, so they need no explicit gate.
 */
export const PROFILES_UI_ENABLED = false
