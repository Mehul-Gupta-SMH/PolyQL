import { Handle, Position } from '@xyflow/react'

/** Glossary term annotation, linked by dotted edges to the tables it uses. */
export default function TermNode({ data }) {
  return (
    <div
      title={data.term.definition}
      style={{
        width: 190,
        padding: '6px 10px',
        background: '#f5f3ff',
        border: data.selected ? '2px solid #7c3aed' : '1px dashed #a78bfa',
        borderRadius: 14,
        boxShadow: data.selected ? '0 0 0 3px #ddd6fe' : 'none',
        fontFamily: 'sans-serif',
        fontSize: 12,
        color: '#4c1d95',
        textAlign: 'center',
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}
    >
      {data.label}
      <Handle id="out" type="source" position={Position.Left} style={{ opacity: 0 }} />
    </div>
  )
}
