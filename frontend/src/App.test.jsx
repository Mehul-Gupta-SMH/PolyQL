import React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App.jsx'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const TOKEN_KEY = 'poly_ql_token'

/** Build an unsigned JWT-shaped token; the app only decodes the payload. */
function makeToken({ sub = 'alice', uid = 1, expOffsetSec = 3600 } = {}) {
  const b64 = (o) => btoa(JSON.stringify(o))
  const exp = Math.floor(Date.now() / 1000) + expOffsetSec
  return `${b64({ alg: 'none' })}.${b64({ sub, uid, exp })}.sig`
}

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: () => Promise.resolve(body) }
}

/** Minimal SSE response whose body yields the given events then ends. */
function sseResponse(events) {
  const encoder = new TextEncoder()
  const chunks = events.map((e) => encoder.encode(`data: ${JSON.stringify(e)}\n\n`))
  let i = 0
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: () =>
          Promise.resolve(i < chunks.length ? { value: chunks[i++], done: false } : { done: true }),
      }),
    },
  }
}

/**
 * URL-routed fetch mock. `chat` is what POST /api/chat/stream resolves to
 * (a response object) or rejects with (an Error).
 */
function mockFetch({ providers = ['anthropic', 'codex', 'open_ai'], providersFail = false, sessions = [], chat } = {}) {
  const fetchMock = vi.fn((url) => {
    switch (url) {
      case '/api/providers':
        return providersFail
          ? Promise.reject(new Error('network'))
          : Promise.resolve(jsonResponse({ providers }))
      case '/api/providers/balance':
        return Promise.resolve(jsonResponse({ balances: {} }))
      case '/api/instances':
        return Promise.resolve(jsonResponse({ instances: [] }))
      case '/api/sessions':
        return Promise.resolve(jsonResponse({ sessions }))
      case '/api/chat/stream':
        return chat instanceof Error ? Promise.reject(chat) : Promise.resolve(chat)
      case '/auth/google/enabled':
        return Promise.resolve(jsonResponse({ enabled: false }))
      default:
        return Promise.resolve(jsonResponse({}))
    }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function callsTo(fetchMock, url) {
  return fetchMock.mock.calls.filter(([u]) => u === url)
}

async function renderAuthenticated(opts) {
  localStorage.setItem(TOKEN_KEY, makeToken())
  const fetchMock = mockFetch(opts)
  render(<App />)
  // Provider <select> is the first combobox in the chat toolbar
  await waitFor(() => expect(screen.getAllByRole('option', { name: /anthropic/i }).length).toBeGreaterThan(0))
  return fetchMock
}

/** The login page is the only screen with a password field. */
function findLoginPage() {
  return waitFor(() => {
    const pw = document.querySelector('input[type="password"]')
    expect(pw).toBeInTheDocument()
    return pw
  })
}

async function sendMessage(text) {
  await userEvent.type(screen.getByPlaceholderText(/ask a question/i), text)
  await userEvent.click(screen.getByRole('button', { name: 'Send' }))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('App', () => {
  beforeAll(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn()
  })

  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    localStorage.clear()
  })

  describe('when not authenticated', () => {
    it('renders the login page and does not fetch providers', async () => {
      const fetchMock = mockFetch()
      render(<App />)
      await findLoginPage()
      expect(callsTo(fetchMock, '/api/providers')).toHaveLength(0)
      expect(screen.queryByRole('button', { name: 'Schema / ERD' })).not.toBeInTheDocument()
    })

    it('treats an expired token as not logged in', async () => {
      localStorage.setItem(TOKEN_KEY, makeToken({ expOffsetSec: -60 }))
      const fetchMock = mockFetch()
      render(<App />)
      await findLoginPage()
      expect(callsTo(fetchMock, '/api/providers')).toHaveLength(0)
    })
  })

  describe('when authenticated', () => {
    it('fetches /api/providers on mount', async () => {
      const fetchMock = await renderAuthenticated()
      expect(fetchMock).toHaveBeenCalledWith('/api/providers')
    })

    it('populates the provider dropdown after mount', async () => {
      await renderAuthenticated({ providers: ['anthropic', 'open_ai'] })
      const providerSelect = screen
        .getAllByRole('combobox')
        .find((s) => within_options(s).includes('anthropic'))
      expect(providerSelect).toBeDefined()
      expect(within_options(providerSelect)).toHaveLength(2)
      expect(within_options(providerSelect)).toEqual(['anthropic', 'open_ai'])
    })

    it('renders the header with all tabs and the signed-in username', async () => {
      await renderAuthenticated()
      expect(screen.getByText('Poly-QL')).toBeInTheDocument()
      for (const name of ['Query', 'Schema / ERD', 'Ingest Table', 'Join Path', 'Glossary', 'Knowledge']) {
        expect(screen.getByRole('button', { name })).toBeInTheDocument()
      }
      expect(screen.getByText('alice')).toBeInTheDocument()
    })

    it('still renders the app (with an empty provider list) when the provider fetch fails', async () => {
      localStorage.setItem(TOKEN_KEY, makeToken())
      const fetchMock = mockFetch({ providersFail: true })
      render(<App />)
      await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/providers'))
      expect(screen.getByRole('button', { name: 'Schema / ERD' })).toBeInTheDocument()
      expect(screen.queryByRole('option', { name: /anthropic/i })).not.toBeInTheDocument()
    })

    it('switches tabs', async () => {
      const fetchMock = await renderAuthenticated()
      expect(screen.getByPlaceholderText(/ask a question/i)).toBeInTheDocument()

      await userEvent.click(screen.getByRole('button', { name: 'Join Path' }))
      // Join Path tab loads the schema table list
      await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/schema'))
      expect(screen.queryByPlaceholderText(/ask a question/i)).not.toBeInTheDocument()

      await userEvent.click(screen.getByRole('button', { name: 'Query' }))
      expect(screen.getByPlaceholderText(/ask a question/i)).toBeInTheDocument()
    })

    it('returns to the login page and clears the token on Sign out', async () => {
      await renderAuthenticated()
      await userEvent.click(screen.getByRole('button', { name: /sign out/i }))
      await findLoginPage()
      expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    })

    it('loads saved sessions into the session pane', async () => {
      await renderAuthenticated({
        sessions: [{ id: 's1', title: 'orders last month', timestamp: Date.now(), provider: 'open_ai', messages: [] }],
      })
      expect(await screen.findByText('orders last month')).toBeInTheDocument()
    })
  })

  describe('chat', () => {
    it('disables Send until a message is typed and makes no chat request for an empty message', async () => {
      const fetchMock = await renderAuthenticated()
      const send = screen.getByRole('button', { name: 'Send' })
      expect(send).toBeDisabled()
      await userEvent.click(send)
      expect(callsTo(fetchMock, '/api/chat/stream')).toHaveLength(0)
    })

    it('posts the message to /api/chat/stream with the bearer token and shows the SQL result', async () => {
      const fetchMock = await renderAuthenticated({
        chat: sseResponse([
          { event: 'token', data: 'SELECT' },
          { event: 'done', type: 'sql', content: 'SELECT 1', query_type: 'sql' },
        ]),
      })
      await sendMessage('show me orders')

      expect(await screen.findByText('SELECT 1')).toBeInTheDocument()

      const [, init] = callsTo(fetchMock, '/api/chat/stream')[0]
      expect(init.method).toBe('POST')
      expect(init.headers.Authorization).toMatch(/^Bearer /)
      const body = JSON.parse(init.body)
      expect(body.messages).toEqual([{ role: 'user', content: 'show me orders' }])
      expect(body.provider).toBe('anthropic')
    })

    it('adds the conversation to the session pane and saves it', async () => {
      const fetchMock = await renderAuthenticated({
        chat: sseResponse([{ event: 'done', type: 'sql', content: 'SELECT 1', query_type: 'sql' }]),
      })
      await sendMessage('show me orders')
      await screen.findByText('SELECT 1')

      // title is the first user message; it now appears in the session pane too
      await waitFor(() => expect(screen.getAllByText('show me orders').length).toBeGreaterThanOrEqual(2))
      const saves = callsTo(fetchMock, '/api/sessions').filter(([, init]) => init?.method === 'POST')
      expect(saves.length).toBeGreaterThan(0)
      expect(JSON.parse(saves.at(-1)[1].body).title).toBe('show me orders')
    })

    it('shows the detail field as an error when the API returns a non-ok response', async () => {
      await renderAuthenticated({
        chat: { ok: false, status: 400, json: () => Promise.resolve({ detail: 'Invalid query input.' }) },
      })
      await sendMessage('bad query')
      expect(await screen.findByText('Invalid query input.')).toBeInTheDocument()
    })

    it('shows a network error message when fetch throws', async () => {
      await renderAuthenticated({ chat: new Error('network') })
      await sendMessage('show me orders')
      expect(await screen.findByText(/network error/i)).toBeInTheDocument()
    })

    it('logs the user out when the chat endpoint returns 401', async () => {
      await renderAuthenticated({
        chat: { ok: false, status: 401, json: () => Promise.resolve({ detail: 'Token expired' }) },
      })
      await sendMessage('show me orders')
      await findLoginPage()
      expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    })
  })
})

function within_options(select) {
  return Array.from(select.querySelectorAll('option')).map((o) => o.value)
}
