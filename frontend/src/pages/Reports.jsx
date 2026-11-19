import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { FileText, FileCode2, FileJson, Inbox, ExternalLink } from 'lucide-react'
import { api } from '../lib/api'
import { pct, fmtTime } from '../lib/format'
import { PageHeader, SevBadge, Empty, ErrorBox } from '../components/ui'

export default function Reports() {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => { api.incidents().then((x) => setItems(x.filter((i) => i.status === 'completed'))).catch((e) => setError(e.message)) }, [])
  return (
    <div>
      <PageHeader title="RCA reports" subtitle="Download root cause analysis reports as HTML (print to PDF), Markdown or JSON." />
      <ErrorBox error={error} />
      {items && !items.length && <Empty icon={Inbox} title="No reports yet">Reports appear here once an investigation completes.</Empty>}
      <div className="grid gap-4 lg:grid-cols-2">
        {(items || []).map((i) => (
          <div key={i.id} className="card card-pad">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2"><SevBadge sev={i.severity} /><span className="font-mono text-[11px] text-slate-500">{i.id}</span></div>
                <Link to={`/incidents/${i.id}`} className="mt-2 block font-medium text-white hover:text-indigo-300">{i.title}</Link>
                <p className="mt-1 text-sm text-slate-400">{i.root_cause}</p>
                <p className="mt-1 text-xs text-slate-500">Onset {fmtTime(i.onset, true)} · {i.affected_count} services affected · {i.analysis_mode}</p>
              </div>
              <div className="text-right"><div className="text-2xl font-semibold text-white">{pct(i.confidence)}</div><div className="text-[10px] uppercase tracking-wider text-slate-500">confidence</div></div>
            </div>
            <div className="mt-4 flex flex-wrap gap-2 border-t border-ink-700 pt-4">
              <a className="btn-ghost py-1.5 text-xs" href={api.reportUrl(i.id, 'html', false)} target="_blank" rel="noreferrer"><ExternalLink size={14} /> Open</a>
              <a className="btn-ghost py-1.5 text-xs" href={api.reportUrl(i.id, 'html')}><FileText size={14} /> HTML</a>
              <a className="btn-ghost py-1.5 text-xs" href={api.reportUrl(i.id, 'md')}><FileCode2 size={14} /> Markdown</a>
              <a className="btn-ghost py-1.5 text-xs" href={api.reportUrl(i.id, 'json')}><FileJson size={14} /> JSON</a>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
