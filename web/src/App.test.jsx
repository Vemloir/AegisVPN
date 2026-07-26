import { useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import App, { LegalContent, Modal } from './App.jsx'
import * as api from './api.js'

vi.mock('./Globe.jsx', () => ({ default: () => <canvas aria-hidden="true" /> }))
vi.mock('./TelegramLogin.jsx', () => ({ default: () => <div data-testid="telegram-login" /> }))
vi.mock('./api.js', () => ({
  getLocations: vi.fn(),
  getPlans: vi.fn(),
  getMe: vi.fn(),
  getLegal: vi.fn(),
  loginWithTelegram: vi.fn(),
  loginWithTma: vi.fn(),
  logout: vi.fn(),
  acceptTerms: vi.fn(),
  checkout: vi.fn(),
}))

beforeEach(() => {
  api.getLocations.mockResolvedValue([])
  api.getPlans.mockResolvedValue([])
  api.getMe.mockResolvedValue(null)
  window.history.replaceState(null, '', '/ru/')
  window.innerWidth = 1200
})

function ModalHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <main data-modal-background>
        <button onClick={() => setOpen(true)}>Open</button>
      </main>
      {open && (
        <Modal onClose={() => setOpen(false)} ariaLabel="Test dialog" closeLabel="Close">
          <button>Action</button>
        </Modal>
      )}
    </>
  )
}

test('support link points to the support bot and copy does not claim cancellable renewal', async () => {
  render(<App />)

  expect(await screen.findByRole('link', { name: 'Поддержка' })).toHaveAttribute(
    'href',
    'https://t.me/AegisVPNsupportBot',
  )
  expect(screen.queryByText(/отмена в любой момент/i)).not.toBeInTheDocument()
})

test('modal is labelled, locks the page, traps focus, and restores it', async () => {
  const user = userEvent.setup()
  render(<ModalHarness />)
  const opener = screen.getByRole('button', { name: 'Open' })

  await user.click(opener)
  const dialog = screen.getByRole('dialog', { name: 'Test dialog' })
  const close = screen.getByRole('button', { name: 'Close' })
  expect(dialog).toHaveAttribute('aria-modal', 'true')
  expect(document.body).toHaveStyle({ overflow: 'hidden' })
  expect(document.activeElement).toBe(close)

  await user.tab()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Action' }))
  await user.tab({ shift: true })
  expect(document.activeElement).toBe(close)

  await user.click(close)
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(document.activeElement).toBe(opener)
  expect(document.body).not.toHaveStyle({ overflow: 'hidden' })
})

test('legal markdown renders headings and emphasis instead of source markers', () => {
  render(<LegalContent text={'## Раздел\n\nЭто **важно**.'} unavailable="Нет документа" />)

  expect(screen.getByRole('heading', { name: 'Раздел' })).toBeInTheDocument()
  expect(screen.getByText('важно')).toHaveProperty('tagName', 'STRONG')
  expect(screen.queryByText(/\*\*|<b>/)).not.toBeInTheDocument()
})

test('english route updates document language and metadata', async () => {
  window.history.replaceState(null, '', '/en/')
  render(<App />)

  await waitFor(() => expect(document.documentElement.lang).toBe('en'))
  expect(document.title).toBe('AegisVPN — private VPN for a steady connection')
  expect(document.querySelector('meta[name="description"]')).toHaveAttribute(
    'content',
    'Fast, private VPN with VLESS Reality and one subscription for every location.',
  )
})
