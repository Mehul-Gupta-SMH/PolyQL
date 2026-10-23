import { BaseEdge, EdgeLabelRenderer, getBezierPath } from '@xyflow/react'
import { edgeLabel } from '../utils/erdModel.js'

/** Bezier edge with a cardinality / "lineage" label and a "+N" badge for alternative joins. */
export default function RelationEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, style, data, selected,
}) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  })
  const label = edgeLabel(data)
  const lineage = data.kind === 'lineage'
  const title = data.alternatives.length
    ? data.alternatives.join('\n')
    : lineage ? 'Pipeline lineage (not used for joins)' : ''

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={selected ? { ...style, strokeWidth: 3 } : style} />
      {label && (
        <EdgeLabelRenderer>
          <div
            title={title}
            data-testid={`edge-label-${id}`}
            onClick={() => data.onSelect?.(id)}
            className="nodrag nopan"
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: 'all',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              padding: '1px 6px',
              fontSize: 10,
              fontWeight: 600,
              borderRadius: 4,
              background: '#fff',
              border: `1px solid ${data.color}`,
              color: data.cardinality === 'n:m' ? '#92400e' : lineage ? '#71717a' : '#334155',
              fontStyle: lineage ? 'italic' : 'normal',
            }}
          >
            <span>{label}</span>
            {data.extra > 0 && (
              <span style={{ background: '#e0e7ff', color: '#3730a3', borderRadius: 8, padding: '0 5px' }}>
                +{data.extra}
              </span>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
