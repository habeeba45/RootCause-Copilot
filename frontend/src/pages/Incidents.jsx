import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Trash2, UploadCloud, Inbox, Search } from 'lucide-react'
import { api } from '../lib/api'
import { pct, fmtTime, timeAgo } from '../lib/format'
import { PageHeader, StatusBadge, SevBadge, Empty, ErrorBox } from '../components/ui'

export default function Incidents() {
  const [items, setItems] = useState(null)
  const [q, setQ] = useState('')
  const [error, setError] = useState(null)
  const load = () => api.incidents().then(setItems).catch((e) => setError(e.message))
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t) }, [])

  const remove = async (id) => {
    if (!window.confirm(`Delete incident ${id}? This cannot be undone.`)) return
    try { await api.remove(id); load() } catch (e) { setError(e.message) }
  }
  const shown = (items || []).filter((i) => `${i.id} ${i.title} ${i.root_cause || ''} ${i.root_service || ''}`.toLowerCase().includes(q.toLowerCase()))

  return (
    <div>
      <PageHeader title="Incidents" subtitle="Every investigation the agent has run.">
        <div className="relative"><Search size={15} className="absolute left-3 top-2.5 text-slate-500" /><input className="input w-64 pl-9" placeholder="Search incidents" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        <Link to="/incidents/new" className="btn-primary"><UploadCloud size={16} /> New incident</Link>
      </PageHeader>
      <ErrorBox error={error} />
      {items && items.length === 0 ? (
        <Empty icon={Inbox} title="No incidents yet">Upload telemetry or run a sample from the dashboard.</Empty>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr className="border-b border-ink-700 text-left">
              {['Incident', 'Severity', 'Status', 'Probable root cause', 'Confidence', 'Onset', 'Created', ''].map((h) => <th key={h} className="label px-4 py-3 font-semibold">{h}</th>)}
            </tr></thead>
            <tbody className="divide-y divide-ink-700/70">
              {shown.map((i) => (
                <tr key={i.id} className="hover:bg-ink-850">
                  <td className="px-4 py-3"><Link to={`/incidents/${i.id}`} className="font-medium text-slate-100 hover:text-indigo-300">{i.title}</Link><div className="font-mono text-[11px] text-slate-500">{i.id} · {i.source}</div></td>
                  <td className="px-4 py-3"><SevBadge sev={i.severity} /></td>
                  <td className="px-4 py-3"><StatusBadge status={i.status} /></td>
                  <td className="max-w-sm px-4 py-3 text-slate-300"><div className="line-clamp-2">{i.root_cause || '—'}</div>{i.verdict === 'inconclusive' && <span className="chip mt-1 bg-amber-500/15 text-amber-300">not confirmed</span>}</td>
                  <td className="px-4 py-3 font-semibold tabular-nums text-white">{pct(i.confidence)}</td>
                  <td className="px-4 py-3 text-slate-400">{fmtTime(i.onset, true)}</td>
                  <td className="px-4 py-3 text-slate-500">{timeAgo(i.created_at)}</td>
                  <td className="px-4 py-3 text-right"><button onClick={() => remove(i.id)} disabled={i.status === 'analyzing'} className="rounded p-1.5 text-slate-500 hover:bg-rose-950/50 hover:text-rose-300 disabled:opacity-30" aria-label="Delete"><Trash2 size={15} /></button></td>
                </tr>
              ))}
            </tbody>
          </table>
          {items === null && <p className="p-5 text-sm text-slate-500">Loading…</p>}
        </div>
      )}
    </div>
  )
}
