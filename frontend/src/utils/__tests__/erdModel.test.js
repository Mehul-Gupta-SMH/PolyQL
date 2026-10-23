import {
  resolveTermTables, termInInstance, termElements, termsForTable, relationEdges, edgeLabel, EDGE_COLORS,
} from '../erdModel.js'

const TABLES = ['sales.public.orders', 'customers', 'Dim_Date']

describe('resolveTermTables', () => {
  it('matches exact names case-insensitively', () => {
    expect(resolveTermTables({ table_deps: ['CUSTOMERS', 'dim_date'] }, TABLES)).toEqual(['customers', 'Dim_Date'])
  })
  it('falls back to the last dotted segment in either direction', () => {
    expect(resolveTermTables({ table_deps: ['orders'] }, TABLES)).toEqual(['sales.public.orders'])
    expect(resolveTermTables({ table_deps: ['db.public.customers'] }, TABLES)).toEqual(['customers'])
  })
  it('ignores unknown, blank and duplicate deps', () => {
    expect(resolveTermTables({ table_deps: ['nope', ' ', 'customers', 'customers'] }, TABLES)).toEqual(['customers'])
    expect(resolveTermTables({}, TABLES)).toEqual([])
  })
})

describe('termInInstance', () => {
  it('accepts global terms and the named instance only', () => {
    expect(termInInstance({ instance_name: '' }, 'prod')).toBe(true)
    expect(termInInstance({}, 'prod')).toBe(true)
    expect(termInInstance({ instance_name: 'prod' }, 'prod')).toBe(true)
    expect(termInInstance({ instance_name: 'dev' }, 'prod')).toBe(false)
  })
})

describe('termElements', () => {
  const nodes = TABLES.map((id, i) => ({ id, position: { x: i * 300, y: 100 } }))
  const terms = [
    { term_id: 'a', term_name: 'A', table_deps: ['customers', 'orders'] },
    { term_id: 'b', term_name: 'B', table_deps: ['customers'] },
    { term_id: 'c', term_name: 'C', table_deps: ['missing'] },
  ]
  it('places term nodes above their first table, one dotted edge per table', () => {
    const out = termElements(terms, nodes)
    expect(out.nodes.map((n) => n.id)).toEqual(['term:a', 'term:b'])
    expect(out.nodes[0].position.x).toBe(300)
    expect(out.nodes[1].position.y).toBeLessThan(out.nodes[0].position.y) // stacked
    expect(out.edges.map((e) => e.target)).toEqual(['customers', 'sales.public.orders', 'customers'])
    expect(out.edges[0].style.strokeDasharray).toBeTruthy()
    expect(out.omitted).toBe(1)
  })
  it('finds the terms for a table', () => {
    const out = termElements(terms, nodes)
    expect(termsForTable('customers', out.nodes).map((t) => t.term_id)).toEqual(['a', 'b'])
    expect(termsForTable('Dim_Date', out.nodes)).toEqual([])
  })
})

describe('relationEdges', () => {
  const edges = relationEdges([
    { source: 'a', target: 'b', joinKeys: 'a.x = b.id', alternatives: ['a.x = b.id', 'a.y = b.id'], cardinality: 'n:1', kind: 'fk' },
    { source: 'a', target: 'c', joinKeys: '', alternatives: [], cardinality: 'n:m', kind: 'fk' },
    { source: 'a', target: 'd', joinKeys: '', cardinality: null, kind: 'lineage' },
  ])
  it('keeps one edge per relation with +N for alternatives', () => {
    expect(edges).toHaveLength(3)
    expect(edges[0].data.extra).toBe(1)
    expect(edgeLabel(edges[0].data)).toBe('n:1')
  })
  it('colours n:m amber and dashes lineage', () => {
    expect(edges[1].style.stroke).toBe(EDGE_COLORS.nm)
    expect(edges[2].style.strokeDasharray).toBeTruthy()
    expect(edgeLabel(edges[2].data)).toBe('lineage')
  })
  it('tolerates an older API response without alternatives', () => {
    const [e] = relationEdges([{ source: 'a', target: 'b', joinKeys: 'a.x = b.id' }])
    expect(e.data.alternatives).toEqual(['a.x = b.id'])
    expect(e.data.kind).toBe('fk')
  })
})
