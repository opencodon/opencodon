import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MemoryProviderConfig } from '@/types/opencodon'

const getMemoryProviderConfig = vi.fn()
const saveMemoryProviderConfig = vi.fn()

vi.mock('@/opencodon', () => ({
  getMemoryProviderConfig: (provider: string) => getMemoryProviderConfig(provider),
  saveMemoryProviderConfig: (provider: string, values: unknown) => saveMemoryProviderConfig(provider, values)
}))

vi.mock('@/store/profile', async () => {
  const { atom } = await import('nanostores')

  return { $activeGatewayProfile: atom('default') }
})

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

function extmemSchema(): MemoryProviderConfig {
  return {
    name: 'extmem',
    label: 'ExtMem',
    docs_url: 'https://example.test/extmem/docs',
    fields: [
      {
        key: 'apiKey',
        label: 'API key',
        kind: 'secret',
        value: '',
        description: 'Authenticate with ExtMem Cloud.',
        placeholder: 'Enter ExtMem API key',
        is_set: false,
        inline: true,
        group: 'Connection',
        options: []
      },
      {
        key: 'baseUrl',
        label: 'Base URL',
        kind: 'text',
        value: '',
        description: 'Self-hosted ExtMem URL.',
        placeholder: 'https://… (self-hosted)',
        is_set: false,
        inline: true,
        group: 'Connection',
        options: []
      },
      {
        key: 'environment',
        label: 'Environment',
        kind: 'select',
        value: 'production',
        description: 'ExtMem environment.',
        placeholder: '',
        is_set: true,
        inline: true,
        group: 'Connection',
        options: [
          { value: 'production', label: 'Production', description: '' },
          { value: 'demo', label: 'Demo', description: '' },
          { value: 'local', label: 'Local', description: '' }
        ]
      },
      {
        key: 'workspace',
        label: 'Workspace',
        kind: 'text',
        value: 'myws',
        description: 'ExtMem workspace ID.',
        placeholder: 'opencodon',
        is_set: true,
        inline: true,
        group: 'Connection',
        options: []
      },
      // Non-inline field: must NOT render in the compact panel and must NOT be
      // submitted when the panel saves.
      {
        key: 'writeFrequency',
        label: 'Write frequency',
        kind: 'text',
        value: 'async',
        description: '',
        placeholder: '',
        is_set: true,
        inline: false,
        group: 'Message writing',
        options: []
      }
    ]
  }
}

beforeEach(() => {
  getMemoryProviderConfig.mockResolvedValue(extmemSchema())
  saveMemoryProviderConfig.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function renderPanel(provider = 'extmem') {
  const { ProviderConfigPanel } = await import('./provider-config-panel')

  return render(<ProviderConfigPanel provider={provider} />)
}

describe('ProviderConfigPanel', () => {
  it('renders the declared inline fields generically', async () => {
    await renderPanel()

    expect(await screen.findByDisplayValue('myws')).toBeTruthy()
    expect(screen.getByPlaceholderText('https://… (self-hosted)')).toBeTruthy()
    expect(screen.getByText('Production')).toBeTruthy()
    expect(screen.getByText('Self-hosted ExtMem URL.')).toBeTruthy()
  })

  it('hides fields that are not marked inline', async () => {
    await renderPanel()

    await screen.findByDisplayValue('myws')
    expect(screen.queryByDisplayValue('async')).toBeNull()
    expect(screen.queryByText('Write frequency')).toBeNull()
  })

  it('collapses and expands the fields', async () => {
    await renderPanel()

    expect(await screen.findByDisplayValue('myws')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /ExtMem settings/ }))
    expect(screen.queryByDisplayValue('myws')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /ExtMem settings/ }))
    expect(await screen.findByDisplayValue('myws')).toBeTruthy()
  })

  it('autosaves a text field on blur as a one-key partial save', async () => {
    await renderPanel()

    const baseUrl = await screen.findByPlaceholderText('https://… (self-hosted)')
    fireEvent.change(baseUrl, { target: { value: 'http://localhost:8000' } })
    fireEvent.blur(baseUrl)

    await waitFor(() =>
      expect(saveMemoryProviderConfig).toHaveBeenCalledWith('extmem', { baseUrl: 'http://localhost:8000' })
    )
    expect(saveMemoryProviderConfig).toHaveBeenCalledTimes(1)
  })

  it('does not save on blur when nothing changed', async () => {
    await renderPanel()

    const workspace = await screen.findByDisplayValue('myws')
    fireEvent.blur(workspace)

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).toBeNull())
    expect(saveMemoryProviderConfig).not.toHaveBeenCalled()
  })

  it('autosaves a committed secret and clears the draft', async () => {
    await renderPanel()

    const apiKey = await screen.findByPlaceholderText('Enter ExtMem API key')
    fireEvent.blur(apiKey)
    expect(saveMemoryProviderConfig).not.toHaveBeenCalled()

    fireEvent.change(apiKey, { target: { value: 'hch-new-key' } })
    fireEvent.blur(apiKey)

    await waitFor(() => expect(saveMemoryProviderConfig).toHaveBeenCalledWith('extmem', { apiKey: 'hch-new-key' }))
    await waitFor(() => expect((apiKey as HTMLInputElement).value).toBe(''))
  })

  it('offers a full-config trigger when modal-only fields exist', async () => {
    await renderPanel()

    await screen.findByDisplayValue('myws')
    expect(screen.getByRole('button', { name: /Full config/ })).toBeTruthy()
  })

  it('shows an inline error with retry when the load fails, then recovers', async () => {
    getMemoryProviderConfig.mockRejectedValueOnce(new Error('Timed out connecting to Opencodon backend'))

    await renderPanel()

    expect(await screen.findByText(/Timed out connecting/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByDisplayValue('myws')).toBeTruthy()
  })

  it('renders nothing for a provider with no declared config surface', async () => {
    getMemoryProviderConfig.mockResolvedValue({ name: 'builtin', label: 'builtin', docs_url: '', fields: [] })

    const { container } = await renderPanel('builtin')

    await waitFor(() => expect(getMemoryProviderConfig).toHaveBeenCalledWith('builtin'))
    expect(container.querySelector('section')).toBeNull()
  })
})
