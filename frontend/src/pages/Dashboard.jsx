import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, Cell } from 'recharts'
import { Siren, CheckCircle2, Gauge, Timer, GitCommit, Play, ArrowRight, UploadCloud, Inbox } from 'lucide-react'
import { api } from '../lib/api'
import { pct, timeAgo } from '../lib/format'
import { StatCard, StatusBadge, SevBadge, Spinner, Empty, PageHeader, ErrorBox } from '../components/ui'

export default function Dashboard() {
  const nav = useNavigate()
  const [stats, setStats] = useState(null)
  const [incidents, setIncidents] = useState([])
  const [samples, setSamples] = useState([])
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () => Promise.all([api.stats(), api.incidents()])
      .then(([s, i]) => { if (alive) { setStats(s); setIncidents(i); setError(null) } })
      .catch((e) => alive && setError(e.message))
    load()
    api.samples().then((s) => alive && setSamples(s)).catch(() => {})
    const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const runSample = async (id) => {
    setBusy(id)
    try { const inc = await api.fromSample(id); nav(`/incidents/${inc.id}`) } catch (e) { setError(e.message); setBusy(null) }
  }

  return (
    <div className="space-y-6">
      <PageHeader title="Incident command center" subtitle="AI-assisted root cause analysis across metrics, logs, changes and service dependencies.">
        <Link to="/incidents/new" className="btn-primary"><UploadCloud size={16} /> Analyze new incident</Link>
      </PageHeader>
      <ErrorBox error={error} />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <StatCard label="Incidents" value={stats?.total ?? '—'} sub={`${stats?.analyzing ?? 0} analyzing · ${stats?.failed ?? 0} failed`} icon={Siren} accent="text-rose-300" />
        <StatCard label="RCAs completed" value={stats?.completed ?? '—'} sub={`${stats?.inconclusive ?? 0} inconclusive (not enough evidence)`} icon={CheckCircle2} accent="text-emerald-300" />
        <StatCard label="Avg. confidence" value={pct(stats?.avg_confidence)} sub="confirmed root causes only" icon={Gauge} />
        <StatCard label="Avg. time to RCA" value={stats?.avg_duration_s != null ? `${stats.avg_duration_s.toFixed(1)}s` : '—'} sub="agent compute time (excludes display pacing)" icon={Timer} accent="text-sky-300" />
        <StatCard label="Change-induced" value={pct(stats?.change_induced_pct)} sub="caused by a deploy / config change" icon={GitCommit} accent="text-purple-300" />
      </div>

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">Try a realistic sample incident</h2>
          <span className="text-xs text-slate-500">Each sample includes metrics, logs, change records and a dependency map</span>
        </div>
        <div className="grid gap-4 lg:grid-cols-3">
          {samples.map((s) => (
            <div key={s.id} className="card card-pad flex flex-col">
              <div className="flex items-center justify-between"><SevBadge sev={s.severity} /><span className="font-mono text-[11px] text-slate-500">{s.id}</span></div>
              <h3 className="mt-3 font-medium text-white">{s.title}</h3>
              <p className="mt-1 flex-1 text-sm text-slate-400">{s.description}</p>
              <button onClick={() => runSample(s.id)} disabled={!!busy} className="btn-ghost mt-4 w-full">
                {busy === s.id ? <Spinner /> : <Play size={15} />} Run investigation
              </button>
            </div>
          ))}
          {!samples.length && <div className="card card-pad text-sm text-slate-500 lg:col-span-3">Loading samples…</div>}
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-3">
        <section className="card xl:col-span-2">
          <div className="flex items-center justify-between border-b border-ink-700 px-5 py-3.5">
            <h2 className="text-sm font-semibold text-white">Recent incidents</h2>
            <Link to="/incidents" className="flex items-center gap-1 text-xs text-indigo-300 hover:text-indigo-200">View all <ArrowRight size={13} /></Link>
          </div>
          {incidents.length === 0 ? (
            <div className="p-5"><Empty icon={Inbox} title="No incidents yet">Run a sample above or upload your own telemetry.</Empty></div>
          ) : (
            <ul className="divide-y divide-ink-700/70">
              {incidents.slice(0, 6).map((i) => (
                <li key={i.id}>
                  <Link to={`/incidents/${i.id}`} className="flex items-center gap-4 px-5 py-3.5 hover:bg-ink-850">
                    <SevBadge sev={i.severity} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-slate-100">{i.title}</div>
                      <div className="truncate text-xs text-slate-500">{i.root_cause || (i.status === 'analyzing' ? 'Agent investigating…' : i.error || 'Not analyzed')}</div>
                    </div>
                    {i.verdict === 'inconclusive' && <span className="chip bg-amber-500/15 text-amber-300">inconclusive</span>}
                    {i.confidence != null && i.verdict === 'confirmed' && <span className="text-sm font-semibold tabular-nums text-white">{pct(i.confidence)}</span>}
                    <StatusBadge status={i.status} />
                    <span className="hidden w-16 text-right text-xs text-slate-500 sm:block">{timeAgo(i.created_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="card card-pad">
          <h2 className="text-sm font-semibold text-white">Root causes by failure mode</h2>
          <p className="mb-3 text-xs text-slate-500">Across completed analyses</p>
          {stats?.by_failure_mode?.length ? (
            <ResponsiveContainer width="100%" height={Math.max(160, stats.by_failure_mode.length * 44)}>
              <BarChart data={stats.by_failure_mode} layout="vertical" margin={{ left: 0, right: 16 }}>
                <XAxis type="number" allowDecimals={false} hide />
                <YAxis type="category" dataKey="label" width={150} tickLine={false} axisLine={false} tick={{ fill: '#94a3b8', fontSize: 11 }} />
                <Tooltip cursor={{ fill: '#131c31' }} contentStyle={{ background: '#0b1120', border: '1px solid #2a3756', borderRadius: 8, fontSize: 12 }} />
                <Bar dataKey="count" radius={[0, 6, 6, 0]} barSize={18}>
                  {stats.by_failure_mode.map((_, i) => <Cell key={i} fill={['#fb7185', '#818cf8', '#fbbf24', '#34d399', '#38bdf8'][i % 5]} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <p className="py-10 text-center text-sm text-slate-500">No completed analyses yet.</p>}
          {stats?.by_service?.length > 0 && (
            <div className="mt-4 border-t border-ink-700 pt-3">
              <div className="label mb-2">Most implicated services</div>
              <div className="flex flex-wrap gap-2">{stats.by_service.map((s) => <span key={s.service} className="chip bg-ink-700 text-slate-300">{s.service} · {s.count}</span>)}</div>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
