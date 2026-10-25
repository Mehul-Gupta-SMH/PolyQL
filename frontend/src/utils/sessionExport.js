// Pure helpers for exporting / importing chat sessions (U3).
export const FORMAT = 'polyql-session'
export const VERSION = 1
export const MAX_IMPORT_BYTES = 5 * 1024 * 1024

const ROLES = ['user', 'assistant']
const TYPES = ['text', 'clarify', 'sql', 'error', 'stopped']

const OUTCOME_TEXT = {
  success: '✓ Last execution succeeded',
  empty: '○ Last execution returned no rows',
  failure: '✕ Last execution failed',
}

const FENCE_LANG = { sql: 'sql', spark_sql: 'sql', dataframe_api: 'python', pandas: 'python' }

/** Full-fidelity, versioned JSON string for one session. */
export function toJSON(session) {
  return JSON.stringify({ format: FORMAT, version: VERSION, session }, null, 2)
}

/** Readable Markdown transcript. */
export function toMarkdown(session) {
  const lines = [`# ${session.title || 'Conversation'}`, '']
  const meta = []
  if (session.provider) meta.push(`Provider: ${session.provider}`)
  if (session.model) meta.push(`Model: ${session.model}`)
  if (session.queryType) meta.push(`Mode: ${session.queryType}`)
  if (session.timestamp) meta.push(`Started: ${new Date(session.timestamp).toISOString()}`)
  if (meta.length) lines.push(meta.join(' · '), '')

  for (const m of session.messages ?? []) {
    const content = m.content ?? ''
    if (m.role === 'user') {
      lines.push('**User:**', '', content, '')
    } else if (m.type === 'clarify') {
      lines.push('**Assistant (clarifying question):**', '', content)
      if (m.options?.length) lines.push('', ...m.options.map((o) => `- ${o}`))
      lines.push('')
    } else if (m.type === 'sql') {
      const lang = FENCE_LANG[m.queryType ?? session.queryType] ?? 'sql'
      lines.push('**Assistant:**', '', '```' + lang, content, '```', '')
    } else if (m.type === 'stopped') {
      lines.push('**Assistant (stopped, partial):**', '', content, '')
    } else if (m.type === 'error') {
      lines.push(`**Error:** ${content}`, '')
    } else {
      lines.push('**Assistant:**', '', content, '')
    }
  }
  if (OUTCOME_TEXT[session.lastOutcome]) lines.push('---', '', OUTCOME_TEXT[session.lastOutcome], '')
  return lines.join('\n')
}

/** Filesystem-safe filename stem for a session. */
export function exportFilename(session, ext) {
  const stem = (session.title || 'conversation').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40)
  return `polyql-${stem || 'conversation'}.${ext}`
}

const fail = (error) => ({ ok: false, error })

/**
 * Validate an exported session file's text.
 * Returns { ok: true, session } (new id if it clashes with existingIds) or { ok: false, error }.
 */
export function parseImport(text, existingIds = []) {
  if (typeof text !== 'string' || !text.trim()) return fail('The file is empty.')
  if (text.length > MAX_IMPORT_BYTES) return fail('The file is too large to import.')
  let data
  try { data = JSON.parse(text) } catch { return fail('Not valid JSON.') }
  if (!data || typeof data !== 'object' || data.format !== FORMAT) {
    return fail('Not a Poly-QL session export.')
  }
  if (data.version !== VERSION) return fail(`Unsupported session export version: ${data.version}.`)
  const s = data.session
  if (!s || typeof s !== 'object') return fail('The export has no session.')
  if (!Array.isArray(s.messages)) return fail('The session has no messages list.')
  if (typeof s.provider !== 'string' || !s.provider) return fail('The session has no provider.')
  if (typeof s.queryType !== 'string' || !s.queryType) return fail('The session has no query type.')

  const messages = []
  for (const [i, m] of s.messages.entries()) {
    if (!m || typeof m !== 'object' || !ROLES.includes(m.role) || typeof m.content !== 'string') {
      return fail(`Message ${i + 1} is malformed.`)
    }
    const type = TYPES.includes(m.type) ? m.type : m.type === 'streaming' ? 'stopped' : null
    if (!type) return fail(`Message ${i + 1} has an unknown type.`)
    const msg = { id: i + 1, role: m.role, type, content: m.content }
    if (m.queryType) msg.queryType = String(m.queryType)
    if (Array.isArray(m.options)) msg.options = m.options.filter((o) => typeof o === 'string')
    if (m.retryQuery) msg.retryQuery = String(m.retryQuery)
    messages.push(msg)
  }
  if (!messages.length) return fail('The session has no messages.')

  const taken = new Set(existingIds.map(String))
  let id = typeof s.id === 'string' && s.id ? s.id : Date.now().toString()
  if (taken.has(id)) {
    id = Date.now().toString()
    while (taken.has(id)) id += 'i'
  }
  const session = {
    id,
    title: (typeof s.title === 'string' && s.title ? s.title : messages.find((m) => m.role === 'user')?.content ?? 'Imported conversation').slice(0, 55),
    timestamp: Number.isFinite(s.timestamp) ? s.timestamp : Date.now(),
    messages,
    provider: s.provider,
    queryType: s.queryType,
  }
  if (typeof s.model === 'string') session.model = s.model
  if (OUTCOME_TEXT[s.lastOutcome]) session.lastOutcome = s.lastOutcome
  return { ok: true, session }
}

/** Add `session` to the front of `sessions`, dropping the oldest beyond `max`. */
export function addSession(sessions, session, max = 30) {
  return [session, ...sessions.filter((s) => s.id !== session.id)].slice(0, max)
}
