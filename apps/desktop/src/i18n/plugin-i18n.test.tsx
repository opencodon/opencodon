import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider, useI18n } from './context'
import { createPluginI18n, registerPluginLocales, translatePlugin, usePluginI18n } from './plugin-i18n'
import { setRuntimeI18nLocale } from './runtime'

const noopTrack = (dispose: () => void) => dispose

afterEach(() => {
  cleanup()
  setRuntimeI18nLocale('en')
})

describe('plugin locale registry', () => {
  it('resolves the active locale, falling back to English then the raw key', () => {
    const dispose = registerPluginLocales('cost', {
      en: { panel: { title: 'Cost' }, spent: (n: number) => `$${n} spent` },
      ja: { panel: { title: 'コスト' } }
    })

    expect(translatePlugin('cost', 'ja', 'panel.title', [])).toBe('コスト')
    // Missing in ja → English.
    expect(translatePlugin('cost', 'ja', 'spent', [7])).toBe('$7 spent')
    // Missing everywhere → the key itself.
    expect(translatePlugin('cost', 'ja', 'nope', [])).toBe('nope')

    dispose()
  })

  it('scopes bundles per plugin — no cross-read', () => {
    const a = registerPluginLocales('a', { en: { hi: 'from a' } })
    const b = registerPluginLocales('b', { en: { hi: 'from b' } })

    expect(translatePlugin('a', 'en', 'hi', [])).toBe('from a')
    expect(translatePlugin('b', 'en', 'hi', [])).toBe('from b')
    // An unknown plugin resolves to the key.
    expect(translatePlugin('c', 'en', 'hi', [])).toBe('hi')

    a()
    b()
  })

  it('merges repeated registrations and drops everything on dispose', () => {
    const one = registerPluginLocales('merge', { en: { a: 'A' } })
    const two = registerPluginLocales('merge', { en: { b: 'B' }, ja: { a: 'あ' } })

    expect(translatePlugin('merge', 'en', 'a', [])).toBe('A')
    expect(translatePlugin('merge', 'en', 'b', [])).toBe('B')
    expect(translatePlugin('merge', 'ja', 'a', [])).toBe('あ')

    one()
    two()

    expect(translatePlugin('merge', 'en', 'a', [])).toBe('a')
  })

  it('ctx.i18n.t reads the app runtime locale', () => {
    const i18n = createPluginI18n('runtime-plugin', noopTrack)
    // A plugin may ship bundles for languages the app cannot select; only the
    // app's active locale ever resolves, and unknown keys fall back to the key.
    i18n.register({ en: { greet: 'hello' }, ja: { greet: 'こんにちは' } })

    setRuntimeI18nLocale('en')
    expect(i18n.t('greet')).toBe('hello')
    expect(i18n.t('absent')).toBe('absent')
  })
})

function Probe({ pluginId }: { pluginId: string }) {
  const t = usePluginI18n(pluginId)

  return <p data-testid="copy">{t('greet')}</p>
}

describe('usePluginI18n', () => {
  it('picks up a bundle registered after mount', () => {
    render(
      <I18nProvider configClient={null}>
        <Probe pluginId="late" />
      </I18nProvider>
    )

    expect(screen.getByTestId('copy').textContent).toBe('greet')

    let dispose = () => {}
    act(() => {
      dispose = registerPluginLocales('late', { en: { greet: 'landed' } })
    })

    expect(screen.getByTestId('copy').textContent).toBe('landed')

    dispose()
  })
})
