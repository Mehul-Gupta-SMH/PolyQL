import {
  resolveTermTables, termInInstance, termElements, termsForTable, relationEdges, edgeLabel, EDGE_COLORS,
  searchSchema, matchingTables, facetsOf, facetOptions, neighbours, visibleTables, schemaOf, NODE_WIDTH, TERM_ROW,
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
  it('puts term nodes in a column right of every table, one dotted straight edge per table', () => {
    const out = termElements(terms, nodes)
    expect(out.nodes.map((n) => n.id)).toEqual(['term:a', 'term:b'])
    const right = Math.max(...nodes.map((n) => n.position.x)) + NODE_WIDTH
    out.nodes.forEach((n) => expect(n.position.x).toBeGreaterThan(right))
    expect(out.edges.map((e) => e.target)).toEqual(['customers', 'sales.public.orders', 'customers'])
    expect(out.edges[0].type).toBe('straight')
    expect(out.edges[0].style.strokeDasharray).toBeTruthy()
    expect(out.omitted).toBe(1)
  })
  it('stacks terms without overlap even when they want the same row', () => {
    const many = Array.from({ length: 6 }, (_, i) => ({ term_id: `m${i}`, term_name: `M${i}`, table_deps: ['customers'] }))
    const ys = termElements(many, nodes).nodes.map((n) => n.position.y).sort((a, b) => a - b)
    for (let i = 1; i < ys.length; i++) expect(ys[i] - ys[i - 1]).toBeGreaterThanOrEqual(TERM_ROW)
  })
  it('centres a two-table term between its tables', () => {
    const two = [
      { id: 'a', position: { x: 0, y: 0 }, data: { columns: [] } },
      { id: 'b', position: { x: 0, y: 500 }, data: { columns: [] } },
    ]
    const [n] = termElements([{ term_id: 'x', term_name: 'X', table_deps: ['a', 'b'] }], two).nodes
    expect(n.position.y + TERM_ROW / 2).toBe((30 + 530) / 2)
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

describe('search, facets and visibility', () => {
  const tables = [
    { name: 'sales.public.orders', columns: [{ name: 'id' }, { name: 'customer_id' }] },
    { name: 'sales.public.customers', columns: [{ name: 'id' }] },
    { name: 'hr.employees', columns: [{ name: 'Order_ref' }] },
  ]
  const relations = [{ source: 'sales.public.orders', target: 'sales.public.customers' }]

  it('matches qualified or short table names and columns, case-insensitively', () => {
    expect(searchSchema('ORDERS', tables)).toEqual([{ table: 'sales.public.orders' }])
    expect(searchSchema('public.cust', tables)).toEqual([{ table: 'sales.public.customers' }])
    expect(searchSchema('order', tables)).toEqual([
      { table: 'sales.public.orders' }, { table: 'hr.employees', column: 'Order_ref' },
    ])
    expect(searchSchema('customer_id', tables)).toEqual([{ table: 'sales.public.orders', column: 'customer_id' }])
    expect(searchSchema('  ', tables)).toEqual([])
  })
  it('caps results but not the dimming set', () => {
    expect(searchSchema('id', tables, 2)).toHaveLength(2)
    expect([...matchingTables('id', tables)]).toEqual(['sales.public.orders', 'sales.public.customers'])
  })
  it('facets by tags/domain when present, else schema prefix', () => {
    expect(facetsOf({ name: 'a.b.c' })).toEqual(['a.b'])
    expect(facetsOf({ name: 'c' })).toEqual(['(no schema)'])
    expect(facetsOf({ name: 'c', tags: ['x'], domain: 'finance' })).toEqual(['x', 'finance'])
    expect(schemaOf('a.b.c')).toBe('a.b')
    expect(facetOptions(tables)).toEqual([{ value: 'hr', count: 1 }, { value: 'sales.public', count: 2 }])
    expect(facetOptions([tables[0], tables[1]])).toEqual([]) // single facet: nothing to filter
  })
  it('filters by facet and by neighbours', () => {
    expect(neighbours('sales.public.orders', relations)).toEqual(new Set(['sales.public.customers']))
    expect(visibleTables(tables, relations).size).toBe(3)
    expect([...visibleTables(tables, relations, { facets: new Set(['hr']) })]).toEqual(['hr.employees'])
    expect([...visibleTables(tables, relations, { onlyNeighboursOf: 'sales.public.customers' })])
      .toEqual(['sales.public.orders', 'sales.public.customers'])
  })
})
