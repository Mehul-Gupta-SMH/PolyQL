import { useState, useRef, useEffect } from 'react'
import ChatMessage from './ChatMessage.jsx'
import { PROVIDER_LABELS, PROVIDER_MODELS, formatProviderLabel, defaultModel } from '../constants/providerLabels.js'
import { apiFetch } from '../utils/api.js'

const QUERY_TYPE_LABELS = {
  sql: 'SQL',
  spark_sql: 'Spark SQL',
  dataframe_api: 'DataFrame API',
  pandas: 'Pandas',
}

const MAX_SESSIONS = 30

let _msgId = 0
function nextId() { return ++_msgId }

function formatTimestamp(ts) {
  const d = new Date(ts)
  const now = new Date()
  if (d.toDateString() === now.toDateString())
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday'
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

// ---------------------------------------------------------------------------
// Left pane — session list
// ---------------------------------------------------------------------------
function SessionPane({ sessions, currentId, onSelect, onNew, onDelete }) {
  const [hoverId, setHoverId] = useState(null)

  return (
    <div style={{
      width: 220, flexShrink: 0,
      borderRight: '1px solid #e5e7eb',
      display: 'flex', flexDirection: 'column',
      background: '#f9fafb',
      overflowY: 'auto',
    }}>
      <div style={{ padding: '10px 10px 6px' }}>
        <button
          onClick={onNew}
          style={{
            width: '100%', padding: '7px 12px', fontSize: 13, fontWeight: 600,
            background: '#2563eb', color: '#fff', border: 'none',
            borderRadius: 7, cursor: 'pointer', textAlign: 'left',
          }}
        >
          + New Chat
        </button>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '2px 6px 8px' }}>
        {sessions.length === 0 && (
          <p style={{ fontSize: 12, color: '#9ca3af', textAlign: 'center', marginTop: 24 }}>
            No conversations yet
          </p>
        )}
        {sessions.map((s) => (
          <div
            key={s.id}
            onMouseEnter={() => setHoverId(s.id)}
            onMouseLeave={() => setHoverId(null)}
            style={{
              position: 'relative',
              borderRadius: 6,
              marginBottom: 2,
              background: s.id === currentId ? '#e0e7ff' : hoverId === s.id ? '#f3f4f6' : 'transparent',
              cursor: 'pointer',
            }}
          >
            <div
              onClick={() => onSelect(s)}
              style={{ padding: '7px 28px 7px 10px' }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                <div style={{
                  fontSize: 13, fontWeight: s.id === currentId ? 600 : 400,
                  color: '#1f2937', flex: 1,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {s.title}
                </div>
                {s.lastOutcome && (
                  <span
                    title={s.lastOutcome === 'success' ? 'Query ran successfully' : s.lastOutcome === 'empty' ? 'Query returned no rows' : 'Query failed'}
                    style={{
                      flexShrink: 0,
                      width: 10, height: 10, borderRadius: '50%',
                      background: s.lastOutcome === 'success' ? '#16a34a'
                                : s.lastOutcome === 'empty'   ? '#ca8a04'
                                : '#dc2626',
                      boxShadow: `0 0 0 2px ${
                        s.lastOutcome === 'success' ? '#bbf7d0'
                        : s.lastOutcome === 'empty' ? '#fef08a'
                        : '#fecaca'}`,
                    }}
                  />
                )}
              </div>
              <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 2 }}>
                {formatTimestamp(s.timestamp)} · {PROVIDER_LABELS[s.provider] ?? s.provider}
              </div>
            </div>
            {hoverId === s.id && (
              <button
                onClick={(e) => { e.stopPropagation(); onDelete(s.id) }}
                style={{
                  position: 'absolute', top: 6, right: 6,
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: '#9ca3af', fontSize: 14, lineHeight: 1, padding: 2,
                }}
                title="Delete"
              >
                ×
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main chat component
// ---------------------------------------------------------------------------
export default function ChatInterface({ providers }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [provider, setProvider] = useState(providers[0] ?? '')
  const [model, setModel] = useState(() => defaultModel(providers[0] ?? ''))
  const [queryType, setQueryType] = useState('sql')
  const [loading, setLoading] = useState(false)
  const [balances, setBalances] = useState({})
  const [sessions, setSessions] = useState([])
  const [currentSessionId, setCurrentSessionId] = useState(null)
  const [instance, setInstance] = useState('default')
  const [instances, setInstances] = useState([{ instance_name: 'default', db_type: 'generic' }])
  const bottomRef       = useRef(null)
  const sessionIdRef    = useRef(null)
  const skipNextSaveRef = useRef(false)  // skip auto-save when loading a session

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  // Sync provider (and default model) when list first loads
  useEffect(() => {
    if (providers.length && !provider) {
      setProvider(providers[0])
      setModel(defaultModel(providers[0]))
    }
  }, [providers])

  // Fetch provider balances
  useEffect(() => {
    if (!providers.length) return
    fetch('/api/providers/balance')
      .then((r) => r.json())
      .then((data) => setBalances(data.balances ?? {}))
      .catch(() => {})
  }, [providers])

  // Fetch available instances
  useEffect(() => {
    fetch('/api/instances')
      .then((r) => r.json())
      .then((data) => {
        if (data.instances?.length) setInstances(data.instances)
      })
      .catch(() => {})
  }, [])

  // Load sessions from server on mount
  useEffect(() => {
    apiFetch('/api/sessions')
      .then((r) => r.json())
      .then((data) => setSessions(data.sessions ?? []))
      .catch(() => {})
  }, [])

  // Auto-save current session to server whenever messages change
  useEffect(() => {
    if (!messages.length || !sessionIdRef.current) return
    if (skipNextSaveRef.current) { skipNextSaveRef.current = false; return }

    const title    = (messages.find((m) => m.role === 'user')?.content ?? 'Conversation').slice(0, 55)
    const existing = sessions.find((s) => s.id === sessionIdRef.current)
    const entry    = {
      id:        sessionIdRef.current,
      title,
      timestamp: existing?.timestamp ?? Date.now(),
      messages,
      provider,
      model,
      queryType,
    }

    setSessions((prev) => {
      const others = prev.filter((s) => s.id !== sessionIdRef.current)
      return [entry, ...others].slice(0, MAX_SESSIONS)
    })

    apiFetch('/api/sessions', {
      method: 'POST',
      body:   JSON.stringify(entry),
    }).catch(() => {})
  }, [messages])

  // ---- Session management -----------------------------------------------

  function handleNewChat() {
    skipNextSaveRef.current = true
    setMessages([])
    setCurrentSessionId(null)
    sessionIdRef.current = null
    setInput('')
  }

  function handleSelectSession(session) {
    skipNextSaveRef.current = true
    setMessages(session.messages)
    setProvider(session.provider)
    setModel(session.model ?? defaultModel(session.provider))
    setQueryType(session.queryType)
    setCurrentSessionId(session.id)
    sessionIdRef.current = session.id
  }

  function handleDeleteSession(id) {
    setSessions((prev) => prev.filter((s) => s.id !== id))
    apiFetch(`/api/sessions/${id}`, { method: 'DELETE' }).catch(() => {})
    if (sessionIdRef.current === id) handleNewChat()
  }

  // ---- Message helpers ---------------------------------------------------

  function pushMsg(msg) {
    setMessages((prev) => [...prev, { id: nextId(), ...msg }])
  }

  // Conversation history for /api/chat — only dialogue turns, not SQL blobs
  function buildHistory(msgs) {
    return msgs
      .filter((m) => m.type === 'text' || m.type === 'clarify')
      .map((m) => ({ role: m.role, content: m.content }))
  }

  // ---- API call ----------------------------------------------------------

  // Streaming call — uses /api/chat/stream SSE endpoint.
  // Tokens arrive incrementally and update the in-progress bubble; the final
  // "done" event replaces it with the validated, formatted response.
  async function _callApiStream(history, prov, qType, retryLabel, mdl, inst) {
    setLoading(true)
    // Insert a placeholder streaming bubble so the user sees tokens arriving.
    const streamId = nextId()
    setMessages((prev) => [
      ...prev,
      { id: streamId, role: 'assistant', type: 'streaming', content: '', queryType: qType },
    ])

    try {
      const token = localStorage.getItem('poly_ql_token') || ''
      const res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          messages: history,
          provider: prov,
          query_type: qType,
          model: mdl,
          instance_name: inst ?? instance,
        }),
      })

      if (res.status === 401) {
        localStorage.removeItem('poly_ql_token')
        window.dispatchEvent(new Event('auth:logout'))
        return
      }

      if (res.status === 429) {
        const secs = parseInt(res.headers?.get?.('Retry-After'), 10)
        const wait = Number.isFinite(secs) && secs > 0 ? `${secs} s` : 'a moment'
        setMessages((prev) => prev.map((m) =>
          m.id === streamId
            ? { ...m, type: 'error', content: `Too many requests — try again in ${wait}.`, retryQuery: retryLabel }
            : m
        ))
        return
      }

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        setMessages((prev) => prev.map((m) =>
          m.id === streamId
            ? { ...m, type: 'error', content: errData.detail ?? 'An error occurred.', retryQuery: retryLabel }
            : m
        ))
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // Process complete SSE lines from buffer
        const lines = buffer.split('\n')
        buffer = lines.pop() // last fragment — may be incomplete

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const evt = JSON.parse(line.slice(6))

            if (evt.event === 'token') {
              setMessages((prev) => prev.map((m) =>
                m.id === streamId ? { ...m, content: m.content + evt.data } : m
              ))
            } else if (evt.event === 'done') {
              const msgType = evt.type === 'clarify' ? 'clarify' : 'sql'
              const finalMsg = { id: streamId, role: 'assistant', type: msgType, content: evt.content, queryType: evt.query_type ?? qType }
              if (evt.options?.length) finalMsg.options = evt.options
              setMessages((prev) => prev.map((m) => m.id === streamId ? finalMsg : m))
            } else if (evt.event === 'error') {
              setMessages((prev) => prev.map((m) =>
                m.id === streamId
                  ? { ...m, type: 'error', content: evt.detail ?? 'An error occurred.', retryQuery: retryLabel }
                  : m
              ))
            }
          } catch {
            // Malformed SSE line — skip
          }
        }
      }
    } catch {
      setMessages((prev) => prev.map((m) =>
        m.id === streamId
          ? { ...m, type: 'error', content: 'Network error — is the backend running?', retryQuery: retryLabel }
          : m
      ))
    } finally {
      setLoading(false)
    }
  }

  async function handleSend() {
    const text = input.trim()
    if (!text || loading) return
    setInput('')

    // Start a new session on the first message
    if (!sessionIdRef.current) {
      const newId = Date.now().toString()
      sessionIdRef.current = newId
      setCurrentSessionId(newId)
    }

    const userEntry = { id: nextId(), role: 'user', type: 'text', content: text }
    const updated = [...messages, userEntry]
    setMessages(updated)

    await _callApiStream(buildHistory(updated), provider, queryType, text, model, instance)
  }

  function handleRetry(failedMsg) {
    if (loading) return
    const prior = messages.filter((m) => m.id !== failedMsg.id)
    setMessages(prior)
    // Exclude previous assistant clarify messages so the requirement gathering
    // agent re-evaluates fresh from user messages only (not its own prior questions).
    const retryHistory = prior
      .filter((m) => m.type === 'text')
      .map((m) => ({ role: m.role, content: m.content }))
    _callApiStream(retryHistory, provider, queryType, failedMsg.retryQuery, model, instance)
  }

  async function handleOptionSelect(optionText) {
    if (loading) return
    setInput('')
    if (!sessionIdRef.current) {
      const newId = Date.now().toString()
      sessionIdRef.current = newId
      setCurrentSessionId(newId)
    }
    const userEntry = { id: nextId(), role: 'user', type: 'text', content: optionText }
    const updated = [...messages, userEntry]
    setMessages(updated)
    await _callApiStream(buildHistory(updated), provider, queryType, optionText, model, instance)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }

  const isEmpty = messages.length === 0

  // ---- Render ------------------------------------------------------------

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 110px)' }}>

      {/* ── Left pane: session history ── */}
      <SessionPane
        sessions={sessions}
        currentId={currentSessionId}
        onSelect={handleSelectSession}
        onNew={handleNewChat}
        onDelete={handleDeleteSession}
      />

      {/* ── Right: chat area ── */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, paddingLeft: 16 }}>

        {/* Toolbar */}
        <div style={{
          display: 'flex', gap: 8, alignItems: 'center',
          padding: '6px 0 10px',
          borderBottom: '1px solid #e5e7eb',
          flexShrink: 0,
        }}>
          <span style={{ fontSize: 12, color: '#6b7280', fontWeight: 500 }}>Provider</span>
          <select
            style={{ padding: '4px 10px', fontSize: 13, border: '1px solid #d1d5db', borderRadius: 6, background: '#fff' }}
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value)
              setModel(defaultModel(e.target.value))
            }}
            disabled={loading}
          >
            {providers.map((p) => {
              const bal = balances[p]
              const unavailable = bal?.available === false
              const label = formatProviderLabel(p, balances)
              return (
                <option key={p} value={p} disabled={unavailable}>
                  {label}{unavailable ? ' (unavailable)' : ''}
                </option>
              )
            })}
          </select>

          {PROVIDER_MODELS[provider]?.length > 0 && (
            <>
              <span style={{ fontSize: 12, color: '#6b7280', fontWeight: 500, marginLeft: 4 }}>Model</span>
              <select
                style={{ padding: '4px 10px', fontSize: 13, border: '1px solid #d1d5db', borderRadius: 6, background: '#fff' }}
                value={model ?? ''}
                onChange={(e) => setModel(e.target.value)}
                disabled={loading}
              >
                {PROVIDER_MODELS[provider].map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </>
          )}

          <span style={{ fontSize: 12, color: '#6b7280', fontWeight: 500, marginLeft: 8 }}>Mode</span>
          <select
            style={{ padding: '4px 10px', fontSize: 13, border: '1px solid #d1d5db', borderRadius: 6, background: '#fff' }}
            value={queryType}
            onChange={(e) => setQueryType(e.target.value)}
            disabled={loading}
          >
            {Object.entries(QUERY_TYPE_LABELS).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>

          <span style={{ fontSize: 12, color: '#6b7280', fontWeight: 500, marginLeft: 8 }}>Instance</span>
          <select
            style={{ padding: '4px 10px', fontSize: 13, border: '1px solid #d1d5db', borderRadius: 6, background: '#fff' }}
            value={instance}
            onChange={(e) => setInstance(e.target.value)}
            disabled={loading}
          >
            {instances.map((i) => (
              <option key={i.instance_name} value={i.instance_name}>
                {i.instance_name}
              </option>
            ))}
          </select>

          {messages.length > 0 && (
            <button
              onClick={handleNewChat}
              style={{
                marginLeft: 'auto', background: 'none', border: '1px solid #e5e7eb',
                borderRadius: 5, padding: '3px 10px', fontSize: 12,
                color: '#6b7280', cursor: 'pointer',
              }}
            >
              Clear chat
            </button>
          )}
        </div>

        {/* Messages */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '20px 0 8px' }}>
          {isEmpty && (
            <div style={{ textAlign: 'center', marginTop: '15%', color: '#9ca3af' }}>
              <div style={{ marginBottom: 12 }}>
                <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="#d1d5db" strokeWidth="1.2">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M16.24 7.76l-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12z" fill="#e5e7eb" stroke="none" />
                  <circle cx="12" cy="12" r="1.5" fill="#9ca3af" stroke="none" />
                </svg>
              </div>
              <p style={{ fontSize: 15, fontWeight: 600, color: '#6b7280', margin: '0 0 6px' }}>
                Ask a question about your data
              </p>
              <p style={{ fontSize: 13, color: '#9ca3af', margin: 0 }}>
                Type below — the assistant will gather requirements then generate {QUERY_TYPE_LABELS[queryType]}.
              </p>
            </div>
          )}

          {messages.map((msg) => (
            <ChatMessage key={msg.id} msg={msg} onRetry={handleRetry} onOptionSelect={handleOptionSelect}
              onOutcome={(outcome) => {
                setSessions((prev) => {
                  const next = prev.map((s) =>
                    s.id === sessionIdRef.current ? { ...s, lastOutcome: outcome } : s
                  )
                  // persist using the freshly-updated session (avoids stale closure)
                  const updated = next.find((s) => s.id === sessionIdRef.current)
                  if (updated) {
                    apiFetch('/api/sessions', {
                      method: 'POST',
                      body: JSON.stringify(updated),
                    }).catch(() => {})
                  }
                  return next
                })
              }}
            />
          ))}

          {loading && (
            <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 14 }}>
              <div style={{
                background: '#f3f4f6', borderRadius: '4px 16px 16px 16px',
                padding: '10px 18px', fontSize: 14, color: '#6b7280',
                display: 'flex', gap: 4, alignItems: 'center',
              }}>
                <span style={{ animation: 'pulse 1s infinite' }}>●</span>
                <span style={{ opacity: 0.6 }}>●</span>
                <span style={{ opacity: 0.3 }}>●</span>
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input bar */}
        <div style={{
          borderTop: '1px solid #e5e7eb', paddingTop: 12,
          flexShrink: 0, display: 'flex', gap: 8, alignItems: 'flex-end',
        }}>
          <textarea
            rows={2}
            style={{
              flex: 1, resize: 'none', padding: '10px 14px', fontSize: 14,
              border: '1px solid #d1d5db', borderRadius: 10, fontFamily: 'inherit',
              lineHeight: 1.5, outline: 'none', boxSizing: 'border-box',
            }}
            placeholder="Ask a question… (Enter to send, Shift+Enter for newline)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={loading}
          />
          <button
            onClick={handleSend}
            disabled={loading || !input.trim()}
            style={{
              padding: '10px 18px',
              background: loading || !input.trim() ? '#e5e7eb' : '#2563eb',
              color: loading || !input.trim() ? '#9ca3af' : '#fff',
              border: 'none', borderRadius: 10, fontWeight: 700, fontSize: 14,
              cursor: loading || !input.trim() ? 'default' : 'pointer',
              flexShrink: 0, alignSelf: 'flex-end', height: 44,
            }}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  )
}
