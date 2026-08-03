/**
 * Build-time feature flags for the TUI.
 *
 * These gate surfaces that are implemented but not shipped yet. Nothing behind
 * a flag is deleted — flip it to `true` to bring the feature back in one move.
 */

/**
 * Voice (dictation, TTS, push-to-talk) is wired end to end but not supported
 * yet, so the TUI hides all three of its entry points together:
 *
 *   1. the `voice on/off` status-bar segment (and its `● REC` / `◉ STT` states)
 *   2. the push-to-talk record key (`voice.record_key`, default per platform)
 *   3. the `/voice` slash command
 *
 * They have to move as a unit. Hiding only the indicator would leave the record
 * key live, so voice could start with nothing on screen to say it had — worse
 * than leaving it visible.
 *
 * Mirrors `VOICE_UI_ENABLED` in apps/client/src/app/chat/composer/controls.tsx.
 *
 * Typed as `boolean` rather than inferred as `false` so the guarded branches
 * stay type-checked instead of narrowing to dead code.
 */
export const VOICE_UI_ENABLED: boolean = false
