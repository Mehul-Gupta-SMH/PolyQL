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

function sseStreamUntilAbort(signal, first) {
  const enc = new TextEncoder()
  let sent = false
  return {
    getReader: () => ({
      read: () => {
        if (!sent) { sent = true; return Promise.resolve({ done: false, value: enc.encode(first) }) }
        return new Promise((_, reject) => {
          const err = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
          if (signal.aborted) err()
          else signal.addEventListener('abort', err)
        })
      },
    }),
  }
}

describe('ChatInterface stop button', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('shows Stop while streaming, aborts the fetch and keeps partial text as Stopped', async () => {
    let streamSignal
    vi.stubGlobal('fetch', vi.fn((url, opts) => {
      if (String(url).startsWith('/api/chat/stream')) {
        streamSignal = opts.signal
        return Promise.resolve({
          ok: true, status: 200,
          body: sseStreamUntilAbort(opts.signal, 'data: {"event":"token","data":"SELECT par"}\n\n'),
        })
      }
      return Promise.resolve({ json: () => Promise.resolve({ balances: {}, instances: [], sessions: [] }) })
    }))
    render(<ChatInterface providers={['open_ai']} />)
    await userEvent.type(screen.getByPlaceholderText(/Ask a question/), 'show orders{Enter}')

    expect(await screen.findByText('SELECT par')).toBeInTheDocument()
    const stop = screen.getByRole('button', { name: 'Stop' })
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    await userEvent.click(stop)

    await waitFor(() => expect(streamSignal.aborted).toBe(true))
    expect(await screen.findByText('Stopped')).toBeInTheDocument()
    expect(screen.getByText('SELECT par')).toBeInTheDocument()
    expect(screen.queryByText(/Network error/)).toBeNull()
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument()
  })

  it('Esc also stops', async () => {
    let streamSignal
    vi.stubGlobal('fetch', vi.fn((url, opts) => {
      if (String(url).startsWith('/api/chat/stream')) {
        streamSignal = opts.signal
        return Promise.resolve({ ok: true, status: 200, body: sseStreamUntilAbort(opts.signal, 'data: {"event":"token","data":"x"}\n\n') })
      }
      return Promise.resolve({ json: () => Promise.resolve({ balances: {}, instances: [], sessions: [] }) })
    }))
    render(<ChatInterface providers={['open_ai']} />)
    await userEvent.type(screen.getByPlaceholderText(/Ask a question/), 'q{Enter}')
    await screen.findByRole('button', { name: 'Stop' })
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(streamSignal.aborted).toBe(true))
    expect(await screen.findByText('Stopped')).toBeInTheDocument()
  })
})

describe('ChatInterface session import', () => {
  afterEach(() => { vi.restoreAllMocks() })

  function setup() {
    const fetchMock = vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ balances: {}, instances: [], sessions: [] }) }))
    vi.stubGlobal('fetch', fetchMock)
    render(<ChatInterface providers={['open_ai']} />)
    return fetchMock
  }
  const upload = (text) => {
    const file = new File([text], 's.json', { type: 'application/json' })
    file.text = () => Promise.resolve(text)
    return userEvent.upload(screen.getByLabelText('Import conversation file'), file)
  }

  it('shows an error and changes nothing for a bad file', async () => {
    const fetchMock = setup()
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const before = fetchMock.mock.calls.length
    await upload('{"hello":1}')
    expect(await screen.findByRole('alert')).toHaveTextContent(/Import failed: Not a Poly-QL session export/)
    expect(screen.getByText('No conversations yet')).toBeInTheDocument()
    expect(fetchMock.mock.calls.slice(before).some(([u]) => String(u) === '/api/sessions')).toBe(false)
  })

  it('imports a valid export, selects it and persists it', async () => {
    const fetchMock = setup()
    const doc = JSON.stringify({ format: 'polyql-session', version: 1, session: {
      id: 'x1', title: 'Imported one', timestamp: 1700000000000, provider: 'open_ai', queryType: 'sql',
      messages: [{ id: 1, role: 'user', type: 'text', content: 'hello there' }],
    } })
    await upload(doc)
    expect(await screen.findByText('Imported one')).toBeInTheDocument()
    expect(screen.getByText('hello there')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, o]) => String(u) === '/api/sessions' && o?.method === 'POST')).toBe(true))
  })
})
