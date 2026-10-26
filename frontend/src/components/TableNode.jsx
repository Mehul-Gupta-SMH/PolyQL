import { Handle, Position } from '@xyflow/react'

export default function TableNode({ data }) {
  return (
    <div
      style={{
        minWidth: 260,
        width: 260,
        background: '#fff',
        border: data.selected ? '2px solid #2563eb' : data.related ? '2px solid #8b5cf6' : '1px solid #ddd',
        borderRadius: 8,
        overflow: 'hidden',
        fontFamily: 'sans-serif',
        fontSize: 12,
        boxShadow: data.selected ? '0 0 0 3px #bfdbfe' : data.related ? '0 0 0 3px #ddd6fe' : 'none',
      }}
    >
      <Handle type="target" position={Position.Left} />
      <Handle id="term" type="target" position={Position.Right} style={{ opacity: 0 }} />

      <div
        style={{
          background: '#1e1e2e',
          color: '#fff',
          padding: '8px 12px',
          fontWeight: 600,
          fontSize: 13,
        }}
      >
        {data.label}
      </div>

      {data.description && (
        <div
          style={{
            padding: '4px 12px',
            color: '#666',
            fontSize: 11,
            borderBottom: '1px solid #eee',
            fontStyle: 'italic',
          }}
        >
          {data.description}
        </div>
      )}

      {data.columns.map((col) => {
        const isPK = /primary\s*key/i.test(col.constraints)
        const isFK = /foreign\s*key/i.test(col.constraints)
        return (
          <div
            key={col.name}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '3px 12px',
              borderBottom: '1px solid #f0f0f0',
            }}
          >
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              {isPK && (
                <span
                  style={{
                    background: '#fbbf24',
                    color: '#78350f',
                    borderRadius: 3,
                    padding: '0 4px',
                    fontSize: 10,
                    fontWeight: 700,
                  }}
                >
                  PK
                </span>
              )}
              {isFK && (
                <span
                  style={{
                    background: '#93c5fd',
                    color: '#1e3a5f',
                    borderRadius: 3,
                    padding: '0 4px',
                    fontSize: 10,
                    fontWeight: 700,
                  }}
                >
                  FK
                </span>
              )}
              {col.name}
            </span>
            <span style={{ color: '#888', fontStyle: 'italic' }}>{col.type}</span>
          </div>
        )
      })}

      <Handle type="source" position={Position.Right} />
    </div>
  )
}
