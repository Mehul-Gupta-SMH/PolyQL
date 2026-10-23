import React, { useState, useEffect, useCallback } from 'react'
import { DB_TYPES } from '../constants/providerLabels.js'
import { apiFetch } from '../utils/api.js'

/**
 * KnowledgePanel — import / export the context PolyQL retrieves from:
 *   OKF bundles  (concept notes searched during table search)
 *   Apache Ossie (semantic models → tables, joins, glossary metrics)
 */

const s = {
  card: { background: '#fff', border: '1px solid #ddd', borderRadius: 8, padding: '1.25rem', marginBottom: '1.25rem' },
  h: { margin: '0 0 0.25rem', fontSize: '1rem' },
  hint: { margin: '0 0 0.9rem', fontSize: 13, color: '#6b7280' },
  label: { fontWeight: 600, fontSize: 13, marginBottom: 4, display: 'block' },
  input: {
    padding: '0.45rem 0.7rem', fontSize: '0.88rem', border: '1px solid #ccc', borderRadius: 6,
    fontFamily: 'inherit', boxSizing: 'border-box', minWidth: 0,
  },
  textarea: {
    width: '100%', minHeight: 160, padding: '0.6rem 0.75rem', fontSize: '0.82rem', border: '1px solid #ccc',
    borderRadius: 6, resize: 'vertical', fontFamily: 'monospace', boxSizing: 'border-box', marginBottom: '0.75rem',
  },
  row: { display: 'flex', gap: '0.75rem', alignItems: 'flex-end', marginBottom: '0.75rem', flexWrap: 'wrap' },
  field: { display: 'flex', flexDirection: 'column', flex: '1 1 180px', minWidth: 0 },
  btn: (color = '#2563eb', disabled = false) => ({
    padding: '0.5rem 1.1rem', fontSize: '0.88rem', background: disabled ? '#9ca3af' : color, color: '#fff',
    border: 'none', borderRadius: 6, fontWeight: 600, cursor: disabled ? 'default' : 'pointer', flex: '0 0 auto',
  }),
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { textAlign: 'left', padding: '0.4rem 0.5rem', borderBottom: '1px solid #e5e7eb', color: '#6b7280', fontWeight: 600 },
  td: { padding: '0.4rem 0.5rem', borderBottom: '1px solid #f3f4f6' },
  ok: { fontSize: 13, color: '#166534', background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 6, padding: '0.5rem 0.75rem' },
  err: { fontSize: 13, color: '#991b1b', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 6, padding: '0.5rem 0.75rem' },
}

async function errorText(res) {
  try {
    const body = await res.json()
    return body.detail || `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}

async function download(url, fallbackName) {
  const res = await apiFetch(url)
  if (!res.ok) throw new Error(await errorText(res))
  const match = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') || '')
  const href = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = href
  a.download = match ? match[1] : fallbackName
  a.click()
  URL.revokeObjectURL(href)
}

function Status({ status }) {
  if (!status) return null
  return <div style={status.error ? s.err : s.ok}>{status.error || status.message}</div>
}

function OKFSection({ instance }) {
  const [path, setPath] = useState('')
  const [bundleName, setBundleName] = useState('')
  const [bundles, setBundles] = useState([])
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState(null)

  const refresh = useCallback(async () => {
    const res = await apiFetch('/api/okf/bundles')
    if (res.ok) setBundles((await res.json()).bundles ?? [])
  }, [])

  useEffect(() => { refresh().catch(() => {}) }, [refresh])

  const run = async (fn) => {
    setBusy(true)
    setStatus(null)
    try {
      setStatus({ message: await fn() })
    } catch (e) {
      setStatus({ error: e.message })
    } finally {
      setBusy(false)
    }
  }

  const importBundle = () => run(async () => {
    const res = await apiFetch('/api/okf/import', {
      method: 'POST',
      body: JSON.stringify({ path: path.trim(), instance_name: instance, bundle_name: bundleName.trim() || null }),
    })
    if (!res.ok) throw new Error(await errorText(res))
    const r = await res.json()
    await refresh()
    return `Imported "${r.bundle}": ${r.indexed} concepts (${r.table_concepts} describe tables), ` +
      `${r.skipped?.length ?? 0} skipped, ${r.deprecated} deprecated.`
  })

  const removeBundle = (b) => run(async () => {
    const q = new URLSearchParams({ instance_name: b.instance_name, bundle: b.bundle })
    const res = await apiFetch(`/api/okf/bundles?${q}`, { method: 'DELETE' })
    if (!res.ok) throw new Error(await errorText(res))
    const r = await res.json()
    await refresh()
    return `Removed "${b.bundle}" (${r.removed} concepts).`
  })

  const exportBundle = () => run(async () => {
    await download(`/api/okf/export?${new URLSearchParams({ instance_name: instance })}`, `polyql_${instance}.okf.zip`)
    return 'OKF bundle downloaded.'
  })

  return (
    <div style={s.card}>
      <h3 style={s.h}>Open Knowledge Format (OKF)</h3>
      <p style={s.hint}>
        Concept notes from an OKF bundle are searched alongside table descriptions and shown to the model as
        Knowledge Notes. The bundle directory must be readable by the PolyQL server.
      </p>
      <div style={s.row}>
        <div style={{ ...s.field, flex: '3 1 260px' }}>
          <label style={s.label} htmlFor="okf-path">Bundle directory on the server</label>
          <input id="okf-path" style={s.input} value={path} placeholder="/data/okf/sales"
            onChange={(e) => setPath(e.target.value)} />
        </div>
        <div style={s.field}>
          <label style={s.label} htmlFor="okf-name">Bundle name (optional)</label>
          <input id="okf-name" style={s.input} value={bundleName} placeholder="directory name"
            onChange={(e) => setBundleName(e.target.value)} />
        </div>
        <button style={s.btn('#2563eb', busy || !path.trim())} disabled={busy || !path.trim()} onClick={importBundle}>
          Import
        </button>
        <button style={s.btn('#475569', busy)} disabled={busy} onClick={exportBundle}>
          Export instance as OKF
        </button>
      </div>
      <Status status={status} />
      <table style={{ ...s.table, marginTop: '0.75rem' }}>
        <thead>
          <tr><th style={s.th}>Instance</th><th style={s.th}>Bundle</th><th style={s.th}>Concepts</th><th style={s.th} /></tr>
        </thead>
        <tbody>
          {bundles.length === 0 && (
            <tr><td style={{ ...s.td, color: '#9ca3af' }} colSpan={4}>No bundles imported.</td></tr>
          )}
          {bundles.map((b) => (
            <tr key={`${b.instance_name}/${b.bundle}`}>
              <td style={s.td}>{b.instance_name}</td>
              <td style={s.td}>{b.bundle}</td>
              <td style={s.td}>{b.concepts}</td>
              <td style={{ ...s.td, textAlign: 'right' }}>
                <button style={s.btn('#dc2626', busy)} disabled={busy} onClick={() => removeBundle(b)}>Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function OssieSection({ instance }) {
  const [content, setContent] = useState('')
  const [dbType, setDbType] = useState('generic')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState(null)

  const run = async (fn) => {
    setBusy(true)
    setStatus(null)
    try {
      setStatus({ message: await fn() })
    } catch (e) {
      setStatus({ error: e.message })
    } finally {
      setBusy(false)
    }
  }

  const loadFile = async (e) => {
    const file = e.target.files?.[0]
    if (file) setContent(await file.text())
  }

  const importModel = () => run(async () => {
    const res = await apiFetch('/api/semantic/import/ossie', {
      method: 'POST',
      body: JSON.stringify({ content, instance_name: instance, db_type: dbType }),
    })
    if (!res.ok) throw new Error(await errorText(res))
    const r = await res.json()
    return `Imported ${r.models?.length ?? 0} model(s): ${r.tables} tables, ${r.relationships} relationships, ` +
      `${r.metrics} metrics${r.notes ? `, ${r.notes} notes` : ''}${r.skipped?.length ? `, ${r.skipped.length} skipped` : ''}.`
  })

  const exportModel = () => run(async () => {
    await download(`/api/semantic/export/ossie?${new URLSearchParams({ instance_name: instance })}`,
      `polyql_${instance}.ossie.yaml`)
    return 'Ossie model downloaded.'
  })

  return (
    <div style={s.card}>
      <h3 style={s.h}>Apache Ossie semantic model</h3>
      <p style={s.hint}>
        Datasets become tables, relationships become join paths and metrics become glossary terms.
        Re-importing replaces the same tables' metadata.
      </p>
      <div style={s.row}>
        <div style={s.field}>
          <label style={s.label} htmlFor="ossie-file">Load a YAML / JSON file</label>
          <input id="ossie-file" type="file" accept=".yaml,.yml,.json" onChange={loadFile} />
        </div>
        <div style={{ ...s.field, flex: '0 1 180px' }}>
          <label style={s.label} htmlFor="ossie-db">Database type</label>
          <select id="ossie-db" style={s.input} value={dbType} onChange={(e) => setDbType(e.target.value)}>
            {DB_TYPES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
          </select>
        </div>
      </div>
      <label style={s.label} htmlFor="ossie-content">…or paste the document</label>
      <textarea id="ossie-content" style={s.textarea} value={content} placeholder="version: 0.2.0&#10;semantic_model: …"
        onChange={(e) => setContent(e.target.value)} />
      <div style={s.row}>
        <button style={s.btn('#2563eb', busy || !content.trim())} disabled={busy || !content.trim()} onClick={importModel}>
          Import
        </button>
        <button style={s.btn('#475569', busy)} disabled={busy} onClick={exportModel}>
          Export instance as Ossie
        </button>
      </div>
      <Status status={status} />
    </div>
  )
}

export default function KnowledgePanel() {
  const [instanceInput, setInstanceInput] = useState('default')
  const instance = instanceInput.trim() || 'default'
  return (
    <div style={{ maxWidth: 960, margin: '0 auto', padding: '1.25rem' }}>
      <div style={{ ...s.row, marginBottom: '1.25rem' }}>
        <div style={{ ...s.field, flex: '0 1 260px' }}>
          <label style={s.label} htmlFor="knowledge-instance">Instance</label>
          <input id="knowledge-instance" style={s.input} value={instanceInput}
            onChange={(e) => setInstanceInput(e.target.value)} />
        </div>
      </div>
      <OKFSection instance={instance} />
      <OssieSection instance={instance} />
    </div>
  )
}
