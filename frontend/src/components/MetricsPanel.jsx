import { useMemo, useState } from 'react'
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, ReferenceLine, ReferenceArea, CartesianGrid, AreaChart, Area } from 'recharts'
import { METRIC_LABEL, fmtTime } from '../lib/format'

const PALETTE = ['#818cf8', '#fb7185', '#34d399', '#fbbf24', '#38bdf8', '#c084fc', '#f472b6', '#a3e635']
const tooltipStyle = { background: '#0b1120', border: '1px solid #2a3756', borderRadius: 8, fontSize: 12, color: '#e2e8f0' }

function MiniChart({ metric, points, onset, changeAt, baselineEnd, anomaly }) {
  const data = useMemo(() => points.map(([t, v]) => ({ t, label: fmtTime(t), v })), [points])
  const first = data[0]?.t
  return (
    <div className={`rounded-lg border p-3 ${anomaly ? 'border-rose-900/70 bg-rose-950/10' : 'border-ink-700 bg-ink-950/40'}`}>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-xs font-medium text-slate-300">{METRIC_LABEL[metric] || metric}</span>
        {anomaly && <span className="chip bg-rose-500/15 text-rose-300">z={anomaly.z_score} · {anomaly.pattern}</span>}
      </div>
      <ResponsiveContainer width="100%" height={130}>
        <AreaChart data={data} margin={{ top: 6, right: 6, left: -6, bottom: 0 }}>
          <defs>
            <linearGradient id={`g-${metric}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={anomaly ? '#fb7185' : '#818cf8'} stopOpacity={0.35} />
              <stop offset="100%" stopColor={anomaly ? '#fb7185' : '#818cf8'} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} />
          {baselineEnd && first && <ReferenceArea x1={first} x2={baselineEnd} fill="#334155" fillOpacity={0.15} />}
          <XAxis dataKey="t" tickFormatter={(v) => fmtTime(v)} minTickGap={40} tickLine={false} axisLine={false} />
          <YAxis tickLine={false} axisLine={false} width={40} tickFormatter={compact} />
          <Tooltip contentStyle={tooltipStyle} labelFormatter={(t) => fmtTime(t, true)} formatter={(v) => [v, METRIC_LABEL[metric] || metric]} />
          {changeAt && <ReferenceLine x={nearest(data, changeAt)} stroke="#c084fc" strokeDasharray="4 3" />}
          {onset && <ReferenceLine x={nearest(data, onset)} stroke="#fb7185" strokeDasharray="4 3" />}
          <Area type="monotone" dataKey="v" stroke={anomaly ? '#fb7185' : '#818cf8'} strokeWidth={1.6} fill={`url(#g-${metric})`} dot={false} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function compact(v) {
  const a = Math.abs(v)
  return a >= 1e6 ? `${+(v / 1e6).toFixed(1)}M` : a >= 1e3 ? `${+(v / 1e3).toFixed(1)}k` : `${+v.toFixed(1)}`
}

function nearest(data, iso) {
  if (!data.length) return undefined
  const target = new Date(iso).getTime()
  let best = data[0].t; let bd = Infinity
  for (const d of data) { const dd = Math.abs(new Date(d.t).getTime() - target); if (dd < bd) { bd = dd; best = d.t } }
  return best
}

export function ErrorTimeline({ timeline, onset }) {
  const { data, services } = useMemo(() => {
    const by = {}; const svcs = new Set()
    for (const r of timeline || []) { (by[r.minute] ||= { t: r.minute }); by[r.minute][r.service] = r.errors; svcs.add(r.service) }
    const keys = Object.keys(by).sort()
    const rows = []
    if (keys.length) {
      // zero-fill every minute from 30 min before the first error to the last one, so quiet periods are visible
      const start = new Date(keys[0]).getTime() - 30 * 60000
      const end = new Date(keys.at(-1)).getTime()
      for (let t = start; t <= end; t += 60000) {
        const k = new Date(t).toISOString().slice(0, 19) + 'Z'
        const row = { t: k }
        svcs.forEach((s) => { row[s] = by[k]?.[s] ?? 0 })
        rows.push(row)
      }
    }
    return { data: rows, services: [...svcs] }
  }, [timeline])
  if (!data.length) return <p className="text-sm text-slate-500">No error log lines were found.</p>
  return (
    <div>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={data} margin={{ top: 8, right: 12, left: -12, bottom: 0 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="t" tickFormatter={(v) => fmtTime(v)} minTickGap={40} tickLine={false} axisLine={false} />
          <YAxis tickLine={false} axisLine={false} allowDecimals={false} />
          <Tooltip contentStyle={tooltipStyle} labelFormatter={(t) => fmtTime(t, true)} />
          {onset && <ReferenceLine x={nearest(data, onset)} stroke="#fb7185" strokeDasharray="4 3" label={{ value: 'onset', fill: '#fb7185', fontSize: 11, position: 'insideTopRight' }} />}
          {services.map((s, i) => <Line key={s} type="monotone" dataKey={s} stroke={PALETTE[i % PALETTE.length]} dot={false} strokeWidth={1.8} connectNulls={false} isAnimationActive={false} />)}
        </LineChart>
      </ResponsiveContainer>
      <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
        {services.map((s, i) => <span key={s} className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: PALETTE[i % PALETTE.length] }} />{s}</span>)}
      </div>
    </div>
  )
}

export default function MetricsPanel({ result }) {
  const series = result?.charts?.series || {}
  const services = Object.keys(series)
  const root = result?.summary?.service
  const [svc, setSvc] = useState(services.includes(root) ? root : services[0])
  const anomalies = result?.evidence?.metric_anomalies || []
  const change = (result?.evidence?.changes || []).find((c) => c.id === result?.summary?.change_id)
  if (!services.length) return <p className="text-sm text-slate-500">No metrics were supplied for this incident.</p>
  const current = series[svc] || {}
  const affectedSet = new Set((result?.impact?.services || []).map((s) => s.service))
  return (
    <div>
      <div className="mb-4 flex flex-wrap gap-2">
        {services.map((s) => (
          <button key={s} onClick={() => setSvc(s)}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition ${s === svc ? 'border-indigo-500 bg-indigo-500/15 text-indigo-200' : 'border-ink-600 text-slate-400 hover:text-slate-200'}`}>
            {s === root && <span className="mr-1 text-rose-400">●</span>}{s}{!affectedSet.has(s) && <span className="ml-1 text-slate-500">(traffic)</span>}
          </button>
        ))}
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {Object.entries(current).map(([metric, pts]) => (
          <MiniChart key={metric} metric={metric} points={pts} onset={result.summary.onset} baselineEnd={result.charts.baseline_end}
            changeAt={change && change.service === svc ? change.timestamp : null}
            anomaly={anomalies.find((a) => a.service === svc && a.metric === metric)} />
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-4 text-xs text-slate-500">
        <span className="flex items-center gap-1.5"><span className="h-3 w-3 rounded-sm bg-slate-600/40" />Baseline window</span>
        <span className="flex items-center gap-1.5"><span className="h-0 w-4 border-t-2 border-dashed border-rose-400" />Incident onset</span>
        {change && <span className="flex items-center gap-1.5"><span className="h-0 w-4 border-t-2 border-dashed border-purple-400" />{change.id}</span>}
      </div>
    </div>
  )
}
