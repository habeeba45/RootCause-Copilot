import { createContext, useContext, useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import { fmtTime } from '../lib/format'

// Shares the evidence ledger with every component that shows citations, and lets a chip jump to the Evidence tab.
export const EvidenceContext = createContext({ byId: {}, focus: () => {} })

export function EvidenceProvider({ ledger, onFocus, children }) {
  const value = useMemo(() => ({ byId: Object.fromEntries((ledger || []).map((e) => [e.id, e])), focus: onFocus || (() => {}) }), [ledger, onFocus])
  return <EvidenceContext.Provider value={value}>{children}</EvidenceContext.Provider>
}

export function EvidenceChip({ id }) {
  const { byId, focus } = useContext(EvidenceContext)
  const e = byId[id]
  const loaded = Object.keys(byId).length > 0 // while an analysis is still running the ledger is not loaded yet
  return (
    <button type="button" onClick={() => focus(id)} title={e ? e.summary : loaded ? 'Unknown evidence' : 'Evidence details appear when the analysis completes'}
      className={`mx-0.5 inline-flex items-center rounded px-1.5 py-px align-baseline font-mono text-[10.5px] font-semibold transition ${e || !loaded ? 'bg-indigo-500/15 text-indigo-200 hover:bg-indigo-500/30' : 'bg-rose-500/15 text-rose-300'}`}>
      {id}
    </button>
  )
}

export function EvidenceChips({ ids }) {
  if (!ids?.length) return null
  return <span className="inline-flex flex-wrap">{ids.map((id) => <EvidenceChip key={id} id={id} />)}</span>
}

// Renders text containing citations like "[E3]" or "[E3, E7]" with clickable chips.
export function CitedText({ text, className = '' }) {
  if (!text) return null
  const parts = String(text).split(/\[((?:E\d+)(?:\s*,\s*E\d+)*)\]/g)
  return (
    <span className={className}>
      {parts.map((p, i) => (i % 2 === 1 ? <EvidenceChips key={i} ids={p.split(/\s*,\s*/)} /> : <span key={i}>{p}</span>))}
    </span>
  )
}

const KIND_STYLE = {
  metric_anomaly: 'bg-amber-500/10 text-amber-300', log_signature: 'bg-sky-500/10 text-sky-300', log_reference: 'bg-cyan-500/10 text-cyan-300',
  log_precursor: 'bg-teal-500/10 text-teal-300', deployment_marker: 'bg-purple-500/10 text-purple-300', change: 'bg-purple-500/10 text-purple-300',
  dependency_root: 'bg-rose-500/10 text-rose-300', dependency_victim: 'bg-orange-500/10 text-orange-300', propagation: 'bg-rose-500/10 text-rose-300',
  change_impact: 'bg-fuchsia-500/10 text-fuchsia-300', caller_lag: 'bg-fuchsia-500/10 text-fuchsia-300', log_search: 'bg-fuchsia-500/10 text-fuchsia-300',
  onset: 'bg-indigo-500/10 text-indigo-300', coverage: 'bg-slate-500/15 text-slate-300', traffic_shift: 'bg-slate-500/15 text-slate-300',
}

export function EvidenceLedger({ ledger, highlight }) {
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('all')
  const kinds = useMemo(() => ['all', ...new Set((ledger || []).map((e) => e.kind))], [ledger])
  const rows = (ledger || []).filter((e) => (kind === 'all' || e.kind === kind) && `${e.id} ${e.service || ''} ${e.summary}`.toLowerCase().includes(q.toLowerCase()))
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative"><Search size={14} className="absolute left-2.5 top-2.5 text-slate-500" /><input className="input w-56 py-1.5 pl-8 text-xs" placeholder="Filter evidence" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <select className="input w-auto py-1.5 text-xs" value={kind} onChange={(e) => setKind(e.target.value)}>
          {kinds.map((k) => <option key={k} value={k}>{k === 'all' ? 'All types' : k.replace(/_/g, ' ')}</option>)}
        </select>
        <span className="text-xs text-slate-500">{rows.length} of {ledger?.length || 0} items</span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-ink-700">
        <table className="w-full text-sm">
          <thead><tr className="border-b border-ink-700 bg-ink-850 text-left">{['ID', 'Type', 'Tool', 'Service', 'Time', 'Finding'].map((h) => <th key={h} className="label px-3 py-2">{h}</th>)}</tr></thead>
          <tbody className="divide-y divide-ink-700/60">
            {rows.map((e) => (
              <tr key={e.id} id={`ev-${e.id}`} className={highlight === e.id ? 'bg-indigo-500/15' : ''}>
                <td className="px-3 py-2 font-mono text-xs font-semibold text-indigo-200">{e.id}</td>
                <td className="px-3 py-2"><span className={`chip ${KIND_STYLE[e.kind] || 'bg-ink-700 text-slate-300'}`}>{e.kind.replace(/_/g, ' ')}</span></td>
                <td className="px-3 py-2 font-mono text-[11px] text-slate-500">{e.tool}</td>
                <td className="px-3 py-2 text-xs text-slate-300">{e.service || '—'}</td>
                <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] text-slate-500">{e.time ? fmtTime(e.time) : '—'}</td>
                <td className="px-3 py-2 text-xs leading-relaxed text-slate-300">{e.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function InvestigationLog({ log }) {
  if (!log?.length) return <p className="text-sm text-slate-500">No investigation log.</p>
  return (
    <ol className="space-y-2">
      {log.map((x) => (
        <li key={x.step} className="flex gap-3 rounded-lg border border-ink-700 bg-ink-950/30 px-3 py-2.5">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-ink-700 text-[11px] font-semibold text-slate-300">{x.step}</span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-mono font-semibold text-slate-100">{x.action}</span>
              {Object.keys(x.args || {}).length > 0 && <span className="font-mono text-slate-500">{Object.entries(x.args).map(([k, v]) => `${k}=${v}`).join(', ')}</span>}
              <span className="chip bg-ink-700 text-slate-400">{x.phase}</span>
              <span className={`chip ${x.planner === 'llm' ? 'bg-fuchsia-500/15 text-fuchsia-300' : 'bg-sky-500/10 text-sky-300'}`}>{x.planner === 'llm' ? 'LLM planner' : 'rule planner'}</span>
            </div>
            <p className="mt-1 text-xs text-slate-400"><span className="text-slate-500">Why:</span> {x.reason}</p>
            <p className="text-xs text-slate-300"><span className="text-slate-500">Result:</span> {x.outcome} <EvidenceChips ids={x.evidence?.slice(0, 12)} />{x.evidence?.length > 12 && <span className="text-slate-500"> +{x.evidence.length - 12}</span>}</p>
          </div>
        </li>
      ))}
    </ol>
  )
}
