import { VOICE_UI_ENABLED } from '../../lib/featureFlags.js'

import { coreCommands } from './commands/core.js'
import { debugCommands } from './commands/debug.js'
import { opsCommands } from './commands/ops.js'
import { sessionCommands } from './commands/session.js'
import { setupCommands } from './commands/setup.js'
import type { SlashCommand } from './types.js'

/** Commands for features that aren't shipped yet. Dropped from the registry, so
 *  they vanish from the palette AND stop resolving in findSlashCommand(). */
const isHiddenCommand = (cmd: SlashCommand) => !VOICE_UI_ENABLED && cmd.name === 'voice'

export const SLASH_COMMANDS: SlashCommand[] = [
  ...coreCommands,
  ...sessionCommands,
  ...opsCommands,
  ...setupCommands,
  ...debugCommands
].filter(cmd => !isHiddenCommand(cmd))

const byName = new Map<string, SlashCommand>(
  SLASH_COMMANDS.flatMap(cmd => [cmd.name, ...(cmd.aliases ?? [])].map(name => [name, cmd] as const))
)

export const findSlashCommand = (name: string) => byName.get(name.toLowerCase())
