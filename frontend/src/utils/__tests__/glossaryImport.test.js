import { describe, it, expect } from 'vitest'
import { parseCSV, parseGlossaryFile, splitList } from '../glossaryImport.js'

describe('parseCSV', () => {
  it('handles quoted commas, escaped quotes, newlines and CRLF', () => {
    const text = 'a,b\r\n"SUM(x, y)","say ""hi"""\n"multi\nline",z\n'
    expect(parseCSV(text)).toEqual([['a', 'b'], ['SUM(x, y)', 'say "hi"'], ['multi\nline', 'z']])
  })

  it('drops blank lines and a BOM', () => {
    expect(parseCSV('﻿a\n\n1\n')).toEqual([['a'], ['1']])
  })
})

describe('splitList', () => {
  it('splits on ; and | and trims', () => {
    expect(splitList(' orders ; customers|items ')).toEqual(['orders', 'customers', 'items'])
    expect(splitList(['a ', ''])).toEqual(['a'])
  })
})

describe('parseGlossaryFile', () => {
  it('maps CSV columns, lists and default instance', () => {
    const csv = 'Term Name,formula,table_deps,synonyms\nNet Revenue,"SUM(total, 0)",orders;refunds,net sales\n'
    const { terms, errors } = parseGlossaryFile(csv, 'prod')
    expect(errors).toEqual([])
    expect(terms).toEqual([{
      term_name: 'Net Revenue', formula: 'SUM(total, 0)', table_deps: ['orders', 'refunds'],
      synonyms: ['net sales'], instance_name: 'prod',
    }])
  })

  it('reports rows without a name', () => {
    const { terms, errors } = parseGlossaryFile('term_name,definition\n,orphan\nAUM,assets\n')
    expect(terms.map((t) => t.term_name)).toEqual(['AUM'])
    expect(errors).toEqual(['Row 2: term_name is missing — skipped.'])
  })

  it('requires a term_name header', () => {
    expect(parseGlossaryFile('name,definition\nAUM,x').errors[0]).toMatch(/term_name column/)
  })

  it('accepts a JSON array or the list endpoint shape, ignoring unknown keys', () => {
    const exported = JSON.stringify({ terms: [{ term_id: 'x', term_name: 'AUM', synonyms: ['assets'], created_at: 'now' }] })
    expect(parseGlossaryFile(exported).terms).toEqual([{ term_name: 'AUM', synonyms: ['assets'] }])
    expect(parseGlossaryFile('[{"term_name":"A","instance_name":"dev"}]', 'prod').terms[0].instance_name).toBe('dev')
  })

  it('reports invalid JSON and empty input', () => {
    expect(parseGlossaryFile('[oops').errors[0]).toMatch(/Invalid JSON/)
    expect(parseGlossaryFile('  ').errors).toEqual(['The file is empty.'])
  })
})
