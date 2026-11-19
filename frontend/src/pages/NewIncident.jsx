import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { LineChart as LineIcon, ScrollText, GitCommit, Network, Play, Download, FlaskConical, AlertTriangle, Info, XCircle, ShieldCheck } from 'lucide-react'
import { api } from '../lib/api'
import FileDrop from '../components/FileDrop'
import { PageHeader, ErrorBox, Spinner, SevBadge } from '../components/ui'

const KINDS = [
  { key: 'metrics', label: 'Metrics CSV', icon: LineIcon, accept: '.csv,.txt', hint: 'timestamp,service,metric,value (long) or one column per metric (wide)' },
  { key: 'logs', label: 'Application logs', icon: ScrollText, accept: '.log,.txt,.json,.jsonl,.ndjson', hint: '"<ISO time> LEVEL [service] message" lines or JSON lines' },
  { key: 'changes', label: 'Deployment / change records', icon: GitCommit, accept: '.json,.csv', hint: 'JSON list or CSV: timestamp, service, type, description, version' },
  { key: 'dependencies', label: 'Service dependency map', icon: Network, accept: '.json,.csv', hint: 'JSON {services, edges:[{from,to}]}, adjacency map, or CSV from,to' },
]

const span = (r) => (r ? `${r.start.slice(0, 16).replace('T', ' ')} → ${r.end.slice(0, 16).replace('T', ' ')} UTC (${Math.round(r.minutes)} min)` : 'no timestamps')
const n = (x) => (x ?? 0).toLocaleString()
const DESCRIBE = {
  metrics: (s) => `${n(s.points)} points · ${s.series} series · ${s.services.length} services · ${s.metrics.length} metrics · every ${s.sample_interval_s ?? '?'}s · ${span(s.time_range)}`,
  logs: (s) => `${n(s.lines)} lines · ${n(s.levels.ERROR || 0)} errors · ${n(s.levels.WARN || 0)} warnings · ${s.services.length} services · ${span(s.time_range)}`,
  changes: (s) => `${s.records} changes (${Object.entries(s.types).map(([k, v]) => `${v} ${k.replace('_', ' ')}`).join(', ') || 'none'}) · ${span(s.time_range)}`,
  dependencies: (s) => `${s.services.length} services · ${s.edges} dependencies`,
}
const CROSS_ICON = { error: [XCircle, 'text-rose-300'], warning: [AlertTriangle, 'text-amber-300'], info: [Info, 'text-slate-400'] }

export default function NewIncident() {
  const nav = useNavigate()
  const [form, setForm] = useState({ title: '', description: '', severity: 'SEV-2' })
  const [files, setFiles] = useState({})
  const [samples, setSamples] = useState([])
  const [busy, setBusy] = useState(false)
  const [sampleBusy, setSampleBusy] = useState(null)
  const [error, setError] = useState(null)
  const [report, setReport] = useState(null)
  const [checking, setChecking] = useState(false)
  const reqId = useRef(0)

  useEffect(() => { api.samples().then(setSamples).catch(() => {}) }, [])

  // Validate the selected files on the server as soon as they change, so problems show before submitting.
  useEffect(() => {
    const chosen = Object.entries(files).filter(([, f]) => f)
    if (!chosen.length) { setReport(null); return }
    const my = ++reqId.current
    setChecking(true); setReport(null)
    const t = setTimeout(async () => {
      const fd = new FormData()
      chosen.forEach(([k, f]) => fd.append(k, f))
      try { const r = await api.validate(fd); if (my === reqId.current) setReport(r) }
      catch (e) { if (my === reqId.current) setError(e.message) }
      finally { if (my === reqId.current) setChecking(false) }
    }, 250)
    return () => clearTimeout(t)
  }, [files])

  const checkFor = (k) => {
    const f = report?.files?.[k]
    if (!f) return null
    return { ok: f.ok, error: f.error, warnings: f.warnings, text: f.summary ? DESCRIBE[k](f.summary) : null }
  }
  const blocked = checking || (report && !report.can_analyze)

  const submit = async (e) => {
    e.preventDefault()
    setError(null)
    if (!form.title.trim()) return setError('Give the incident a title.')
    if (!files.metrics && !files.logs) return setError('Upload at least a metrics CSV or a log file.')
    if (report && !report.can_analyze) return setError('Fix the file problems shown above before starting the analysis.')
    const fd = new FormData()
    Object.entries(form).forEach(([k, v]) => fd.append(k, v))
    Object.entries(files).forEach(([k, f]) => f && fd.append(k, f))
    setBusy(true)
    try { const inc = await api.upload(fd); nav(`/incidents/${inc.id}`) } catch (err) { setError(err.message); setBusy(false) }
  }

  const runSample = async (id) => {
    setSampleBusy(id)
    try { const inc = await api.fromSample(id); nav(`/incidents/${inc.id}`) } catch (e) { setError(e.message); setSampleBusy(null) }
  }

  return (
    <div>
      <PageHeader title="New incident" subtitle="Upload the telemetry around the incident window. The agent will investigate as soon as you submit." />
      <div className="grid gap-6 xl:grid-cols-3">
        <form onSubmit={submit} className="card card-pad space-y-5 xl:col-span-2">
          <div className="grid gap-4 sm:grid-cols-4">
            <div className="sm:col-span-3">
              <label className="label mb-1.5 block" htmlFor="title">Incident title</label>
              <input id="title" className="input" placeholder="e.g. Checkout failures and elevated 502s" value={form.title} onChange={(e) => { setForm({ ...form, title: e.target.value }); setError(null) }} />
            </div>
            <div>
              <label className="label mb-1.5 block" htmlFor="sev">Severity</label>
              <select id="sev" className="input" value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value })}>
                {['SEV-1', 'SEV-2', 'SEV-3', 'SEV-4'].map((s) => <option key={s}>{s}</option>)}
              </select>
            </div>
          </div>
          <div>
            <label className="label mb-1.5 block" htmlFor="desc">What are users seeing? (optional)</label>
            <textarea id="desc" rows={3} className="input" placeholder="Symptoms, alerts fired, customer reports…" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            {KINDS.map((k) => (
              <FileDrop key={k.key} label={k.label} hint={k.hint} accept={k.accept} icon={k.icon} required={k.key === 'metrics' || k.key === 'logs'}
                file={files[k.key]} onChange={(f) => { setFiles({ ...files, [k.key]: f }); setError(null) }} check={files[k.key] ? checkFor(k.key) : null} checking={checking} />
            ))}
          </div>
          {report && (
            <div className={`rounded-lg border p-3 text-xs ${report.can_analyze ? 'border-ink-700 bg-ink-950/40' : 'border-rose-900/60 bg-rose-950/30'}`}>
              <div className={`mb-1.5 flex items-center gap-1.5 font-semibold ${report.can_analyze ? 'text-emerald-300' : 'text-rose-300'}`}>
                {report.can_analyze ? <ShieldCheck size={14} /> : <XCircle size={14} />}{report.can_analyze ? 'Files validated: ready to analyze' : 'Cannot analyze yet'}
              </div>
              <ul className="space-y-1">
                {report.cross_checks.map((c) => { const [Icon, color] = CROSS_ICON[c.level]; return <li key={c.message} className={`flex gap-1.5 ${color}`}><Icon size={13} className="mt-px shrink-0" />{c.message}</li> })}
                {!report.can_analyze && Object.values(report.files).some((f) => !f.ok) && <li className="flex gap-1.5 text-rose-300"><XCircle size={13} className="mt-px shrink-0" />Fix or remove the file marked in red above.</li>}
                {report.can_analyze && report.cross_checks.length === 0 && <li className="text-slate-400">All files line up: overlapping time windows and matching service names.</li>}
              </ul>
            </div>
          )}
          <ErrorBox error={error} />
          <div className="flex items-center justify-between border-t border-ink-700 pt-4">
            <p className="text-xs text-slate-500">Files are checked on the server as you add them. Max 50 MB each.</p>
            <button type="submit" className="btn-primary" disabled={busy || blocked}>{busy ? <Spinner /> : <Play size={16} />} Upload & investigate</button>
          </div>
        </form>

        <aside className="space-y-4">
          <div className="card card-pad">
            <div className="flex items-center gap-2 text-sm font-semibold text-white"><FlaskConical size={16} className="text-indigo-300" /> Use a sample incident</div>
            <p className="mt-1 text-xs text-slate-500">Realistic datasets with red herrings. Download the files to see the expected formats.</p>
            <ul className="mt-4 space-y-3">
              {samples.map((s) => (
                <li key={s.id} className="rounded-lg border border-ink-700 bg-ink-950/40 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-slate-100">{s.title}</span>
                    <SevBadge sev={s.severity} />
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {Object.keys(s.files || {}).map((k) => (
                      <a key={k} href={api.sampleFileUrl(s.id, k)} className="flex items-center gap-1 rounded bg-ink-700 px-1.5 py-0.5 text-[11px] text-slate-300 hover:text-white"><Download size={11} />{s.files[k].name}</a>
                    ))}
                  </div>
                  <button onClick={() => runSample(s.id)} disabled={!!sampleBusy} className="btn-ghost mt-3 w-full py-1.5 text-xs">
                    {sampleBusy === s.id ? <Spinner size={13} /> : <Play size={13} />} Investigate sample
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </aside>
      </div>
    </div>
  )
}
