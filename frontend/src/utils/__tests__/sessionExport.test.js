import { toJSON, toMarkdown, parseImport, addSession, exportFilename } from '../sessionExport.js'

const SESSION = {
  id: 's1', title: 'Top customers', timestamp: 1700000000000, provider: 'open_ai', model: 'gpt-4o', queryType: 'sql',
  lastOutcome: 'success',
  messages: [
    { id: 1, role: 'user', type: 'text', content: 'top customers' },
    { id: 2, role: 'assistant', type: 'clarify', content: 'By revenue?', options: ['Yes', 'No'] },
    { id: 3, role: 'user', type: 'text', content: 'Yes' },
    { id: 4, role: 'assistant', type: 'sql', content: 'SELECT 1', queryType: 'sql' },
    { id: 5, role: 'assistant', type: 'error', content: 'boom' },
  ],
}

describe('toJSON / parseImport', () => {
  it('round-trips with full fidelity', () => {
    const r = parseImport(toJSON(SESSION), [])
    expect(r.ok).toBe(true)
    expect(r.session.id).toBe('s1')
    expect(r.session.model).toBe('gpt-4o')
    expect(r.session.lastOutcome).toBe('success')
    expect(r.session.messages).toHaveLength(5)
    expect(r.session.messages[1].options).toEqual(['Yes', 'No'])
    expect(JSON.parse(toJSON(SESSION))).toMatchObject({ format: 'polyql-session', version: 1 })
  })
  it('gives a new id on clash', () => {
    const r = parseImport(toJSON(SESSION), ['s1'])
    expect(r.ok).toBe(true)
    expect(r.session.id).not.toBe('s1')
  })
  it.each([
    ['', 'empty'],
    ['{nope', 'Not valid JSON'],
    ['{"a":1}', 'Not a Poly-QL session export'],
    [JSON.stringify({ format: 'polyql-session', version: 2, session: {} }), 'Unsupported'],
    [JSON.stringify({ format: 'polyql-session', version: 1, session: { messages: 'x' } }), 'messages list'],
    [JSON.stringify({ format: 'polyql-session', version: 1, session: { provider: 'p', queryType: 'sql', messages: [{ role: 'bot', content: 'x' }] } }), 'malformed'],
    [JSON.stringify({ format: 'polyql-session', version: 1, session: { provider: 'p', queryType: 'sql', messages: [] } }), 'no messages'],
  ])('rejects bad input %#', (text, msg) => {
    const r = parseImport(text, [])
    expect(r.ok).toBe(false)
    expect(r.error).toContain(msg)
  })
  it('turns a leftover streaming message into a stopped one', () => {
    const s = { ...SESSION, messages: [{ role: 'assistant', type: 'streaming', content: 'SEL' }] }
    expect(parseImport(toJSON(s), []).session.messages[0].type).toBe('stopped')
  })
})

describe('toMarkdown', () => {
  it('renders a readable transcript', () => {
    const md = toMarkdown(SESSION)
    expect(md).toContain('# Top customers')
    expect(md).toContain('**User:**\n\ntop customers')
    expect(md).toContain('By revenue?')
    expect(md).toContain('- Yes')
    expect(md).toContain('```sql\nSELECT 1\n```')
    expect(md).toContain('**Error:** boom')
    expect(md).toContain('✓ Last execution succeeded')
  })
})

describe('addSession / exportFilename', () => {
  it('drops the oldest beyond max and replaces same id', () => {
    const list = [{ id: 'a' }, { id: 'b' }, { id: 'c' }]
    expect(addSession(list, { id: 'n' }, 3).map((s) => s.id)).toEqual(['n', 'a', 'b'])
    expect(addSession(list, { id: 'b' }, 3).map((s) => s.id)).toEqual(['b', 'a', 'c'])
  })
  it('makes safe filenames', () => {
    expect(exportFilename({ title: 'Top: customers/2024?' }, 'json')).toBe('polyql-top-customers-2024.json')
  })
})
