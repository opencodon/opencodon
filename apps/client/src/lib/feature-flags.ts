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

/**
 * Messaging — running the agent over chat platforms (Telegram, Discord, Slack,
 * WhatsApp, and the webhook / API-server / relay adapters).
 *
 * Turned OFF: the dashboard does not present messaging as a surface. The
 * backend is untouched — the gateway still runs every wired adapter, and
 * `opencodon gateway` / the Channels page of the config dashboard at `/` are
 * unaffected. This flag hides only the *session UI* surfaces:
 *
 *   - the `/messaging` page (the route stays reserved so the path is never
 *     mistaken for a session id; it just renders nothing)
 *   - the sidebar's Messaging nav row and its per-platform session groups
 *   - the command palette's "Messaging" navigation entry
 *   - the `nav.messaging` keybind and its row in keybind settings
 *
 * Sessions that ARRIVED over a platform are not hidden from the session list
 * itself — they are real work, and dropping them would look like data loss.
 * What goes is the platform-grouped presentation and every affordance for
 * configuring a channel.
 *
 * Set to `true` to bring all of it back — no other change should be needed.
 */
export const MESSAGING_UI_ENABLED = false
