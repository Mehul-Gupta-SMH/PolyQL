/**
 * Pure helpers for the Schema/ERD tab: turn the /api/schema and glossary
 * responses into React Flow nodes and edges.
 */

export const NODE_WIDTH = 260
export const TERM_WIDTH = 190
export const TERM_STEP = 64

export const EDGE_COLORS = { join: '#94a3b8', nm: '#d97706', lineage: '#a1a1aa', term: '#8b5cf6' }

export function tableNodes(tables) {
  return tables.map((t) => ({
    id: t.name,
    type: 'tableNode',
    position: { x: 0, y: 0 },
    width: NODE_WIDTH,
    data: { label: t.name, description: t.description, columns: t.columns },
  }))
}

/** Pick the edge colour: lineage muted, n:m amber (rows multiply), else slate. */
export function edgeColor(relation) {
  if (relation.kind === 'lineage') return EDGE_COLORS.lineage
  return relation.cardinality === 'n:m' ? EDGE_COLORS.nm : EDGE_COLORS.join
}

/** One React Flow edge per directed relation returned by the API. */
export function relationEdges(relations) {
  return relations.map((r, i) => {
    const alternatives = r.alternatives?.length ? r.alternatives : r.joinKeys ? [r.joinKeys] : []
    const color = edgeColor(r)
    return {
      id: `e-${i}`,
      source: r.source,
      target: r.target,
      type: 'relation',
      markerEnd: { type: 'arrowclosed', width: 14, height: 14, color },
      style: {
        stroke: color,
        strokeWidth: r.cardinality === 'n:m' ? 2 : 1.5,
        ...(r.kind === 'lineage' ? { strokeDasharray: '6 4' } : {}),
      },
      data: {
        kind: r.kind === 'lineage' ? 'lineage' : 'fk',
        cardinality: r.cardinality || null,
        joinKeys: r.joinKeys || '',
        alternatives,
        extra: Math.max(alternatives.length - 1, 0),
        color,
      },
    }
  })
}

/** Text shown on the edge: cardinality for joins, "lineage" for lineage edges. */
export function edgeLabel(data) {
  if (data.kind === 'lineage') return 'lineage'
  return data.cardinality || ''
}

/**
 * Tables a glossary term refers to. `table_deps` may be unqualified or qualified
 * differently from the diagram: exact (case-insensitive) match first, then the
 * last dotted segment of the dep or of the table name.
 */
export function resolveTermTables(term, tableNames) {
  const lower = new Map(tableNames.map((n) => [n.toLowerCase(), n]))
  const last = (s) => s.split('.').pop()
  const byLast = new Map()
  tableNames.forEach((n) => {
    const key = last(n.toLowerCase())
    byLast.set(key, [...(byLast.get(key) || []), n])
  })
  const found = []
  for (const raw of term.table_deps || []) {
    const dep = String(raw).trim().toLowerCase().replace(/[`"[\]]/g, '')
    if (!dep) continue
    const matches = lower.has(dep) ? [lower.get(dep)] : byLast.get(last(dep)) || []
    matches.forEach((m) => { if (!found.includes(m)) found.push(m) })
  }
  return found
}

/** A term applies to the instance being shown when it is global or names that instance. */
export function termInInstance(term, instance) {
  const own = term.instance_name || ''
  return own === '' || own === instance
}

/**
 * Term annotation nodes, placed in a column above the first table they link to,
 * plus dotted edges to each table. Terms with no table on the diagram are
 * counted in `omitted` instead.
 */
export function termElements(terms, tableNodeList) {
  const names = tableNodeList.map((n) => n.id)
  const pos = new Map(tableNodeList.map((n) => [n.id, n.position]))
  const stacked = new Map()
  const nodes = []
  const edges = []
  const tablesByTerm = {}
  let omitted = 0

  for (const term of terms) {
    const tables = resolveTermTables(term, names)
    if (!tables.length) { omitted += 1; continue }
    const home = tables[0]
    const k = stacked.get(home) || 0
    stacked.set(home, k + 1)
    const id = `term:${term.term_id}`
    tablesByTerm[id] = tables
    nodes.push({
      id,
      type: 'termNode',
      position: { x: pos.get(home).x, y: pos.get(home).y - TERM_STEP * (k + 1) },
      data: { label: term.term_name, term, tables },
    })
    tables.forEach((t) => edges.push({
      id: `${id}->${t}`,
      source: id,
      target: t,
      targetHandle: 'term',
      type: 'default',
      selectable: false,
      style: { stroke: EDGE_COLORS.term, strokeWidth: 1, strokeDasharray: '2 4' },
    }))
  }
  return { nodes, edges, omitted, tablesByTerm }
}

/** Glossary terms (already resolved by termElements) that use *table*. */
export function termsForTable(tableName, termNodeList) {
  return termNodeList.filter((n) => n.data.tables.includes(tableName)).map((n) => n.data.term)
}
