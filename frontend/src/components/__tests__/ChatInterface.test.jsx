import React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import ChatInterface from '../ChatInterface.jsx'
import ChatMessage from '../ChatMessage.jsx'

let originalScrollIntoView

beforeAll(() => {
  originalScrollIntoView = window.HTMLElement.prototype.scrollIntoView
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

afterAll(() => {
  window.HTMLElement.prototype.scrollIntoView = originalScrollIntoView
})

describe('ChatInterface Claude Code label handling', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      json: () => Promise.resolve({
        balances: {
          claude_code: { label: 'CLI 1.2.3', available: true },
        },
      }),
    })))
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the dropdown option with the backend-provided CLI label', async () => {
    render(<ChatInterface providers={['claude_code']} />)

    await waitFor(() => {
      expect(
        screen.getByRole('option', { name: /Claude Code\s+—\s+CLI 1\.2\.3/ }),
      ).toBeInTheDocument()
    })
  })
})

describe('ChatInterface provider balance display', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      json: () => Promise.resolve({
        balances: {
          open_ai: { label: '$5.00', available: false },
          google: { label: 'N/A', available: true },
        },
      }),
    })))
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('disables options when the backend marks the provider unavailable', async () => {
    render(<ChatInterface providers={['open_ai', 'google']} />)

    await waitFor(() => {
      const unavailable = screen.getByRole('option', { name: /OpenAI\s+—\s+\$5\.00/ })
      expect(unavailable).toBeDisabled()
      expect(screen.getByRole('option', { name: /Google Gemini\s+—\s+N\/A/ })).toBeEnabled()
    })
  })
})

describe('ChatInterface Codex dropdown', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      json: () => Promise.resolve({
        balances: {
          codex: { label: '$0.03', available: true },
        },
      }),
    })))
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders Codex option with formatted label', async () => {
    render(<ChatInterface providers={['codex']} />)

    await waitFor(() => {
      expect(
        screen.getByRole('option', { name: /OpenAI Codex\s+—\s+\$0\.03/ }),
      ).toBeInTheDocument()
    })
  })
})

describe('ChatMessage clarify options', () => {
  it('shows clarify options as pills and forwards clicks', async () => {
    const handleOptionSelect = vi.fn()

    render(
      <ChatMessage
        msg={{
          role: 'assistant',
          type: 'clarify',
          content: 'Need more detail',
          options: ['Use Claude Code', 'Try another provider'],
        }}
        onOptionSelect={handleOptionSelect}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Use Claude Code' }))

    expect(handleOptionSelect).toHaveBeenCalledTimes(1)
    expect(handleOptionSelect).toHaveBeenCalledWith('Use Claude Code')
  })
})

describe('ChatInterface rate limiting', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows a Retry-After message on 429', async () => {
    vi.stubGlobal('fetch', vi.fn((url) => {
      if (String(url).startsWith('/api/chat/stream')) {
        return Promise.resolve({
          ok: false,
          status: 429,
          headers: { get: (h) => (h === 'Retry-After' ? '7' : null) },
          json: () => Promise.resolve({ detail: 'Rate limit exceeded — try again in 7 s.' }),
        })
      }
      return Promise.resolve({ json: () => Promise.resolve({ balances: {}, instances: [] }) })
    }))
    render(<ChatInterface providers={['open_ai']} />)
    await userEvent.type(screen.getByPlaceholderText(/Ask a question/), 'show orders{Enter}')
    expect(await screen.findByText(/Too many requests — try again in 7 s/)).toBeInTheDocument()
  })
})
