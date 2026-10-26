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
    data: { label: t.name, description: t.description, columns: t.columns, tags: t.tags, domain: t.domain },
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

export const TERM_GAP = 140
export const TERM_ROW = 56

/** Rendered height of a table node (matches the dagre estimate in SchemaERD). */
export function tableHeight(node) {
  return 60 + (node.data?.columns?.length || 0) * 28
}

/**
 * Term annotation nodes, placed in their own column to the right of the table
 * layout's bounding box. Each term sits at the mean height of the tables it
 * links to (so a term on two tables draws short straight links), then the column
 * is stacked top to bottom without overlap. Terms with no table on the diagram
 * are counted in `omitted` instead.
 */
export function termElements(terms, tableNodeList) {
  const names = tableNodeList.map((n) => n.id)
  const byId = new Map(tableNodeList.map((n) => [n.id, n]))
  const bandX = tableNodeList.length
    ? Math.max(...tableNodeList.map((n) => n.position.x + NODE_WIDTH)) + TERM_GAP
    : 0
  const wanted = []
  const edges = []
  const tablesByTerm = {}
  let omitted = 0

  for (const term of terms) {
    const tables = resolveTermTables(term, names)
    if (!tables.length) { omitted += 1; continue }
    const id = `term:${term.term_id}`
    tablesByTerm[id] = tables
    const ys = tables.map((t) => byId.get(t).position.y + tableHeight(byId.get(t)) / 2)
    wanted.push({
      id, term, tables, y: ys.reduce((a, b) => a + b, 0) / ys.length,
    })
    tables.forEach((t) => edges.push({
      id: `${id}->${t}`,
      source: id,
      target: t,
      sourceHandle: 'out',
      targetHandle: 'term',
      type: 'straight',
      selectable: false,
      style: { stroke: EDGE_COLORS.term, strokeWidth: 1, strokeDasharray: '2 4' },
    }))
  }

  wanted.sort((a, b) => a.y - b.y)
  let floor = -Infinity
  const nodes = wanted.map((w) => {
    const y = Math.max(w.y - TERM_ROW / 2, floor)
    floor = y + TERM_ROW
    return {
      id: w.id,
      type: 'termNode',
      position: { x: bandX, y },
      data: { label: w.term.term_name, term: w.term, tables: w.tables },
    }
  })
  return { nodes, edges, omitted, tablesByTerm }
}

/** Part of a qualified table name before the last dot ('' when unqualified). */
export function schemaOf(name) {
  const i = name.lastIndexOf('.')
  return i < 0 ? '' : name.slice(0, i)
}

/** Part after the last dot. */
export function shortName(name) {
  return name.slice(name.lastIndexOf('.') + 1)
}

/**
 * Search tables and columns. Returns [{ table, column? }]: tables whose
 * qualified or short name contains the query, then matching columns
 * (case-insensitive). Capped at `limit`.
 */
export function searchSchema(query, tables, limit = 12) {
  const q = (query || '').trim().toLowerCase()
  if (!q) return []
  const out = []
  for (const t of tables) {
    if (t.name.toLowerCase().includes(q) || shortName(t.name).toLowerCase().includes(q)) out.push({ table: t.name })
  }
  for (const t of tables) {
    for (const c of t.columns || []) {
      if (c.name.toLowerCase().includes(q)) out.push({ table: t.name, column: c.name })
    }
  }
  return out.slice(0, limit)
}

/** Every table matching the query (not capped), for dimming the rest. */
export function matchingTables(query, tables) {
  return new Set(searchSchema(query, tables, Infinity).map((r) => r.table))
}

/** Facet values of a table: its tags/domain when the API gives them, else its schema prefix. */
export function facetsOf(table) {
  const out = [...(table.tags || []), ...(table.domain ? [table.domain] : [])].map(String)
  return out.length ? out : [schemaOf(table.name) || '(no schema)']
}

/** Distinct facet values with counts, sorted by name; [] when there is nothing to filter on. */
export function facetOptions(tables) {
  const counts = new Map()
  tables.forEach((t) => facetsOf(t).forEach((f) => counts.set(f, (counts.get(f) || 0) + 1)))
  if (counts.size < 2) return []
  return [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([value, count]) => ({ value, count }))
}

/** Names of the tables joined to / from `name` by any relation (not including itself). */
export function neighbours(name, relations) {
  const out = new Set()
  relations.forEach((r) => {
    if (r.source === name) out.add(r.target)
    else if (r.target === name) out.add(r.source)
  })
  return out
}

/**
 * Names of tables to show. `facets` is a Set of selected facet values (empty = all);
 * `onlyNeighboursOf` limits to that table and its neighbours.
 */
export function visibleTables(tables, relations, { facets = new Set(), onlyNeighboursOf = null } = {}) {
  const near = onlyNeighboursOf ? neighbours(onlyNeighboursOf, relations) : null
  if (near) near.add(onlyNeighboursOf)
  return new Set(tables.filter((t) => (
    (!facets.size || facetsOf(t).some((f) => facets.has(f))) && (!near || near.has(t.name))
  )).map((t) => t.name))
}

/** Glossary terms (already resolved by termElements) that use *table*. */
export function termsForTable(tableName, termNodeList) {
  return termNodeList.filter((n) => n.data.tables.includes(tableName)).map((n) => n.data.term)
}
