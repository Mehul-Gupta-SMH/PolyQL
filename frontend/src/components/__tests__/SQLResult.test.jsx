import React from 'react'
import { render, screen, fireEvent, act } from '@testing-library/react'
import SQLResult from '../SQLResult.jsx'

function renderResult(props = {}) {
  return render(
    <SQLResult
      sql="SELECT 1"
      queryType="sql"
      connString=""
      setConnString={vi.fn()}
      onExecute={vi.fn()}
      execLoading={false}
      execResult={null}
      execError=""
      {...props}
    />,
  )
}

describe('SQLResult', () => {
  beforeEach(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders nothing when sql is empty string', () => {
    const { container } = renderResult({ sql: "" })
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when sql is null', () => {
    const { container } = renderResult({ sql: null })
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the SQL text', () => {
    renderResult()
    expect(screen.getByText('SELECT 1')).toBeInTheDocument()
  })

  it('renders "Generated SQL" heading', () => {
    renderResult()
    expect(screen.getByRole('heading', { name: /generated sql/i })).toBeInTheDocument()
  })

  it('renders a copy button', () => {
    renderResult()
    expect(screen.getByRole('button', { name: /copy/i })).toBeInTheDocument()
  })

  it('calls clipboard.writeText with the sql string on copy click', async () => {
    renderResult({ sql: "SELECT 42" })
    fireEvent.click(screen.getByRole('button', { name: /copy/i }))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('SELECT 42')
  })

  it('shows "Copied!" after copy button is clicked', async () => {
    renderResult()
    fireEvent.click(screen.getByRole('button', { name: /copy/i }))
    // flush the resolved clipboard promise
    await act(async () => {})
    expect(screen.getByRole('button', { name: /copied!/i })).toBeInTheDocument()
  })

  it('reverts button label back to "Copy" after 2 s', async () => {
    vi.useFakeTimers()
    renderResult()
    fireEvent.click(screen.getByRole('button', { name: /copy/i }))
    await act(async () => {})
    expect(screen.getByRole('button', { name: /copied!/i })).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(2001))
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })

  describe('execution section', () => {
    it('disables Run Query while the connection string is blank', () => {
      renderResult({ connString: '   ' })
      expect(screen.getByRole('button', { name: 'Run Query' })).toBeDisabled()
    })

    it('calls onExecute when Run Query is clicked with a connection string', () => {
      const onExecute = vi.fn()
      renderResult({ connString: 'sqlite:///x.db', onExecute })
      fireEvent.click(screen.getByRole('button', { name: 'Run Query' }))
      expect(onExecute).toHaveBeenCalledTimes(1)
    })

    it('calls setConnString when the connection input changes', () => {
      const setConnString = vi.fn()
      renderResult({ setConnString })
      fireEvent.change(screen.getByPlaceholderText(/connection string/i), { target: { value: 'sqlite:///y.db' } })
      expect(setConnString).toHaveBeenCalledWith('sqlite:///y.db')
    })

    it('shows the execution error', () => {
      renderResult({ execError: 'no such table' })
      expect(screen.getByText('no such table')).toBeInTheDocument()
    })

    it('renders result rows, with NULL for null cells', () => {
      renderResult({ execResult: { columns: ['id', 'name'], rows: [[1, 'a'], [2, null]] } })
      expect(screen.getByRole('columnheader', { name: 'id' })).toBeInTheDocument()
      expect(screen.getByText('a')).toBeInTheDocument()
      expect(screen.getByText('NULL')).toBeInTheDocument()
    })

    it('shows a notice instead of the execution controls for DataFrame code', () => {
      renderResult({ queryType: 'dataframe_api' })
      expect(screen.getByRole('heading', { name: /generated dataframe code/i })).toBeInTheDocument()
      expect(screen.getByText(/requires a pyspark environment/i)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Run Query' })).not.toBeInTheDocument()
    })
  })
})
