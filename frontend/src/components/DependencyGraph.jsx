import { useMemo, useState } from 'react'
import { fmtTime } from '../lib/format'

const NODE_W = 168
const NODE_H = 54
const COL_W = 215
const ROW_H = 84
const PAD = 24

const STYLE = {
  root: { fill: '#3f0d1a', stroke: '#fb7185', text: '#ffe4e6', label: 'Root cause' },
  candidate: { fill: '#3b1d0a', stroke: '#fb923c', text: '#ffedd5', label: 'Root candidate' },
  impacted: { fill: '#3a2a07', stroke: '#fbbf24', text: '#fef3c7', label: 'Impacted' },
  shift: { fill: '#10243a', stroke: '#38bdf8', text: '#e0f2fe', label: 'Traffic shift' },
  healthy: { fill: '#0f1a2e', stroke: '#334366', text: '#cbd5e1', label: 'Healthy' },
}

function layout(nodes, edges) {
  const ids = nodes.map((n) => n.id)
  const callers = Object.fromEntries(ids.map((i) => [i, []]))
  edges.forEach((e) => { if (callers[e.to]) callers[e.to].push(e.from) })
  const level = Object.fromEntries(ids.map((i) => [i, 0]))
  for (let k = 0; k < ids.length; k++) {
    let changed = false
    edges.forEach((e) => {
      if (level[e.from] != null && level[e.to] != null && level[e.to] < level[e.from] + 1 && level[e.from] + 1 < ids.length) {
        level[e.to] = level[e.from] + 1; changed = true
      }
    })
    if (!changed) break
  }
  const cols = {}
  ids.forEach((i) => { (cols[level[i]] ||= []).push(i) })
  const pos = {}
  const maxRows = Math.max(...Object.values(cols).map((c) => c.length), 1)
  Object.keys(cols).map(Number).sort((a, b) => a - b).forEach((lv) => {
    const col = cols[lv]
    col.sort((a, b) => {
      const bary = (x) => { const cs = callers[x].filter((c) => pos[c]); return cs.length ? cs.reduce((s, c) => s + pos[c].y, 0) / cs.length : 0 }
      return bary(a) - bary(b) || a.localeCompare(b)
    })
    const offset = ((maxRows - col.length) * ROW_H) / 2
    col.forEach((id, i) => { pos[id] = { x: PAD + lv * COL_W, y: PAD + offset + i * ROW_H } })
  })
  const nCols = Object.keys(cols).length
  return { pos, width: PAD * 2 + (nCols - 1) * COL_W + NODE_W, height: PAD * 2 + (maxRows - 1) * ROW_H + NODE_H }
}

export default function DependencyGraph({ graph, impact, compact = false }) {
  const [sel, setSel] = useState(null)
  const nodes = graph?.nodes || []
  const edges = graph?.edges || []
  const { pos, width, height } = useMemo(() => layout(nodes, edges), [nodes, edges])
  const shifts = new Set(impact?.secondary_traffic_shifts || [])
  const path = graph?.path || []
  const pathEdges = new Set(path.slice(1).map((c, i) => `${c}->${path[i]}`))
  const impactBy = Object.fromEntries((impact?.services || []).map((s) => [s.service, s]))

  const kind = (n) => (n.id === graph.root ? 'root' : n.status === 'root_candidate' ? 'candidate' : n.status === 'impacted' ? 'impacted' : shifts.has(n.id) ? 'shift' : 'healthy')
  if (!nodes.length) return <p className="text-sm text-slate-500">No dependency map was supplied for this incident.</p>
  const selected = nodes.find((n) => n.id === sel)

  return (
    <div>
      <div className="overflow-x-auto rounded-lg border border-ink-700 bg-ink-950/60">
        <svg viewBox={`0 0 ${width} ${height}`} style={{ width: '100%', minWidth: Math.min(width, 640), maxWidth: width * 1.15 }} className="mx-auto block">
          <defs>
            {['#475569', '#fb7185'].map((c, i) => (
              <marker key={c} id={`arrow${i}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill={c} />
              </marker>
            ))}
          </defs>
          {edges.map((e) => {
            const a = pos[e.from]; const b = pos[e.to]
            if (!a || !b) return null
            const x1 = a.x + NODE_W; const y1 = a.y + NODE_H / 2; const x2 = b.x - 2; const y2 = b.y + NODE_H / 2
            const back = x2 <= x1
            const d = back
              ? `M${a.x + NODE_W / 2},${a.y + NODE_H} C${a.x + NODE_W / 2},${a.y + NODE_H + 40} ${b.x + NODE_W / 2},${b.y + NODE_H + 40} ${b.x + NODE_W / 2},${b.y + NODE_H + 2}`
              : `M${x1},${y1} C${x1 + 60},${y1} ${x2 - 60},${y2} ${x2},${y2}`
            const onPath = pathEdges.has(`${e.from}->${e.to}`)
            const prop = e.propagation
            return (
              <g key={`${e.from}-${e.to}`}>
                <path d={d} fill="none" stroke={onPath ? '#fb7185' : prop ? '#b45309' : '#334155'} strokeWidth={onPath ? 2.4 : 1.4}
                  markerEnd={`url(#arrow${onPath ? 1 : 0})`} opacity={onPath || prop ? 1 : 0.8} />
                {onPath && <path d={d} fill="none" stroke="#fecdd3" strokeWidth="2.4" strokeDasharray="4 16" className="animate-dash" style={{ animationDirection: 'reverse' }} />}
              </g>
            )
          })}
          {nodes.map((n) => {
            const p = pos[n.id]; if (!p) return null
            const k = kind(n); const s = STYLE[k]
            const imp = impactBy[n.id]
            return (
              <g key={n.id} transform={`translate(${p.x},${p.y})`} onClick={() => setSel(sel === n.id ? null : n.id)} className="cursor-pointer">
                {k === 'root' && <rect x="-5" y="-5" width={NODE_W + 10} height={NODE_H + 10} rx="14" fill="none" stroke="#fb7185" strokeOpacity="0.35" strokeWidth="2"><animate attributeName="stroke-opacity" values="0.6;0.1;0.6" dur="2s" repeatCount="indefinite" /></rect>}
                <rect width={NODE_W} height={NODE_H} rx="10" fill={s.fill} stroke={sel === n.id ? '#a5b4fc' : s.stroke} strokeWidth={sel === n.id ? 2.5 : 1.5} />
                <circle cx="14" cy="18" r="4" fill={s.stroke} />
                <text x="25" y="22" fill={s.text} fontSize="12.5" fontWeight="600" fontFamily="Inter, Segoe UI, sans-serif">{n.id.length > 19 ? n.id.slice(0, 18) + '…' : n.id}</text>
                <text x="14" y="41" fill="#94a3b8" fontSize="10.5" fontFamily="Inter, Segoe UI, sans-serif">
                  {s.label}{n.onset ? ` · ${fmtTime(n.onset)}` : n.tier ? ` · ${n.tier}` : ''}{imp?.error_lines ? ` · ${imp.error_lines} err` : ''}
                </text>
              </g>
            )
          })}
        </svg>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-slate-400">
        {Object.entries(STYLE).map(([k, s]) => (
          <span key={k} className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.stroke }} />{s.label}</span>
        ))}
        <span className="flex items-center gap-1.5"><span className="h-0.5 w-5 bg-rose-400" />Failure propagation path</span>
        <span className="text-slate-500">Arrows point from caller to dependency. Click a service for details.</span>
      </div>
      {selected && (
        <div className="mt-3 grid gap-3 rounded-lg border border-ink-700 bg-ink-850 p-4 text-sm sm:grid-cols-4">
          <div><div className="label">Service</div><div className="font-medium text-white">{selected.id}</div><div className="text-xs text-slate-500">{selected.tier || 'tier n/a'} · {selected.owner || 'owner n/a'}</div></div>
          <div><div className="label">Status</div><div className="text-slate-200">{STYLE[kind(selected)].label}</div><div className="text-xs text-slate-500">onset {fmtTime(selected.onset, true)}</div></div>
          <div><div className="label">Calls</div><div className="text-slate-300">{selected.callees.join(', ') || '—'}</div></div>
          <div><div className="label">Called by</div><div className="text-slate-300">{selected.callers.join(', ') || '—'}</div></div>
          {impactBy[selected.id] && <div className="sm:col-span-4"><div className="label">Anomalous metrics</div><div className="text-slate-300">{impactBy[selected.id].metrics.join(', ') || 'none (log errors only)'}</div></div>}
        </div>
      )}
    </div>
  )
}
