import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import SchemaERD from '../SchemaERD.jsx'

// d3-drag (React Flow nodes) breaks on userEvent's mousedown in jsdom, so clicks use fireEvent.
// React Flow measures with these; jsdom has neither.
beforeAll(() => {
  // The observer fires once on observe() so nodes get "measured" and their edges render.
  globalThis.ResizeObserver = class {
    constructor(cb) { this.cb = cb }
    observe(target) { setTimeout(() => this.cb([{ target }], this), 0) }
    unobserve() {}
    disconnect() {}
  }

  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 260 })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 100 })
  window.DOMMatrixReadOnly = class { constructor() { this.m22 = 1 } }
})

const col = (name, constraints = '') => ({ name, type: 'INT', constraints, description: '' })
const SCHEMA = {
  tables: [
    { name: 'orders', description: 'Order facts', columns: [col('id', 'PRIMARY KEY'), col('customer_id')] },
    { name: 'customers', description: '', columns: [col('id', 'PRIMARY KEY')] },
    { name: 'dim_date', description: '', columns: [col('id', 'PRIMARY KEY')] },
    { name: 'orders_daily', description: '', columns: [col('day')] },
  ],
  relations: [
    { source: 'orders', target: 'customers', joinKeys: 'orders.customer_id = customers.id',
      alternatives: ['orders.customer_id = customers.id'], cardinality: 'n:1', kind: 'fk' },
    { source: 'orders', target: 'dim_date', joinKeys: 'orders.order_date_id = dim_date.id',
      alternatives: ['orders.order_date_id = dim_date.id', 'orders.ship_date_id = dim_date.id'],
      cardinality: 'n:m', kind: 'fk' },
    { source: 'orders', target: 'orders_daily', joinKeys: '', alternatives: [], cardinality: null, kind: 'lineage' },
  ],
}
const TERMS = [
  { term_id: 't1', term_name: 'Revenue', definition: 'Money in', formula: 'SUM(orders.total)',
    table_deps: ['sales.public.orders'], synonyms: ['sales'], instance_name: '' },
  { term_id: 't2', term_name: 'Ghost', definition: 'No tables', formula: '', table_deps: ['nowhere'], instance_name: '' },
]

function mockFetch({ terms = TERMS, termsStatus = 200 } = {}) {
  const fn = vi.fn((url) => {
    const ok = (body, status = 200) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) })
    if (url.startsWith('/api/schema')) return ok(SCHEMA)
    if (url.startsWith('/api/glossary/terms')) return ok({ terms }, termsStatus)
    return ok({ instances: [] })
  })
  globalThis.fetch = fn
  return fn
}
const termCalls = (fn) => fn.mock.calls.filter(([u]) => u.startsWith('/api/glossary/terms'))

async function renderERD(opts) {
  const fn = mockFetch(opts)
  const { container } = render(<SchemaERD />)
  await screen.findByText('customers')
  return { fn, container }
}

describe('SchemaERD relations', () => {
  it('labels edges with cardinality and a +N badge for alternatives', async () => {
    await renderERD()
    expect(await screen.findByText('n:1')).toBeInTheDocument()
    const nm = await screen.findByText('n:m')
    expect(nm.parentElement).toHaveTextContent('+1')
    expect(nm.parentElement.title).toContain('orders.ship_date_id = dim_date.id')
    expect(nm.parentElement.title).toContain('orders.order_date_id = dim_date.id')
  })

  it('draws lineage edges dashed and labelled "lineage"', async () => {
    const { container } = await renderERD()
    const label = await screen.findByText('lineage')
    expect(label.parentElement.style.fontStyle).toBe('italic')
    const dashed = [...container.querySelectorAll('.react-flow__edge-path')]
      .filter((p) => p.style.strokeDasharray)
    expect(dashed).toHaveLength(1)
    expect(screen.getByLabelText('Legend')).toHaveTextContent(/dashed: lineage/i)
  })

  it('shows every join condition when an edge label is clicked', async () => {
    await renderERD()
    fireEvent.click((await screen.findByText('n:m')).parentElement)
    expect(await screen.findByText('Join conditions')).toBeInTheDocument()
    expect(screen.getByText('orders.ship_date_id = dim_date.id')).toBeInTheDocument()
    expect(screen.getByText(/Cardinality n:m — joining multiplies rows/)).toBeInTheDocument()
  })

  it('still opens the data dictionary on a table click', async () => {
    await renderERD()
    fireEvent.click(screen.getByText('customers'))
    expect(await screen.findByText('Column')).toBeInTheDocument()
  })
})

describe('SchemaERD business terms', () => {
  it('fetches terms once, on first toggle, and adds term nodes', async () => {
    const { fn } = await renderERD()
    expect(termCalls(fn)).toHaveLength(0)
    expect(screen.queryByText('Revenue')).not.toBeInTheDocument()

    const toggle = screen.getByLabelText('Show business terms')
    fireEvent.click(toggle)
    expect(await screen.findByText('Revenue')).toBeInTheDocument()
    expect(screen.getByText(/1 term not shown/)).toBeInTheDocument()
    expect(screen.queryByText('Ghost')).not.toBeInTheDocument()

    fireEvent.click(toggle)
    await waitFor(() => expect(screen.queryByText('Revenue')).not.toBeInTheDocument())
    fireEvent.click(toggle)
    expect(await screen.findByText('Revenue')).toBeInTheDocument()
    expect(termCalls(fn)).toHaveLength(1)
  })

  it('shows definition and formula when a term is clicked', async () => {
    await renderERD()
    fireEvent.click(screen.getByLabelText('Show business terms'))
    fireEvent.click(await screen.findByText('Revenue'))
    expect(await screen.findByText('Money in')).toBeInTheDocument()
    expect(screen.getByText('SUM(orders.total)')).toBeInTheDocument()
  })

  it('lists the terms that use a clicked table', async () => {
    await renderERD()
    fireEvent.click(screen.getByLabelText('Show business terms'))
    await screen.findByText('Revenue')
    fireEvent.click(screen.getByText('orders'))
    expect(await screen.findByText('Business terms')).toBeInTheDocument()
    expect(screen.getByText('SUM(orders.total)')).toBeInTheDocument()
  })

  it('keeps the diagram and shows a message when terms fail to load', async () => {
    await renderERD({ termsStatus: 500 })
    fireEvent.click(screen.getByLabelText('Show business terms'))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not load business terms/)
    expect(screen.getByText('customers')).toBeInTheDocument()
  })
})

describe('SchemaERD search and filter', () => {
  it('searches columns, dims non-matches and selects the table on click', async () => {
    const { container } = await renderERD()
    const box = screen.getByRole('searchbox')
    fireEvent.change(box, { target: { value: 'customer_id' } })
    const opt = (await screen.findAllByRole('option')).find((o) => o.tagName === 'LI')
    expect(opt).toHaveTextContent('orders.customer_id')
    const dimmed = () => [...container.querySelectorAll('.react-flow__node')].filter((n) => n.style.opacity === '0.25')
    await waitFor(() => expect(dimmed()).toHaveLength(3))
    fireEvent.click(opt)
    expect(await screen.findByText('Column')).toBeInTheDocument() // side pane open
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('Enter selects the first match and Esc clears the query', async () => {
    await renderERD()
    const box = screen.getByRole('searchbox')
    fireEvent.change(box, { target: { value: 'dim_' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(await screen.findByText('Column')).toBeInTheDocument()
    fireEvent.change(box, { target: { value: 'zzz' } })
    expect(await screen.findByText('No matches')).toBeInTheDocument()
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(box.value).toBe('')
  })

  it('hides non-neighbours and their edges with the neighbours-only toggle', async () => {
    const { container } = await renderERD()
    const nodeIds = () => [...container.querySelectorAll('.react-flow__node')].filter((n) => n.style.visibility !== 'hidden').map((n) => n.dataset.id)
    const neighbours = screen.getByLabelText('Neighbours only')
    expect(neighbours).toBeDisabled()
    fireEvent.click(screen.getByText('customers'))
    await waitFor(() => expect(neighbours).not.toBeDisabled())
    fireEvent.click(neighbours)
    await waitFor(() => expect(nodeIds().sort()).toEqual(['customers', 'orders']))
    expect(screen.queryByText('n:m')).not.toBeInTheDocument() // orders->dim_date edge hidden
    expect(screen.getByText('n:1')).toBeInTheDocument()
  })

  it('filters by schema prefix chips and keeps the term toggle working', async () => {
    const schema = {
      tables: [
        { name: 'sales.orders', description: '', columns: [col('id')] },
        { name: 'hr.people', description: '', columns: [col('id')] },
      ],
      relations: [],
    }
    globalThis.fetch = vi.fn((url) => Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve(url.startsWith('/api/schema') ? schema : url.startsWith('/api/glossary') ? { terms: [
        { term_id: 'r', term_name: 'Revenue', definition: '', table_deps: ['sales.orders'], instance_name: '' },
      ] } : { instances: [] }),
    }))
    const { container } = render(<SchemaERD />)
    await screen.findByText('hr.people')
    fireEvent.click(screen.getByLabelText('Show business terms'))
    expect(await screen.findByText('Revenue')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /^hr \(1\)/ }))
    const shown = () => [...container.querySelectorAll('.react-flow__node')].filter((n) => n.style.visibility !== 'hidden').map((n) => n.dataset.id)
    await waitFor(() => expect(shown()).toEqual(['hr.people'])) // term of a hidden table goes too
    fireEvent.click(screen.getByRole('button', { name: /^hr \(1\)/ }))
    await waitFor(() => expect(shown()).toHaveLength(3))
  })
})
