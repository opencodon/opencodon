import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { fieldCopyForSchemaKey } from '@/app/settings/field-copy'

import { en } from './en'
import { setRuntimeI18nLocale, translateNow } from './runtime'

describe('desktop i18n runtime translator', () => {
  beforeEach(() => {
    setRuntimeI18nLocale('en')
  })

  afterEach(() => {
    setRuntimeI18nLocale('en')
  })

  it('translates string paths for the active runtime locale', () => {
    expect(translateNow('boot.ready')).toBe(en.boot.ready)
    expect(translateNow('notifications.voice.noSpeechDetected')).toBe(
      en.notifications.voice.noSpeechDetected
    )
    expect(translateNow('composer.lookupNoMatches')).toBe(en.composer.lookupNoMatches)
    expect(translateNow('assistant.tool.statusRecovered')).toBe(en.assistant.tool.statusRecovered)
  })

  it('passes arguments to function translations', () => {
    expect(translateNow('notifications.updateReadyMessage', 2)).toBe('2 new changes available.')
  })

  it('keeps settings field copy addressable from schema keys', () => {
    const field = ['display', 'show_reasoning'].join('.')

    expect(fieldCopyForSchemaKey(en.settings.fieldLabels, field)).toBeTruthy()
    expect(fieldCopyForSchemaKey(en.settings.fieldDescriptions, field)).toBeTruthy()
  })

  it('returns the key when no locale can resolve a path', () => {
    expect(translateNow('missing.path')).toBe('missing.path')
  })
})
