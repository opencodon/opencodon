/**
 * Runtime SDK injection — the other half of the vscode-module model. Bundled
 * plugins resolve `@opencodon/plugin-sdk` through the vite alias; RUNTIME-loaded
 * plugins (disk / fetched) import the same specifier and get the same object:
 * the loader rewrites bare specifiers to shim modules that re-export the
 * live namespaces installed here. React ships as the app's singletons —
 * a second React instance would break hooks.
 */

import * as React from 'react'
import * as jsxDevRuntime from 'react/jsx-dev-runtime'
import * as jsxRuntime from 'react/jsx-runtime'

import * as sdk from './index'

const GLOBALS = {
  __OPENCODON_PLUGIN_SDK__: sdk,
  __OPENCODON_REACT__: React,
  __OPENCODON_REACT_JSX__: jsxRuntime,
  __OPENCODON_REACT_JSX_DEV__: jsxDevRuntime
} as const

export function installPluginSdk(): void {
  Object.assign(globalThis, GLOBALS)
}

/** Build a shim ESM blob that re-exports a global namespace's live members.
 *  Export names come from the namespace itself, so the list can't drift. */
function shimUrl(globalKey: keyof typeof GLOBALS): string {
  const names = Object.keys(GLOBALS[globalKey]).filter(name => name !== 'default' && /^[A-Za-z_$][\w$]*$/.test(name))

  const source =
    `const m = globalThis.${globalKey};\n` +
    `export default m.default ?? m;\n` +
    // Guard the destructuring: `export const {  } = m` is a syntax error, so
    // only emit it when the namespace actually has named exports.
    (names.length ? `export const { ${names.join(', ')} } = m;\n` : '')

  return URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
}

let cached: Record<string, string> | null = null

/** Specifier -> shim URL map for the runtime loader (longest keys first). */
export function sdkImportMap(): Record<string, string> {
  cached ??= {
    '@opencodon/plugin-sdk': shimUrl('__OPENCODON_PLUGIN_SDK__'),
    'react/jsx-dev-runtime': shimUrl('__OPENCODON_REACT_JSX_DEV__'),
    'react/jsx-runtime': shimUrl('__OPENCODON_REACT_JSX__'),
    react: shimUrl('__OPENCODON_REACT__')
  }

  return cached
}
