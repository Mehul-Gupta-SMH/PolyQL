import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { apiFetch } from '../utils/api.js'
import { parseGlossaryFile, splitList } from '../utils/glossaryImport.js'

/**
 * BusinessGlossary — SL4: browse, add, edit, delete and bulk-import business
 * terms. Matching terms reach the prompt as "Business Definitions", so every
 * save re-embeds the term; the API reports when embedding wasn't possible.
 */

const s = {
  page: { maxWidth: 1100, margin: '0 auto', padding: '1.25rem' },
  card: { background: '#fff', border: '1px solid #ddd', borderRadius: 8, padding: '1.25rem', marginBottom: '1.25rem' },
  h: { margin: '0 0 0.75rem', fontSize: '1rem' },
  label: { fontWeight: 600, fontSize: 13, marginBottom: 4, display: 'block' },
  input: {
    padding: '0.45rem 0.7rem', fontSize: '0.88rem', border: '1px solid #ccc', borderRadius: 6,
    fontFamily: 'inherit', boxSizing: 'border-box', width: '100%', minWidth: 0,
  },
  textarea: {
    padding: '0.5rem 0.7rem', fontSize: '0.85rem', border: '1px solid #ccc', borderRadius: 6, width: '100%',
    boxSizing: 'border-box', resize: 'vertical', fontFamily: 'inherit', minHeight: 60,
  },
  row: { display: 'flex', gap: '0.75rem', alignItems: 'flex-end', marginBottom: '0.75rem', flexWrap: 'wrap' },
  field: (basis = 200) => ({ display: 'flex', flexDirection: 'column', flex: `1 1 ${basis}px`, minWidth: 0 }),
  btn: (color = '#2563eb', disabled = false) => ({
    padding: '0.45rem 1rem', fontSize: '0.85rem', background: disabled ? '#9ca3af' : color, color: '#fff',
    border: 'none', borderRadius: 6, fontWeight: 600, cursor: disabled ? 'default' : 'pointer', flex: '0 0 auto',
  }),
  link: (color = '#2563eb') => ({
    background: 'none', border: 'none', color, cursor: 'pointer', fontSize: 13, padding: '0 0.25rem', fontWeight: 600,
  }),
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { textAlign: 'left', padding: '0.45rem 0.5rem', borderBottom: '1px solid #e5e7eb', color: '#6b7280', fontWeight: 600 },
  td: { padding: '0.45rem 0.5rem', borderBottom: '1px solid #f3f4f6', verticalAlign: 'top' },
  mono: { fontFamily: 'monospace', fontSize: 12, background: '#f8fafc', padding: '1px 4px', borderRadius: 4 },
  chip: {
    display: 'inline-flex', alignItems: 'center', gap: 4, background: '#eff6ff', color: '#1e40af',
    borderRadius: 12, padding: '1px 8px', fontSize: 12, margin: '0 4px 4px 0',
  },
  ok: { fontSize: 13, color: '#166534', background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 6, padding: '0.5rem 0.75rem', marginBottom: '1rem' },
  warn: { fontSize: 13, color: '#92400e', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: 6, padding: '0.5rem 0.75rem', marginBottom: '1rem' },
  err: { fontSize: 13, color: '#991b1b', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 6, padding: '0.5rem 0.75rem', marginBottom: '1rem' },
}

const EMPTY = {
  term_name: '', full_name: '', definition: '', formula: '', formula_type: '', table_deps: [],
  column_deps: [], synonyms: [], example_value: '', domain: '', instance_name: '',
}
const NOT_INDEXED = 'Saved, but not embedded for search (embedding model unavailable) — use Re-index once it is.'

async function errorText(res) {
  try {
    const body = await res.json()
    return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch {
    return `HTTP ${res.status}`
  }
}

/** Free-text list input: Enter or comma adds a chip; suggestions from a datalist. */
function ChipInput({ id, value, onChange, suggestions = [], placeholder }) {
  const [draft, setDraft] = useState('')
  const add = (text) => {
    const items = splitList(text.replace(/,/g, ';')).filter((v) => !value.includes(v))
    if (items.length) onChange([...value, ...items])
    setDraft('')
  }
  return (
    <div>
      <div>
        {value.map((v) => (
          <span key={v} style={s.chip}>
            {v}
            <button type="button" aria-label={`Remove ${v}`} style={{ ...s.link('#1e40af'), padding: 0 }}
              onClick={() => onChange(value.filter((x) => x !== v))}>×</button>
          </span>
        ))}
      </div>
      <input id={id} style={s.input} value={draft} placeholder={placeholder} list={suggestions.length ? `${id}-list` : undefined}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if ((e.key === 'Enter' || e.key === ',') && draft.trim()) { e.preventDefault(); add(draft) }
        }}
        onBlur={() => draft.trim() && add(draft)} />
      {suggestions.length > 0 && (
        <datalist id={`${id}-list`}>{suggestions.map((t) => <option key={t} value={t} />)}</datalist>
      )}
    </div>
  )
}

function TermForm({ initial, tables, domains, onSave, onCancel, busy }) {
  const [term, setTerm] = useState(initial)
  const set = (key) => (e) => setTerm({ ...term, [key]: e.target ? e.target.value : e })
  const editing = Boolean(initial.term_id)

  return (
    <div style={s.card}>
      <h3 style={s.h}>{editing ? `Edit “${initial.term_name}”` : 'Add a term'}</h3>
      <div style={s.row}>
        <div style={s.field()}>
          <label style={s.label} htmlFor="g-name">Term name *</label>
          <input id="g-name" style={s.input} value={term.term_name} onChange={set('term_name')} placeholder="Net Revenue" />
        </div>
        <div style={s.field()}>
          <label style={s.label} htmlFor="g-full">Full name</label>
          <input id="g-full" style={s.input} value={term.full_name} onChange={set('full_name')} />
        </div>
        <div style={s.field(140)}>
          <label style={s.label} htmlFor="g-domain">Domain</label>
          <input id="g-domain" style={s.input} value={term.domain} onChange={set('domain')} list="g-domain-list" placeholder="finance" />
          <datalist id="g-domain-list">{domains.map((d) => <option key={d} value={d} />)}</datalist>
        </div>
        <div style={s.field(140)}>
          <label style={s.label} htmlFor="g-instance">Instance</label>
          <input id="g-instance" style={s.input} value={term.instance_name} onChange={set('instance_name')} placeholder="default" />
        </div>
      </div>
      <div style={{ marginBottom: '0.75rem' }}>
        <label style={s.label} htmlFor="g-def">Definition</label>
        <textarea id="g-def" style={s.textarea} value={term.definition} onChange={set('definition')} />
      </div>
      <div style={s.row}>
        <div style={s.field(320)}>
          <label style={s.label} htmlFor="g-formula">Formula (SQL)</label>
          <textarea id="g-formula" style={{ ...s.textarea, fontFamily: 'monospace' }} value={term.formula}
            onChange={set('formula')} placeholder="SUM(orders.total) - SUM(refunds.amount)" />
        </div>
        <div style={s.field(140)}>
          <label style={s.label} htmlFor="g-ftype">Formula type</label>
          <input id="g-ftype" style={s.input} value={term.formula_type} onChange={set('formula_type')}
            list="g-ftype-list" placeholder="aggregation" />
          <datalist id="g-ftype-list">
            {['aggregation', 'ratio', 'filter', 'derived', 'count'].map((t) => <option key={t} value={t} />)}
          </datalist>
        </div>
      </div>
      <div style={s.row}>
        <div style={s.field()}>
          <label style={s.label} htmlFor="g-tables">Tables used</label>
          <ChipInput id="g-tables" value={term.table_deps} onChange={set('table_deps')} suggestions={tables}
            placeholder="type or pick a table, Enter to add" />
        </div>
        <div style={s.field()}>
          <label style={s.label} htmlFor="g-cols">Columns used</label>
          <ChipInput id="g-cols" value={term.column_deps} onChange={set('column_deps')} placeholder="orders.total" />
        </div>
        <div style={s.field()}>
          <label style={s.label} htmlFor="g-syn">Synonyms</label>
          <ChipInput id="g-syn" value={term.synonyms} onChange={set('synonyms')} placeholder="net sales" />
        </div>
      </div>
      <div style={{ marginBottom: '0.75rem' }}>
        <label style={s.label} htmlFor="g-example">Example value</label>
        <input id="g-example" style={s.input} value={term.example_value} onChange={set('example_value')} />
      </div>
      <div style={{ display: 'flex', gap: '0.5rem' }}>
        <button style={s.btn('#2563eb', busy || !term.term_name.trim())} disabled={busy || !term.term_name.trim()}
          onClick={() => onSave(term)}>{editing ? 'Save changes' : 'Add term'}</button>
        <button style={s.btn('#6b7280', busy)} disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

function ImportPanel({ defaultInstance, onImported, onCancel }) {
  const [text, setText] = useState('')
  const [instance, setInstance] = useState(defaultInstance)
  const [upsert, setUpsert] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const parsed = useMemo(() => (text.trim() ? parseGlossaryFile(text, instance.trim()) : null), [text, instance])

  const loadFile = async (e) => {
    const file = e.target.files?.[0]
    if (file) setText(await file.text())
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await apiFetch(`/api/glossary/terms/bulk?upsert=${upsert}`, {
        method: 'POST', body: JSON.stringify(parsed.terms),
      })
      if (!res.ok) throw new Error(await errorText(res))
      onImported(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={s.card}>
      <h3 style={s.h}>Import terms</h3>
      <p style={{ margin: '0 0 0.75rem', fontSize: 13, color: '#6b7280' }}>
        CSV with a header row (<code>term_name</code> required; also <code>full_name, definition, formula,
        formula_type, table_deps, column_deps, synonyms, example_value, domain, instance_name</code> — separate list
        values with <code>;</code>), or a JSON array of terms.
      </p>
      <div style={s.row}>
        <div style={s.field()}>
          <label style={s.label} htmlFor="g-import-file">File</label>
          <input id="g-import-file" type="file" accept=".csv,.json,text/csv,application/json" onChange={loadFile} />
        </div>
        <div style={s.field(160)}>
          <label style={s.label} htmlFor="g-import-instance">Instance for rows without one</label>
          <input id="g-import-instance" style={s.input} value={instance} onChange={(e) => setInstance(e.target.value)}
            placeholder="default" />
        </div>
        <label style={{ fontSize: 13, display: 'flex', gap: 6, alignItems: 'center', flex: '0 0 auto' }}>
          <input type="checkbox" checked={upsert} onChange={(e) => setUpsert(e.target.checked)} />
          Update terms that already exist (by name)
        </label>
      </div>
      <label style={s.label} htmlFor="g-import-text">…or paste</label>
      <textarea id="g-import-text" style={{ ...s.textarea, minHeight: 120, fontFamily: 'monospace' }} value={text}
        onChange={(e) => setText(e.target.value)} placeholder={'term_name,formula,table_deps\nNet Revenue,SUM(orders.total),orders'} />
      {parsed && (
        <div style={parsed.errors.length ? s.warn : s.ok}>
          {parsed.terms.length} term{parsed.terms.length === 1 ? '' : 's'} ready to import.
          {parsed.errors.map((e) => <div key={e}>{e}</div>)}
        </div>
      )}
      {error && <div style={s.err}>{error}</div>}
      <div style={{ display: 'flex', gap: '0.5rem' }}>
        <button style={s.btn('#2563eb', busy || !parsed?.terms.length)} disabled={busy || !parsed?.terms.length}
          onClick={submit}>Import</button>
        <button style={s.btn('#6b7280', busy)} disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

export default function BusinessGlossary() {
  const [terms, setTerms] = useState([])
  const [tables, setTables] = useState([])
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [instanceFilter, setInstanceFilter] = useState('__all__')
  const [domainFilter, setDomainFilter] = useState('__all__')
  const [editing, setEditing] = useState(null)       // term being added/edited
  const [importing, setImporting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)         // {kind: ok|warn|err, text}

  const refresh = useCallback(async () => {
    const res = await apiFetch('/api/glossary/terms')
    if (!res.ok) throw new Error(await errorText(res))
    setTerms((await res.json()).terms ?? [])
  }, [])

  useEffect(() => {
    refresh().catch((e) => setNotice({ kind: 'err', text: `Could not load terms: ${e.message}` }))
      .finally(() => setLoading(false))
    apiFetch('/api/schema').then((r) => (r.ok ? r.json() : { tables: [] }))
      .then((d) => setTables((d.tables ?? []).map((t) => t.name).filter(Boolean)))
      .catch(() => {})
  }, [refresh])

  const instances = useMemo(() => [...new Set(terms.map((t) => t.instance_name || ''))].sort(), [terms])
  const domains = useMemo(() => [...new Set(terms.map((t) => t.domain).filter(Boolean))].sort(), [terms])

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase()
    return terms
      .filter((t) => instanceFilter === '__all__' || (t.instance_name || '') === instanceFilter)
      .filter((t) => domainFilter === '__all__' || (t.domain || '') === domainFilter)
      .filter((t) => !q || [t.term_name, t.full_name, t.definition, t.formula, ...(t.synonyms || [])]
        .some((v) => (v || '').toLowerCase().includes(q)))
      .sort((a, b) => a.term_name.localeCompare(b.term_name))
  }, [terms, query, instanceFilter, domainFilter])

  const act = async (fn) => {
    setBusy(true)
    setNotice(null)
    try {
      const result = await fn()
      await refresh()
      if (result) setNotice(result)
    } catch (e) {
      setNotice({ kind: 'err', text: e.message })
    } finally {
      setBusy(false)
    }
  }

  const save = (term) => act(async () => {
    const body = { ...term }
    delete body.term_id; delete body.created_at; delete body.updated_at
    const res = term.term_id
      ? await apiFetch(`/api/glossary/terms/${term.term_id}`, { method: 'PUT', body: JSON.stringify(body) })
      : await apiFetch('/api/glossary/terms/single', { method: 'POST', body: JSON.stringify(body) })
    if (!res.ok) throw new Error(await errorText(res))
    const r = await res.json()
    setEditing(null)
    return r.indexed === false ? { kind: 'warn', text: NOT_INDEXED } : { kind: 'ok', text: `Saved “${term.term_name}”.` }
  })

  const remove = (term) => {
    if (!window.confirm(`Delete “${term.term_name}”? It will no longer be used in generated SQL.`)) return
    act(async () => {
      const res = await apiFetch(`/api/glossary/terms/${term.term_id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await errorText(res))
      if (editing?.term_id === term.term_id) setEditing(null)
      return { kind: 'ok', text: `Deleted “${term.term_name}”.` }
    })
  }

  const reindex = () => act(async () => {
    const res = await apiFetch('/api/glossary/reindex', { method: 'POST' })
    if (!res.ok) throw new Error(await errorText(res))
    return { kind: 'ok', text: `Re-indexed ${(await res.json()).indexed} terms.` }
  })

  const exportJSON = () => {
    const blob = new Blob([JSON.stringify(visible, null, 2)], { type: 'application/json' })
    const href = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = href
    a.download = 'glossary.json'
    a.click()
    URL.revokeObjectURL(href)
  }

  const onImported = (r) => act(async () => {
    setImporting(false)
    const text = `Imported ${r.created.length} new and updated ${r.updated.length} existing terms.`
    return r.indexed === false ? { kind: 'warn', text: `${text} ${NOT_INDEXED}` } : { kind: 'ok', text }
  })

  return (
    <div style={s.page}>
      {notice && <div style={s[notice.kind]} role="status">{notice.text}</div>}

      {editing && (
        <TermForm key={editing.term_id || 'new'} initial={editing} tables={tables} domains={domains} busy={busy}
          onSave={save} onCancel={() => setEditing(null)} />
      )}
      {importing && (
        <ImportPanel defaultInstance={instanceFilter === '__all__' ? '' : instanceFilter}
          onImported={onImported} onCancel={() => setImporting(false)} />
      )}

      <div style={s.card}>
        <div style={s.row}>
          <div style={s.field(240)}>
            <label style={s.label} htmlFor="g-search">Search</label>
            <input id="g-search" style={s.input} value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="name, definition, formula or synonym" />
          </div>
          <div style={s.field(150)}>
            <label style={s.label} htmlFor="g-filter-instance">Instance</label>
            <select id="g-filter-instance" style={s.input} value={instanceFilter} onChange={(e) => setInstanceFilter(e.target.value)}>
              <option value="__all__">All</option>
              {instances.map((i) => <option key={i} value={i}>{i || 'default'}</option>)}
            </select>
          </div>
          <div style={s.field(150)}>
            <label style={s.label} htmlFor="g-filter-domain">Domain</label>
            <select id="g-filter-domain" style={s.input} value={domainFilter} onChange={(e) => setDomainFilter(e.target.value)}>
              <option value="__all__">All</option>
              {domains.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </div>
          <button style={s.btn('#2563eb', busy)} disabled={busy}
            onClick={() => { setImporting(false); setEditing({ ...EMPTY, instance_name: instanceFilter === '__all__' ? '' : instanceFilter }) }}>
            Add term
          </button>
          <button style={s.btn('#475569', busy)} disabled={busy} onClick={() => { setEditing(null); setImporting(true) }}>Import</button>
          <button style={s.btn('#475569', busy || !visible.length)} disabled={busy || !visible.length} onClick={exportJSON}>Export</button>
          <button style={s.btn('#475569', busy || !terms.length)} disabled={busy || !terms.length} onClick={reindex}
            title="Re-embed every term for semantic search">Re-index</button>
        </div>

        <table style={s.table}>
          <thead>
            <tr>
              <th style={s.th}>Term</th><th style={s.th}>Definition</th><th style={s.th}>Formula</th>
              <th style={s.th}>Tables</th><th style={s.th}>Domain</th><th style={s.th}>Instance</th><th style={s.th} />
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td style={s.td} colSpan={7}>Loading…</td></tr>}
            {!loading && visible.length === 0 && (
              <tr><td style={{ ...s.td, color: '#9ca3af' }} colSpan={7}>
                {terms.length ? 'No terms match the filters.' : 'No terms yet — add one or import a CSV/JSON file.'}
              </td></tr>
            )}
            {visible.map((t) => (
              <tr key={t.term_id}>
                <td style={s.td}>
                  <div style={{ fontWeight: 600 }}>{t.term_name}</div>
                  {t.full_name && <div style={{ color: '#6b7280' }}>{t.full_name}</div>}
                  {(t.synonyms || []).length > 0 && <div style={{ color: '#6b7280', fontSize: 12 }}>aka {t.synonyms.join(', ')}</div>}
                </td>
                <td style={{ ...s.td, maxWidth: 320 }}>{t.definition}</td>
                <td style={s.td}>{t.formula && <code style={s.mono}>{t.formula}</code>}</td>
                <td style={s.td}>{(t.table_deps || []).map((d) => <span key={d} style={s.chip}>{d}</span>)}</td>
                <td style={s.td}>{t.domain}</td>
                <td style={s.td}>{t.instance_name || <span style={{ color: '#9ca3af' }}>default</span>}</td>
                <td style={{ ...s.td, whiteSpace: 'nowrap', textAlign: 'right' }}>
                  <button style={s.link()} disabled={busy}
                    onClick={() => { setImporting(false); setEditing({ ...EMPTY, ...t }) }}>Edit</button>
                  <button style={s.link('#dc2626')} disabled={busy} onClick={() => remove(t)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && terms.length > 0 && (
          <div style={{ fontSize: 12, color: '#6b7280', marginTop: '0.5rem' }}>
            {visible.length} of {terms.length} terms
          </div>
        )}
      </div>
    </div>
  )
}
