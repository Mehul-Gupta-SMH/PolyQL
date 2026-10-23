import { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
  ReactFlowProvider,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from '@dagrejs/dagre'
import TableNode from './TableNode.jsx'
import TermNode from './TermNode.jsx'
import RelationEdge from './RelationEdge.jsx'
import { apiFetch } from '../utils/api.js'
import {
  NODE_WIDTH, EDGE_COLORS, tableNodes, relationEdges, termElements, termsForTable, termInInstance,
} from '../utils/erdModel.js'

const nodeTypes = { tableNode: TableNode, termNode: TermNode }
const edgeTypes = { relation: RelationEdge }

function getLayoutedElements(nodes, edges) {
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'LR', nodesep: 60, ranksep: 220 })

  nodes.forEach((node) => {
    const colCount = node.data.columns.length
    g.setNode(node.id, { width: NODE_WIDTH, height: 60 + colCount * 28 })
  })

  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target)
  })

  dagre.layout(g)

  const layoutedNodes = nodes.map((node) => {
    const pos = g.node(node.id)
    return {
      ...node,
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - (60 + node.data.columns.length * 28) / 2 },
    }
  })

  return { nodes: layoutedNodes, edges }
}

const paneStyle = {
  width: 360,
  flexShrink: 0,
  background: '#fff',
  borderLeft: '1px solid #e5e7eb',
  display: 'flex',
  flexDirection: 'column',
  overflow: 'hidden',
}

function PaneHeader({ title, onClose, color = '#1e1e2e' }) {
  return (
    <div
      style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        padding: '12px 16px', background: color, color: '#fff', flexShrink: 0,
      }}
    >
      <span style={{ fontWeight: 700, fontSize: 14 }}>{title}</span>
      <button
        onClick={onClose}
        style={{ background: 'none', border: 'none', color: '#cdd6f4', fontSize: 18, cursor: 'pointer', lineHeight: 1, padding: '0 2px' }}
        title="Close"
      >
        ×
      </button>
    </div>
  )
}

const mono = { fontFamily: 'monospace', fontSize: 12, background: '#f3f4f6', borderRadius: 4, padding: '2px 6px', display: 'block', margin: '4px 0', wordBreak: 'break-all' }
const heading = { margin: '14px 0 4px', fontSize: 12, fontWeight: 700, color: '#374151' }

function TermDetailPane({ term, tables, onClose }) {
  return (
    <div style={paneStyle}>
      <PaneHeader title={term.term_name} onClose={onClose} color="#5b21b6" />
      <div style={{ overflowY: 'auto', padding: '12px 16px', flex: 1, fontSize: 13, color: '#374151' }}>
        {term.full_name && <p style={{ margin: '0 0 8px', fontWeight: 600 }}>{term.full_name}</p>}
        <h4 style={{ ...heading, marginTop: 0 }}>Definition</h4>
        <p style={{ margin: 0 }}>{term.definition || '—'}</p>
        {term.formula && (
          <>
            <h4 style={heading}>Formula</h4>
            <code style={mono}>{term.formula}</code>
          </>
        )}
        <h4 style={heading}>Tables</h4>
        <p style={{ margin: 0 }}>{tables.join(', ')}</p>
        {term.synonyms?.length > 0 && (
          <>
            <h4 style={heading}>Synonyms</h4>
            <p style={{ margin: 0 }}>{term.synonyms.join(', ')}</p>
          </>
        )}
      </div>
    </div>
  )
}

function JoinDetailPane({ edge, onClose }) {
  const { data } = edge
  const lineage = data.kind === 'lineage'
  return (
    <div style={paneStyle}>
      <PaneHeader title={`${edge.source} → ${edge.target}`} onClose={onClose} />
      <div style={{ overflowY: 'auto', padding: '12px 16px', flex: 1, fontSize: 13, color: '#374151' }}>
        <p style={{ margin: '0 0 8px' }}>
          {lineage ? 'Lineage (pipeline source → derived table); never used for joins.' : `Cardinality ${data.cardinality || 'unknown'}`}
          {data.cardinality === 'n:m' && ' — joining multiplies rows.'}
        </p>
        <h4 style={{ ...heading, marginTop: 0 }}>{lineage ? 'Mapping' : `Join condition${data.alternatives.length > 1 ? 's' : ''}`}</h4>
        {data.alternatives.length
          ? data.alternatives.map((c) => <code key={c} style={mono}>{c}</code>)
          : <p style={{ margin: 0, color: '#9ca3af' }}>None recorded.</p>}
      </div>
    </div>
  )
}

function TableDetailPane({ table, terms = [], onClose }) {
  const hasPK = (c) => /primary\s*key/i.test(c.constraints)
  const hasFK = (c) => /foreign\s*key/i.test(c.constraints)

  return (
    <div
      style={{
        width: 360,
        flexShrink: 0,
        background: '#fff',
        borderLeft: '1px solid #e5e7eb',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '12px 16px',
          background: '#1e1e2e',
          color: '#fff',
          flexShrink: 0,
        }}
      >
        <span style={{ fontWeight: 700, fontSize: 14 }}>{table.label}</span>
        <button
          onClick={onClose}
          style={{
            background: 'none',
            border: 'none',
            color: '#cdd6f4',
            fontSize: 18,
            cursor: 'pointer',
            lineHeight: 1,
            padding: '0 2px',
          }}
          title="Close"
        >
          ×
        </button>
      </div>

      {/* Body */}
      <div style={{ overflowY: 'auto', padding: '12px 16px', flex: 1 }}>
        {table.description && (
          <p style={{ margin: '0 0 12px', color: '#555', fontSize: 13, fontStyle: 'italic' }}>
            {table.description}
          </p>
        )}

        <table
          style={{
            width: '100%',
            borderCollapse: 'collapse',
            fontSize: 12,
          }}
        >
          <thead>
            <tr>
              {['Column', 'Type', 'Tags', 'Description'].map((h) => (
                <th
                  key={h}
                  style={{
                    textAlign: 'left',
                    padding: '5px 6px',
                    background: '#f3f4f6',
                    border: '1px solid #e5e7eb',
                    fontWeight: 600,
                    color: '#374151',
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.columns.map((col, i) => (
              <tr key={col.name} style={{ background: i % 2 === 0 ? '#fff' : '#f9fafb' }}>
                <td style={{ padding: '5px 6px', border: '1px solid #e5e7eb', fontWeight: 500 }}>
                  {col.name}
                </td>
                <td style={{ padding: '5px 6px', border: '1px solid #e5e7eb', color: '#6b7280', fontStyle: 'italic' }}>
                  {col.type || '—'}
                </td>
                <td style={{ padding: '5px 6px', border: '1px solid #e5e7eb' }}>
                  <span style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
                    {hasPK(col) && (
                      <span style={{ background: '#fbbf24', color: '#78350f', borderRadius: 3, padding: '0 4px', fontSize: 10, fontWeight: 700 }}>PK</span>
                    )}
                    {hasFK(col) && (
                      <span style={{ background: '#93c5fd', color: '#1e3a5f', borderRadius: 3, padding: '0 4px', fontSize: 10, fontWeight: 700 }}>FK</span>
                    )}
                    {!hasPK(col) && !hasFK(col) && <span style={{ color: '#d1d5db' }}>—</span>}
                  </span>
                </td>
                <td style={{ padding: '5px 6px', border: '1px solid #e5e7eb', color: '#374151' }}>
                  {col.description || <span style={{ color: '#d1d5db' }}>—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {terms.length > 0 && (
          <>
            <h4 style={heading}>Business terms</h4>
            {terms.map((t) => (
              <div key={t.term_id} style={{ fontSize: 12, color: '#374151', marginBottom: 6 }}>
                <strong>{t.term_name}</strong>
                {t.formula && <code style={mono}>{t.formula}</code>}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  )
}

export default function SchemaERD() {
  return (
    <ReactFlowProvider>
      <SchemaERDInner />
    </ReactFlowProvider>
  )
}

function SchemaERDInner() {
  const { fitView } = useReactFlow()
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selection, setSelection] = useState(null) // { kind: 'table' | 'term' | 'join', id }
  const [instance, setInstance] = useState('default')
  const [instances, setInstances] = useState([{ instance_name: 'default', db_type: 'generic' }])
  const [schemaVersion, setSchemaVersion] = useState(0)
  const [showTerms, setShowTerms] = useState(false)
  const [terms, setTerms] = useState(null)
  const [termsError, setTermsError] = useState('')
  const [omitted, setOmitted] = useState(0)
  const termsRequested = useRef(false)
  const nodesRef = useRef([])
  nodesRef.current = nodes

  // Fetch available instances on mount
  useEffect(() => {
    fetch('/api/instances')
      .then((r) => r.json())
      .then((data) => {
        if (data.instances?.length) setInstances(data.instances)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    setLoading(true)
    setError('')
    fetch(`/api/schema?instance_name=${encodeURIComponent(instance)}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data) => {
        if (!data.tables.length) {
          setLoading(false)
          setNodes([])
          setEdges([])
          return
        }

        const { nodes: ln, edges: le } = getLayoutedElements(
          tableNodes(data.tables),
          relationEdges(data.relations).map((e) => ({
            ...e,
            data: { ...e.data, onSelect: (id) => setSelection({ kind: 'join', id }) },
          })),
        )
        setNodes(ln)
        setEdges(le)
        setSchemaVersion((v) => v + 1)
        setLoading(false)
      })
      .catch((err) => {
        setError(`Failed to load schema: ${err.message}`)
        setLoading(false)
      })
  }, [instance])

  // Glossary terms are fetched once, the first time the toggle is turned on.
  useEffect(() => {
    if (!showTerms || termsRequested.current) return
    termsRequested.current = true
    apiFetch('/api/glossary/terms')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then((data) => setTerms(data.terms || []))
      .catch((err) => {
        termsRequested.current = false // let the user retry by toggling again
        setTermsError(`Could not load business terms: ${err.message}`)
      })
  }, [showTerms])

  // Add / remove the term annotation nodes and their dotted links.
  useEffect(() => {
    const tableNodeList = nodesRef.current.filter((n) => n.type === 'tableNode')
    const { nodes: tn, edges: te, omitted: skipped } = showTerms && terms && tableNodeList.length
      ? termElements(terms.filter((t) => termInInstance(t, instance)), tableNodeList)
      : { nodes: [], edges: [], omitted: 0 }
    setNodes((nds) => [...nds.filter((n) => n.type !== 'termNode'), ...tn])
    setEdges((eds) => [...eds.filter((e) => !e.id.startsWith('term:')), ...te])
    setOmitted(skipped)
    setSelection((sel) => (sel?.kind === 'term' && !tn.some((n) => n.id === sel.id) ? null : sel))
    // Term nodes sit outside the original fit; bring them into view.
    if (tn.length) setTimeout(() => fitView({ duration: 200 }), 50)
  }, [showTerms, terms, schemaVersion, instance, setNodes, setEdges, fitView])

  // Highlight the selected table / term (and the tables a selected term uses).
  useEffect(() => {
    setNodes((nds) => {
      const picked = selection?.kind === 'term' ? nds.find((n) => n.id === selection.id) : null
      const related = picked ? picked.data.tables : []
      return nds.map((n) => {
        const selected = !!selection && selection.kind !== 'join' && n.id === selection.id
        const rel = related.includes(n.id)
        return n.data.selected === selected && !!n.data.related === rel
          ? n
          : { ...n, data: { ...n.data, selected, related: rel } }
      })
    })
  }, [selection, setNodes])

  const onNodeClick = useCallback((_, node) => {
    setSelection({ kind: node.type === 'termNode' ? 'term' : 'table', id: node.id })
  }, [])

  const onEdgeClick = useCallback((_, edge) => {
    if (edge.type === 'relation') setSelection({ kind: 'join', id: edge.id })
  }, [])

  const handleClose = useCallback(() => setSelection(null), [])

  const detail = useMemo(() => {
    if (!selection) return null
    if (selection.kind === 'join') {
      const edge = edges.find((e) => e.id === selection.id)
      return edge && <JoinDetailPane edge={edge} onClose={handleClose} />
    }
    const node = nodes.find((n) => n.id === selection.id)
    if (!node) return null
    if (selection.kind === 'term') {
      return <TermDetailPane term={node.data.term} tables={node.data.tables} onClose={handleClose} />
    }
    const used = termsForTable(node.id, nodes.filter((n) => n.type === 'termNode'))
    return <TableDetailPane table={node.data} terms={used} onClose={handleClose} />
  }, [selection, nodes, edges, handleClose])

  const instanceToolbar = (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '6px 10px', background: '#f9fafb',
      borderBottom: '1px solid #e5e7eb', flexShrink: 0,
    }}>
      <span style={{ fontSize: 12, fontWeight: 600, color: '#6b7280' }}>Instance</span>
      <select
        style={{ padding: '4px 10px', fontSize: 13, border: '1px solid #d1d5db', borderRadius: 6, background: '#fff' }}
        value={instance}
        onChange={(e) => { setSelection(null); setInstance(e.target.value) }}
      >
        {instances.map((i) => (
          <option key={i.instance_name} value={i.instance_name}>
            {i.instance_name}{i.db_type !== 'generic' ? ` (${i.db_type})` : ''}
          </option>
        ))}
      </select>
      <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, color: '#374151', marginLeft: 10 }}>
        <input type="checkbox" checked={showTerms} onChange={(e) => { setTermsError(''); setShowTerms(e.target.checked) }} />
        Show business terms
      </label>
      {showTerms && terms === null && !termsError && <span style={{ fontSize: 12, color: '#6b7280' }}>Loading terms…</span>}
      {showTerms && omitted > 0 && (
        <span style={{ fontSize: 12, color: '#6b7280' }}>
          {omitted} term{omitted === 1 ? '' : 's'} not shown (tables not on this diagram)
        </span>
      )}
      {termsError && <span role="alert" style={{ fontSize: 12, color: '#b91c1c' }}>{termsError}</span>}
    </div>
  )

  if (loading) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 100px)' }}>
        {instanceToolbar}
        <div style={{ display: 'flex', justifyContent: 'center', padding: 40, color: '#555' }}>
          Loading schema…
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 100px)' }}>
        {instanceToolbar}
        <div
          style={{
            margin: 20,
            padding: 16,
            background: '#fee2e2',
            border: '1px solid #fca5a5',
            borderRadius: 6,
            color: '#991b1b',
          }}
        >
          {error}
        </div>
      </div>
    )
  }

  if (!nodes.length) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 100px)' }}>
        {instanceToolbar}
        <div style={{ display: 'flex', justifyContent: 'center', padding: 40, color: '#888' }}>
          No schema data available. Import relations and table metadata first.
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 100px)' }}>
      {instanceToolbar}

      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
      <div style={{ flex: 1, minWidth: 0, position: 'relative' }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodeClick={onNodeClick}
          onEdgeClick={onEdgeClick}
          fitView
        >
          <Background />
          <Controls />
          <MiniMap />
        </ReactFlow>
        <Legend showTerms={showTerms} />
      </div>

      {detail}
      </div>
    </div>
  )
}

function LegendLine({ color, dash, children }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <svg width="28" height="8" aria-hidden="true">
        <line x1="0" y1="4" x2="28" y2="4" stroke={color} strokeWidth="2" strokeDasharray={dash} />
      </svg>
      <span>{children}</span>
    </div>
  )
}

function Legend({ showTerms }) {
  return (
    <div
      aria-label="Legend"
      style={{
        position: 'absolute', top: 10, left: 10, zIndex: 5, padding: '6px 10px',
        background: 'rgba(255,255,255,0.92)', border: '1px solid #e5e7eb', borderRadius: 6,
        fontSize: 11, color: '#374151', display: 'flex', flexDirection: 'column', gap: 3,
      }}
    >
      <LegendLine color={EDGE_COLORS.join}>Solid: join, labelled with cardinality (+N = more join conditions)</LegendLine>
      <LegendLine color={EDGE_COLORS.nm}>Amber n:m: joining multiplies rows</LegendLine>
      <LegendLine color={EDGE_COLORS.lineage} dash="6 4">Dashed: lineage (not used for joins)</LegendLine>
      {showTerms && <LegendLine color={EDGE_COLORS.term} dash="2 4">Dotted: business term uses table</LegendLine>}
    </div>
  )
}
