import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { CopyButton } from './copy-button'

describe('CopyButton i18n', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('uses default labels and copied feedback', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText }
    })

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <CopyButton text="hello" />
      </I18nProvider>
    )

    const button = screen.getByRole('button', { name: 'Copy' })

    expect(button.textContent).toContain('Copy')
    fireEvent.click(button)

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('hello'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Copied' }).textContent).toContain('Copied')
  })
})
