import { useState, useEffect } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  MarkerType,
  Handle,
  Position,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from '@dagrejs/dagre'

// ---------------------------------------------------------------------------
// Custom node
// ---------------------------------------------------------------------------
function PathNode({ data }) {
  const isEndpoint = data.role === 'start' || data.role === 'end'
  const bg = data.role === 'start' ? '#2563eb' : data.role === 'end' ? '#059669' : '#374151'
  return (
    <div
      style={{
        background: bg,
        color: '#fff',
        padding: '10px 20px',
        borderRadius: 8,
        fontWeight: 600,
        fontSize: 13,
        fontFamily: 'sans-serif',
        minWidth: 120,
        textAlign: 'center',
        boxShadow: isEndpoint ? `0 0 0 3px ${data.role === 'start' ? '#bfdbfe' : '#a7f3d0'}` : 'none',
        border: isEndpoint ? `2px solid ${data.role === 'start' ? '#93c5fd' : '#6ee7b7'}` : '1px solid #6b7280',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      {data.label}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}

const nodeTypes = { pathNode: PathNode }
const NODE_W = 160
const NODE_H = 44
const joinTypeColors = { '1:1': '#10b981', '1:n': '#2563eb', 'n:1': '#f59e0b', 'n:m': '#94a3b8' }

function buildFlow(pathData, fromTable, toTable, joinTypeFilter = 'all') {
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'LR', nodesep: 60, ranksep: 200 })

  const rfNodes = pathData.path.map((name) => ({
    id: name,
    type: 'pathNode',
    position: { x: 0, y: 0 },
    data: {
      label: name,
      role: name === fromTable.toLowerCase() ? 'start' : name === toTable.toLowerCase() ? 'end' : 'middle',
    },
  }))

  rfNodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }))

  const joinTypeColors = { '1:1': '#10b981', '1:n': '#2563eb', 'n:1': '#f59e0b', 'n:m': '#94a3b8' }
  const normalizedFilter = (joinTypeFilter || 'all').toLowerCase()
  const rfEdges = pathData.edges
    .filter((edge) => {
      if (normalizedFilter === 'all') return true
      const edgeType = (edge.joinType || 'n:m').toLowerCase()
      return edgeType === normalizedFilter
    })
    .map((e, i) => {
    const jt = (e.joinType || 'n:m').toLowerCase()
    const color = joinTypeColors[jt] ?? '#94a3b8'
    const extra = (e.alternatives?.length ?? 0) > 1 ? ` (+${e.alternatives.length - 1} alt)` : ''
    const label = e.joinKeys
      ? `${jt.toUpperCase()} · ${e.joinKeys.length > 50 ? e.joinKeys.slice(0, 47) + '…' : e.joinKeys}${extra}`
      : jt.toUpperCase()
    return {
      id: `e-${i}`,
      source: e.source,
      target: e.target,
      label,
      labelStyle: { fontSize: 11, fill: '#111', fontWeight: 600 },
      labelBgStyle: { fill: '#fff', fillOpacity: 0.95 },
      labelBgPadding: [4, 6],
      labelBgBorderRadius: 4,
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color },
      style: { stroke: color, strokeWidth: 1.7 },
    }
  })

  rfEdges.forEach((e) => g.setEdge(e.source, e.target))
  dagre.layout(g)

  return {
    nodes: rfNodes.map((n) => {
      const pos = g.node(n.id)
      return { ...n, position: { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 } }
    }),
    edges: rfEdges,
  }
}

// ---------------------------------------------------------------------------
// Join conditions list (every alternative per hop + row-multiplication hint)
// ---------------------------------------------------------------------------
function JoinConditions({ edges }) {
  if (!edges?.length) return null
  const fansOut = edges.some((e) => ['1:n', 'n:m'].includes((e.joinType || '').toLowerCase()))
  return (
    <div style={{ marginTop: 12, fontSize: 13, color: '#374151' }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>Join conditions</div>
      <ol style={{ margin: 0, paddingLeft: 20 }}>
        {edges.map((e, i) => {
          const options = e.alternatives?.length ? e.alternatives : [e.joinKeys]
          return (
            <li key={i} style={{ marginBottom: 6 }}>
              <strong>{e.source} → {e.target}</strong>{' '}
              <span style={{ color: '#6b7280' }}>[{(e.joinType || 'n:m').toUpperCase()}]</span>
              {options.length > 1 && (
                <span style={{ color: '#92400e' }}> — {options.length} ways to join; pick the one the question needs</span>
              )}
              <ul style={{ margin: '2px 0 0', paddingLeft: 18 }}>
                {options.map((opt) => <li key={opt}><code>{opt}</code></li>)}
              </ul>
            </li>
          )
        })}
      </ol>
      {fansOut && (
        <div style={{ marginTop: 6, color: '#92400e' }}>
          1:N and N:M joins repeat rows — aggregate before joining or use COUNT(DISTINCT …).
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Derivative tables panel
// ---------------------------------------------------------------------------
function DerivativesPanel({ table, instance }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!table) { setData(null); return }
    setLoading(true)
    fetch(`/api/derivatives/${encodeURIComponent(table)}?instance_name=${encodeURIComponent(instance)}`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [table, instance])

  if (!table) return null
  if (loading) return <p style={{ color: '#6b7280', fontSize: 13 }}>Loading…</p>
  if (!data) return null

  const hasContent = data.is_derived || data.derived_tables.length > 0 || data.parent_tables.length > 0
  if (!hasContent) return (
    <p style={{ color: '#6b7280', fontSize: 13 }}>No derivative relationships found for <strong>{table}</strong>.</p>
  )

  return (
    <div style={{ fontSize: 13 }}>
      {data.is_derived && (
        <div style={{ marginBottom: 10, padding: '6px 12px', background: '#fef9c3', border: '1px solid #fde68a', borderRadius: 6, color: '#92400e' }}>
          <strong>{table}</strong> is a pipeline-derived table.
        </div>
      )}
      {data.parent_tables.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontWeight: 600, marginBottom: 4, color: '#374151' }}>Parent tables (sources):</div>
          {data.parent_tables.map((t) => (
            <div key={t.name} style={{ display: 'flex', gap: 8, padding: '4px 0', borderBottom: '1px solid #f3f4f6' }}>
              <span style={{ fontWeight: 600, color: '#2563eb' }}>{t.name}</span>
              {t.description && <span style={{ color: '#6b7280' }}>— {t.description}</span>}
            </div>
          ))}
        </div>
      )}
      {data.derived_tables.length > 0 && (
        <div>
          <div style={{ fontWeight: 600, marginBottom: 4, color: '#374151' }}>Derived tables (outputs):</div>
          {data.derived_tables.map((t) => (
            <div key={t.name} style={{ display: 'flex', gap: 8, padding: '4px 0', borderBottom: '1px solid #f3f4f6' }}>
              <span style={{ fontWeight: 600, color: '#059669' }}>{t.name}</span>
              {t.description && <span style={{ color: '#6b7280' }}>— {t.description}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Legend
// ---------------------------------------------------------------------------
function Legend() {
  return (
    <div style={{
      position: 'absolute', bottom: 16, left: 16,
      background: 'rgba(255,255,255,0.92)', border: '1px solid #e5e7eb',
      borderRadius: 8, padding: '8px 14px', fontSize: 12, zIndex: 10,
      display: 'flex', gap: 16, alignItems: 'center',
    }}>
      {[
        { color: '#2563eb', label: 'From table' },
        { color: '#059669', label: 'To table' },
        { color: '#374151', label: 'Bridge table' },
      ].map(({ color, label }) => (
        <span key={label} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ background: color, borderRadius: 4, width: 14, height: 14, display: 'inline-block' }} />
          {label}
        </span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export default function DataLineage({ tables }) {
  const [fromTable, setFromTable] = useState('')
  const [toTable, setToTable] = useState('')
  const [instance, setInstance] = useState('default')
  const [instances, setInstances] = useState([{ instance_name: 'default', db_type: 'generic' }])
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [noPath, setNoPath] = useState(false)
  const [pathEdges, setPathEdges] = useState([])
  const [derivativeTable, setDerivativeTable] = useState('')
  const [activeTab, setActiveTab] = useState('joinpath')

  useEffect(() => {
    fetch('/api/instances')
      .then((r) => r.json())
      .then((d) => { if (d.instances?.length) setInstances(d.instances) })
      .catch(() => {})
  }, [])

  async function findPath() {
    if (!fromTable || !toTable) return
    setLoading(true)
    setError('')
    setNoPath(false)
    setNodes([])
    setEdges([])
    setPathEdges([])
    try {
      const res = await fetch(
        `/api/joinpath?from_table=${encodeURIComponent(fromTable)}&to_table=${encodeURIComponent(toTable)}&instance_name=${encodeURIComponent(instance)}`
      )
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Request failed.'); return }
      if (!data.found || data.path.length === 0) { setNoPath(true); return }
      if (data.path.length === 1) { setError('Select two different tables.'); return }
      const { nodes: n, edges: e } = buildFlow(data, fromTable, toTable)
      setNodes(n)
      setEdges(e)
      setPathEdges(data.edges ?? [])
    } catch {
      setError('Network error — is the backend running?')
    } finally {
      setLoading(false)
    }
  }

  function handleInstanceChange(val) {
    setInstance(val)
    setFromTable('')
    setToTable('')
    setNodes([])
    setEdges([])
    setPathEdges([])
    setNoPath(false)
    setError('')
    setDerivativeTable('')
  }

  const selectStyle = {
    padding: '0.45rem 0.7rem', fontSize: '0.9rem',
    border: '1px solid #ccc', borderRadius: 6, background: '#fff', minWidth: 180,
  }

  const tabBtn = (id, label) => (
    <button
      key={id}
      onClick={() => setActiveTab(id)}
      style={{
        padding: '6px 14px', border: 'none', cursor: 'pointer', fontSize: 13,
        background: activeTab === id ? '#eff6ff' : 'transparent',
        color: activeTab === id ? '#2563eb' : '#555',
        borderBottom: activeTab === id ? '2px solid #2563eb' : '2px solid transparent',
        fontWeight: activeTab === id ? 600 : 400,
      }}
    >
      {label}
    </button>
  )

  return (
    <div>
      {/* Sub-tabs */}
      <div style={{ display: 'flex', borderBottom: '1px solid #e5e7eb', marginBottom: '1rem' }}>
        {tabBtn('joinpath', 'Join Path')}
        {tabBtn('derivatives', 'Derivative Tables')}
      </div>

      {/* Instance selector (shared) */}
      <div style={{
        background: '#fff', border: '1px solid #ddd', borderRadius: 8,
        padding: '0.75rem 1.25rem', marginBottom: '1.25rem',
        display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap',
      }}>
        <label style={{ fontWeight: 600, fontSize: 14 }}>Instance</label>
        <select style={selectStyle} value={instance} onChange={(e) => handleInstanceChange(e.target.value)}>
          {instances.map((i) => (
            <option key={i.instance_name} value={i.instance_name}>
              {i.instance_name}{i.db_type !== 'generic' ? ` (${i.db_type})` : ''}
            </option>
          ))}
        </select>
      </div>

      {/* ---- JOIN PATH TAB ---- */}
      {activeTab === 'joinpath' && (
        <>
          <div style={{
            background: '#fff', border: '1px solid #ddd', borderRadius: 8,
            padding: '0.75rem 1.25rem', marginBottom: '1.25rem',
            display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap',
          }}>
            <label style={{ fontWeight: 600, fontSize: 14 }}>From</label>
            <select style={selectStyle} value={fromTable} onChange={(e) => setFromTable(e.target.value)} disabled={loading}>
              <option value="">— table A —</option>
              {tables.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <label style={{ fontWeight: 600, fontSize: 14 }}>To</label>
            <select style={selectStyle} value={toTable} onChange={(e) => setToTable(e.target.value)} disabled={loading}>
              <option value="">— table B —</option>
              {tables.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <button
              onClick={findPath}
              disabled={!fromTable || !toTable || loading}
              style={{
                padding: '0.45rem 1.1rem', fontSize: '0.9rem', border: 'none',
                borderRadius: 6, cursor: !fromTable || !toTable || loading ? 'not-allowed' : 'pointer',
                background: !fromTable || !toTable || loading ? '#e5e7eb' : '#2563eb',
                color: !fromTable || !toTable || loading ? '#9ca3af' : '#fff',
                fontWeight: 600,
              }}
            >
              {loading ? 'Searching…' : 'Find Path'}
            </button>
          </div>

          {error && (
            <div style={{ padding: '0.75rem 1rem', background: '#fee2e2', border: '1px solid #fca5a5', borderRadius: 6, color: '#991b1b', marginBottom: '1rem', fontSize: 13 }}>
              {error}
            </div>
          )}

          {noPath && (
            <div style={{ padding: '0.75rem 1rem', background: '#fffbeb', border: '1px solid #fcd34d', borderRadius: 6, color: '#92400e', fontSize: 13 }}>
              No join path found between <strong>{fromTable}</strong> and <strong>{toTable}</strong>. Make sure relationships are defined between these tables.
            </div>
          )}

          {nodes.length > 0 && (
            <>
              <div style={{ marginBottom: 10, fontSize: 13, color: '#374151' }}>
                Path: <strong>{nodes.map((n) => n.id).join(' → ')}</strong>
                <span style={{ marginLeft: 12, color: '#6b7280' }}>({nodes.length} tables, {edges.length} joins)</span>
              </div>
              <div style={{ height: 'calc(100vh - 360px)', border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden', position: 'relative' }}>
                <ReactFlow
                  nodes={nodes} edges={edges}
                  onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
                  nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.3 }}
                >
                  <Background />
                  <Controls />
                  <MiniMap />
                  <Legend />
                </ReactFlow>
              </div>
              <JoinConditions edges={pathEdges} />
            </>
          )}

          {!fromTable && !toTable && !loading && (
            <div style={{ textAlign: 'center', padding: 60, color: '#9ca3af', fontSize: 14 }}>
              Select two tables above and click <strong>Find Path</strong> to see the shortest JOIN route between them.
            </div>
          )}
        </>
      )}

      {/* ---- DERIVATIVE TABLES TAB ---- */}
      {activeTab === 'derivatives' && (
        <>
          <div style={{
            background: '#fff', border: '1px solid #ddd', borderRadius: 8,
            padding: '0.75rem 1.25rem', marginBottom: '1.25rem',
            display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap',
          }}>
            <label style={{ fontWeight: 600, fontSize: 14 }}>Table</label>
            <select style={selectStyle} value={derivativeTable} onChange={(e) => setDerivativeTable(e.target.value)}>
              <option value="">— choose table —</option>
              {tables.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>

          {derivativeTable && (
            <div style={{ background: '#fff', border: '1px solid #ddd', borderRadius: 8, padding: '1rem 1.25rem' }}>
              <DerivativesPanel table={derivativeTable} instance={instance} />
            </div>
          )}

          {!derivativeTable && (
            <div style={{ textAlign: 'center', padding: 60, color: '#9ca3af', fontSize: 14 }}>
              Select a table to see its parent and derived (pipeline) relationships.
            </div>
          )}
        </>
      )}
    </div>
  )
}
