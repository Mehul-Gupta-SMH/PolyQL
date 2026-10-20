/**
 * Parse a glossary import file (CSV or JSON) into term objects for
 * POST /api/glossary/terms/bulk.
 *
 * CSV: a header row naming any of TERM_FIELDS (term_name is required).
 * List fields (table_deps, column_deps, synonyms) are separated by ";" or "|".
 * Quoted values may contain commas, quotes ("") and newlines — formulas often do.
 *
 * JSON: an array of term objects, or {terms: [...]} as GET /api/glossary/terms returns.
 */

export const TERM_FIELDS = [
  'term_name', 'full_name', 'definition', 'formula', 'formula_type',
  'table_deps', 'column_deps', 'synonyms', 'example_value', 'domain', 'instance_name',
]
export const LIST_FIELDS = ['table_deps', 'column_deps', 'synonyms']

export function splitList(value) {
  if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean)
  return String(value ?? '').split(/[;|]/).map((v) => v.trim()).filter(Boolean)
}

/** RFC 4180-style CSV → array of rows (arrays of strings). */
export function parseCSV(text) {
  const rows = []
  let row = [], field = '', quoted = false
  const src = text.replace(/^﻿/, '')
  for (let i = 0; i < src.length; i++) {
    const c = src[i]
    if (quoted) {
      if (c === '"' && src[i + 1] === '"') { field += '"'; i++ }
      else if (c === '"') quoted = false
      else field += c
    } else if (c === '"' && field === '') {
      quoted = true
    } else if (c === ',') {
      row.push(field); field = ''
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && src[i + 1] === '\n') i++
      row.push(field); rows.push(row); row = []; field = ''
    } else {
      field += c
    }
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row) }
  return rows.filter((r) => r.some((v) => v.trim() !== ''))
}

function normalise(raw, instanceName) {
  const term = {}
  for (const key of TERM_FIELDS) {
    const value = raw[key]
    if (value === undefined || value === null || value === '') continue
    term[key] = LIST_FIELDS.includes(key) ? splitList(value) : String(value).trim()
  }
  if (!term.instance_name && instanceName) term.instance_name = instanceName
  return term
}

/**
 * Returns {terms, errors}. Rows without a term_name are reported in errors
 * (with their line number) and left out of terms.
 */
export function parseGlossaryFile(text, instanceName = '') {
  const trimmed = text.trim()
  if (!trimmed) return { terms: [], errors: ['The file is empty.'] }

  let records, lineOf
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    let data
    try {
      data = JSON.parse(trimmed)
    } catch (e) {
      return { terms: [], errors: [`Invalid JSON: ${e.message}`] }
    }
    records = Array.isArray(data) ? data : data.terms
    if (!Array.isArray(records)) return { terms: [], errors: ['JSON must be an array of terms or {"terms": [...]}.'] }
    lineOf = (i) => `Item ${i + 1}`
  } else {
    const rows = parseCSV(trimmed)
    const header = (rows[0] || []).map((h) => h.trim().toLowerCase().replace(/\s+/g, '_'))
    if (!header.includes('term_name')) return { terms: [], errors: ['CSV header must include a term_name column.'] }
    records = rows.slice(1).map((r) => Object.fromEntries(header.map((h, i) => [h, r[i] ?? ''])))
    lineOf = (i) => `Row ${i + 2}`
  }

  const terms = [], errors = []
  records.forEach((raw, i) => {
    const term = normalise(raw || {}, instanceName)
    if (!term.term_name) errors.push(`${lineOf(i)}: term_name is missing — skipped.`)
    else terms.push(term)
  })
  return { terms, errors }
}
