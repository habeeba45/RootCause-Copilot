import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { History, RefreshCw, FileText, FileCode2, FileJson, ArrowLeft, Target, GitCommit, Layers, CheckCircle2, Circle, Sparkles, AlertOctagon, AlertTriangle, ThumbsUp, ThumbsDown, HelpCircle, ChevronDown } from 'lucide-react'
import { api } from '../lib/api'
import { fmtTime, pct, bytes } from '../lib/format'
import ActivityLog from '../components/ActivityLog'
import DependencyGraph from '../components/DependencyGraph'
import MetricsPanel, { ErrorTimeline } from '../components/MetricsPanel'
import { HypothesisList, Timeline, Recommendations, CheckRow } from '../components/Analysis'
import { StatusBadge, SevBadge, ConfidenceRing, Spinner, ErrorBox } from '../components/ui'
import { EvidenceProvider, EvidenceLedger, InvestigationLog, CitedText, EvidenceChips } from '../components/Evidence'

const TABS = ['Overview', 'Evidence', 'Dependencies', 'Metrics & logs', 'Timeline', 'Report']
const STEPS = [
  { label: 'Metrics analysis', match: (e) => e.tool === 'metrics_analyzer' && e.type === 'tool_result' },
  { label: 'Log analysis', match: (e) => e.tool === 'log_analyzer' && e.type === 'tool_result' },
  { label: 'Change correlation', match: (e) => e.tool === 'change_detector' && e.type === 'tool_result' },
  { label: 'Dependency analysis', match: (e) => e.tool === 'dependency_analyzer' && e.type === 'tool_result' },
  { label: 'Hypothesis generation', match: (e) => e.type === 'hypothesis' },
  { label: 'Follow-up verification', match: (e) => e.type === 'decision' && /stopped follow-up|LLM analysis/.test(e.title) || (e.type === 'llm' && /grounded/.test(e.title)) },
  { label: 'Hypothesis testing', match: (e) => e.type === 'ranking' && /Final/.test(e.title) },
  { label: 'RCA report', match: (e) => e.type === 'conclusion' },
]

function useIncidentStream(id) {
  const [incident, setIncident] = useState(null)
  const [events, setEvents] = useState([])
  const [error, setError] = useState(null)
  const esRef = useRef(null)
  const pollRef = useRef(null)

  const refresh = useCallback(() => api.incident(id).then(setIncident).catch((e) => setError(e.message)), [id])

  const stop = () => { esRef.current?.close(); esRef.current = null; clearInterval(pollRef.current) }

  const follow = useCallback(() => {
    stop()
    setEvents([])
    let lastSeq = -1
    const add = (evs) => setEvents((prev) => {
      const fresh = evs.filter((e) => e.seq > (prev.at(-1)?.seq ?? -1))
      return fresh.length ? [...prev, ...fresh] : prev
    })
    if (typeof EventSource === 'undefined') return poll()
    const es = new EventSource(api.streamUrl(id))
    esRef.current = es
    es.addEventListener('activity', (m) => { const ev = JSON.parse(m.data); lastSeq = ev.seq; add([ev]) })
    es.addEventListener('done', () => { es.close(); refresh() })
    es.onerror = () => { es.close(); poll() } // fall back to polling if streaming is blocked
    function poll() {
      clearInterval(pollRef.current)
      pollRef.current = setInterval(async () => {
        try {
          const r = await api.events(id, lastSeq)
          if (r.events.length) { lastSeq = r.events.at(-1).seq; add(r.events) }
          if (r.status !== 'analyzing') { clearInterval(pollRef.current); refresh() }
        } catch { /* keep polling */ }
      }, 1000)
    }
  }, [id, refresh])

  useEffect(() => {
    let alive = true
    api.incident(id).then((inc) => {
      if (!alive) return
      setIncident(inc)
      if (inc.status === 'analyzing') follow()
      else api.events(id).then((r) => alive && setEvents(r.events))
    }).catch((e) => setError(e.message))
    return () => { alive = false; stop() }
  }, [id, follow])

  const rerun = async () => {
    setError(null)
    try { await api.analyze(id); setIncident((i) => ({ ...i, status: 'analyzing', result: null })); follow() } catch (e) { setError(e.message) }
  }
  return { incident, events, error, rerun }
}

function Progress({ events }) {
  const done = STEPS.map((s) => events.some(s.match))
  const current = done.indexOf(false)
  return (
    <div className="card card-pad">
      <div className="flex items-center gap-3">
        <Spinner size={20} className="text-indigo-300" />
        <div><h2 className="font-semibold text-white">Investigation in progress</h2><p className="text-sm text-slate-400">The agent is calling its tools and testing hypotheses. Results appear when ranking completes.</p></div>
      </div>
      <ol className="mt-5 grid gap-2 sm:grid-cols-2">
        {STEPS.map((s, i) => (
          <li key={s.label} className={`flex items-center gap-2.5 rounded-lg border px-3 py-2.5 text-sm ${done[i] ? 'border-emerald-900/60 bg-emerald-500/5 text-emerald-200' : i === current ? 'border-indigo-700 bg-indigo-500/10 text-indigo-200' : 'border-ink-700 text-slate-500'}`}>
            {done[i] ? <CheckCircle2 size={16} /> : i === current ? <Spinner size={16} /> : <Circle size={16} />} {s.label}
          </li>
        ))}
      </ol>
      <div className="mt-5 h-1.5 overflow-hidden rounded-full bg-ink-700"><div className="h-full rounded-full bg-indigo-500 transition-all duration-500" style={{ width: `${(done.filter(Boolean).length / STEPS.length) * 100}%` }} /></div>
    </div>
  )
}

function Overview({ inc }) {
  const r = inc.result
  const s = r.summary
  const top = r.hypotheses[0]
  const confirmed = s.verdict === 'confirmed'
  return (
    <div className="space-y-5">
      {!confirmed && (
        <div className="flex items-start gap-3 rounded-lg border border-amber-800/60 bg-amber-950/30 p-4 text-sm text-amber-100">
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-300" />
          <div><b>Inconclusive.</b> No hypothesis reached the evidence threshold, so the agent will not name a confirmed root cause. The leading hypothesis is shown below as a lead to verify.
            {r.data_gaps?.length > 0 && <ul className="mt-2 list-disc pl-5 text-amber-200/80">{r.data_gaps.map((g) => <li key={g}>{g}</li>)}</ul>}
          </div>
        </div>
      )}
      <div className="card overflow-hidden">
        <div className={`border-l-4 p-5 ${confirmed ? 'border-rose-500' : 'border-amber-500'}`}>
          <div className="flex flex-wrap items-start gap-5">
            <div className="min-w-0 flex-1">
              <div className={`label flex items-center gap-1.5 ${confirmed ? 'text-rose-300' : 'text-amber-300'}`}><Target size={13} /> {confirmed ? 'Probable root cause' : 'Leading hypothesis (not confirmed)'}</div>
              <h2 className="mt-1.5 text-xl font-semibold leading-snug text-white">{s.title}</h2>
              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                {s.service && <span className="chip bg-ink-700 text-slate-200"><Layers size={12} /> {s.service}</span>}
                <span className="chip bg-rose-500/10 text-rose-200">{s.failure_mode_label}</span>
                {s.change_id && <span className="chip bg-purple-500/10 text-purple-200"><GitCommit size={12} /> {s.change_id}</span>}
                <span className="chip bg-ink-700 text-slate-300">onset {fmtTime(s.onset, true)}</span>
                <span className="chip bg-ink-700 text-slate-300">{s.affected_count} services affected</span>
                <span className="chip bg-indigo-500/10 text-indigo-200">{s.analysis_mode?.startsWith('llm') ? <Sparkles size={12} /> : null}{s.analysis_mode}</span>
              </div>
            </div>
            <div className="flex flex-col items-center">
              <ConfidenceRing value={s.confidence} label={confirmed ? 'confidence' : 'relative'} />
              <span className="mt-1 text-[10px] text-slate-500">score {s.score} · {s.hypothesis_verdict}</span>
            </div>
          </div>
          <p className="mt-4 text-sm leading-relaxed text-slate-300"><CitedText text={r.narrative.executive_summary} /></p>
          <details className="mt-2 text-xs text-slate-500"><summary className="cursor-pointer hover:text-slate-300">How is this confidence calculated?</summary><p className="mt-1">{s.confidence_basis}</p></details>
          {r.impact.propagation_path?.length > 1 && (
            <div className="mt-4 flex flex-wrap items-center gap-1.5 text-xs">
              <span className="label mr-1">Propagation</span>
              {r.impact.propagation_path.map((p, i) => (
                <span key={p} className="flex items-center gap-1.5"><span className={`rounded-md px-2 py-1 font-medium ${i === 0 ? 'bg-rose-500/20 text-rose-200' : 'bg-amber-500/10 text-amber-200'}`}>{p}</span>{i < r.impact.propagation_path.length - 1 && <span className="text-slate-600">→</span>}</span>
              ))}
            </div>
          )}
        </div>
      </div>

      {r.llm_analysis && <LLMAnalysis la={r.llm_analysis} agrees={s.llm_agrees} />}

      <div className="grid gap-5 2xl:grid-cols-2 [&>*]:min-w-0">
        <div className="card card-pad">
          <h3 className="text-sm font-semibold text-white">Why the agent believes this</h3>
          <p className="mb-2 text-xs leading-relaxed text-slate-400"><CitedText text={r.narrative.root_cause_explanation} /></p>
          <ul className="divide-y divide-ink-700/70">{top?.checks.map((c, i) => <CheckRow key={i} c={c} />)}</ul>
        </div>
        <div className="card card-pad">
          <h3 className="text-sm font-semibold text-white">Hypotheses tested & ranked</h3>
          <p className="mb-3 text-xs text-slate-500">Each hypothesis was scored against explicit evidence checks; expand to see them.</p>
          <HypothesisList hypotheses={r.hypotheses} />
        </div>
      </div>

      <div className="card card-pad">
        <h3 className="mb-3 text-sm font-semibold text-white">Recommended solutions</h3>
        <Recommendations recs={r.recommendations} />
      </div>
    </div>
  )
}

const STANCE = { supports: [ThumbsUp, 'text-emerald-300'], doubts: [HelpCircle, 'text-amber-300'], refutes: [ThumbsDown, 'text-rose-300'] }

function LLMAnalysis({ la, agrees }) {
  const d = la.dropped || {}
  const removed = (d.citations?.length || 0) + (d.assessments || 0) + (d.recommendations || 0)
  return (
    <div className="card card-pad">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="label flex items-center gap-1.5 text-fuchsia-300"><Sparkles size={13} /> LLM analysis · {la.provider}/{la.model}</div>
        {la.preferred_hypothesis_id && (
          <span className={`chip ${agrees ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300'}`}>
            {agrees ? `Agrees with evidence ranking (${la.preferred_hypothesis_id})` : `Prefers ${la.preferred_hypothesis_id}; ranking unchanged (scores come from evidence)`}
          </span>
        )}
      </div>
      {la.explanation ? <p className="mt-2 text-sm leading-relaxed text-slate-200"><CitedText text={la.explanation} /></p>
        : <p className="mt-2 text-sm text-amber-200">The LLM's explanation was discarded because it did not cite any valid evidence.</p>}
      {la.assessments?.length > 0 && (
        <ul className="mt-3 space-y-1.5">
          {la.assessments.map((a, i) => {
            const [Icon, color] = STANCE[a.stance] || STANCE.doubts
            return <li key={i} className="flex gap-2 text-xs text-slate-300"><Icon size={14} className={`mt-0.5 shrink-0 ${color}`} /><span><b className="text-slate-100">{a.hypothesis_id}</b> {a.stance}: <CitedText text={a.reasoning} /> <EvidenceChips ids={a.evidence} /></span></li>
          })}
        </ul>
      )}
      {la.caveats && <p className="mt-2 text-xs text-slate-500">Caveats: <CitedText text={la.caveats} /></p>}
      <p className="mt-2 text-[11px] text-slate-500">Grounding check: {removed === 0 ? 'every statement cites valid evidence.' : `removed ${d.citations?.length || 0} invalid citations, ${d.assessments || 0} unsupported assessments and ${d.recommendations || 0} unsupported recommendations.`}</p>
    </div>
  )
}

function ReportTab({ inc, runId }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <a className="btn-primary" href={api.reportUrl(inc.id, 'html', true, runId)}><FileText size={15} /> Download HTML</a>
        <a className="btn-ghost" href={api.reportUrl(inc.id, 'md', true, runId)}><FileCode2 size={15} /> Markdown</a>
        <a className="btn-ghost" href={api.reportUrl(inc.id, 'json', true, runId)}><FileJson size={15} /> JSON</a>
        <span className="self-center text-xs text-slate-500">Tip: open the HTML report and print to save as PDF.</span>
      </div>
      <iframe title="RCA report preview" src={api.reportUrl(inc.id, 'html', false, runId)} className="h-[75vh] w-full rounded-xl border border-ink-700 bg-white" />
    </div>
  )
}

export default function IncidentDetail() {
  const { id } = useParams()
  const { incident: inc, events: liveEvents, error, rerun: rerunLatest } = useIncidentStream(id)
  const [runs, setRuns] = useState([])
  const [viewRun, setViewRun] = useState(null) // a past run selected from the investigation history
  const [runError, setRunError] = useState(null)
  useEffect(() => {
    if (!inc || inc.status === 'analyzing') return
    api.runs(id).then(setRuns).catch(() => setRuns([]))
  }, [id, inc?.status, inc?.current_run_id])
  const rerun = () => { setViewRun(null); rerunLatest() }
  const selectRun = async (runId) => {
    setRunError(null)
    if (!runId || Number(runId) === inc.current_run_id) return setViewRun(null)
    try { setViewRun(await api.run(id, runId)) } catch (e) { setRunError(e.message) }
  }
  const [tab, setTab] = useState('Overview')
  const [highlight, setHighlight] = useState(null)
  const [showWarn, setShowWarn] = useState(false)
  const focusEvidence = useCallback((eid) => {
    setTab('Evidence'); setHighlight(eid)
    setTimeout(() => document.getElementById(`ev-${eid}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 60)
  }, [])
  if (!inc) return error ? <ErrorBox error={error} /> : <div className="flex items-center gap-2 text-slate-400"><Spinner /> Loading incident…</div>
  const running = inc.status === 'analyzing'
  const r = viewRun ? viewRun.result : inc.result
  const events = viewRun ? viewRun.events : liveEvents
  const warnings = inc.validation?.warnings || []

  return (
    <EvidenceProvider ledger={r?.evidence_ledger} onFocus={focusEvidence}>
    <div>
      <Link to="/incidents" className="mb-3 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-300"><ArrowLeft size={13} /> All incidents</Link>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2"><SevBadge sev={inc.severity} /><StatusBadge status={inc.status} /><span className="font-mono text-xs text-slate-500">{inc.id}</span></div>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-white">{inc.title}</h1>
          {inc.description && <p className="mt-1 max-w-3xl text-sm text-slate-400">{inc.description}</p>}
          <div className="mt-2 flex flex-wrap gap-2">
            {Object.entries(inc.files || {}).map(([k, f]) => <span key={k} className="chip bg-ink-800 text-slate-400 ring-1 ring-ink-700">{k}: {f.name} · {bytes(f.bytes)}</span>)}
            {warnings.length > 0 && (
              <button onClick={() => setShowWarn(!showWarn)} className="chip bg-amber-500/10 text-amber-300 ring-1 ring-amber-800/50 hover:bg-amber-500/20">
                <AlertTriangle size={11} /> {warnings.length} data-quality note{warnings.length > 1 ? 's' : ''} <ChevronDown size={11} className={showWarn ? 'rotate-180' : ''} />
              </button>
            )}
          </div>
          {showWarn && <ul className="mt-2 max-w-3xl list-disc space-y-0.5 pl-5 text-xs text-amber-200/80">{warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
        </div>
        <div className="flex flex-wrap gap-2">
          <button onClick={rerun} disabled={running} className="btn-ghost">{running ? <Spinner /> : <RefreshCw size={15} />} {running ? 'Analyzing' : inc.status === 'uploaded' ? 'Run analysis' : 'Re-run analysis'}</button>
          {r && <a className="btn-primary" href={api.reportUrl(inc.id, 'html', true, viewRun?.id)}><FileText size={15} /> Download RCA</a>}
        </div>
      </div>
      {runs.length > 0 && !running && (
        <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
          <History size={14} className="text-slate-500" />
          <span className="label">Investigation history</span>
          <select className="input w-auto py-1 text-xs" value={viewRun?.id ?? inc.current_run_id ?? ''} onChange={(e) => selectRun(e.target.value)}>
            {runs.map((x) => (
              <option key={x.id} value={x.id}>
                Run #{x.number}{x.id === inc.current_run_id ? ' (latest)' : ''} · {x.started_at?.replace('T', ' ').slice(0, 16)} UTC · {x.status === 'completed' ? `${x.verdict}${x.service ? ` · ${x.service}` : ''}` : x.status} · {x.mode || 'rule-based'}
              </option>
            ))}
          </select>
          <span className="text-slate-500">{runs.length} run{runs.length > 1 ? 's' : ''} stored</span>
        </div>
      )}
      {viewRun && (
        <div className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-sky-800/60 bg-sky-950/30 px-4 py-2.5 text-sm text-sky-100">
          <span>Viewing <b>run #{viewRun.number}</b> from {viewRun.started_at?.replace('T', ' ').slice(0, 16)} UTC ({viewRun.status}). This is a saved past investigation, not the latest.</span>
          <button className="btn-ghost py-1 text-xs" onClick={() => setViewRun(null)}>Back to latest</button>
        </div>
      )}
      <ErrorBox error={runError} />
      <ErrorBox error={error} />
      {inc.status === 'failed' && (
        <div className="mb-5 flex items-start gap-3 rounded-lg border border-rose-900/60 bg-rose-950/40 p-4 text-sm text-rose-200"><AlertOctagon size={18} className="mt-0.5 shrink-0" /><div><b>Analysis failed.</b> {inc.error}</div></div>
      )}

      <div className="grid gap-5 xl:grid-cols-12">
        <div className="min-w-0 xl:col-span-4">
          <ActivityLog events={events} running={running} className="xl:sticky xl:top-6 h-[calc(100vh-220px)] min-h-[420px]" />
        </div>
        <div className="min-w-0 space-y-5 xl:col-span-8">
          {running && <Progress events={events} />}
          {!running && !r && inc.status !== 'failed' && (
            <div className="card card-pad text-sm text-slate-400">This incident has not been analyzed yet. Click <b>Run analysis</b> to start the agent.</div>
          )}
          {!running && r && (
            <>
              <div className="flex gap-1 overflow-x-auto rounded-lg border border-ink-700 bg-ink-900 p-1">
                {TABS.map((t) => (
                  <button key={t} onClick={() => setTab(t)} className={`whitespace-nowrap rounded-md px-4 py-1.5 text-sm font-medium transition ${tab === t ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-slate-100'}`}>{t}</button>
                ))}
              </div>
              {tab === 'Overview' && (r.hypotheses.length ? <Overview inc={inc} /> : <div className="card card-pad text-sm text-slate-300">{r.narrative.executive_summary} {r.narrative.root_cause_explanation}</div>)}
              {tab === 'Dependencies' && (
                <div className="card card-pad">
                  <h3 className="text-sm font-semibold text-white">Service dependency map</h3>
                  <p className="mb-4 text-xs text-slate-500">Fault localised by walking the graph: the root cause is the deepest failing service whose own dependencies are healthy.</p>
                  <DependencyGraph graph={r.graph} impact={r.impact} />
                  {r.impact.services.length > 0 && (
                    <table className="mt-5 w-full text-sm">
                      <thead><tr className="border-b border-ink-700 text-left">{['Service', 'Onset', 'Role', 'Anomalous metrics', 'New error lines'].map((h) => <th key={h} className="label py-2 pr-3">{h}</th>)}</tr></thead>
                      <tbody className="divide-y divide-ink-700/60">
                        {r.impact.services.map((x) => (
                          <tr key={x.service}><td className={`py-2 pr-3 font-medium ${x.role === 'root cause' ? 'text-rose-300' : 'text-slate-200'}`}>{x.service}</td><td className="py-2 pr-3 font-mono text-xs text-slate-400">{fmtTime(x.onset)}</td><td className="py-2 pr-3 text-slate-400">{x.role.replace('_', ' ')}</td><td className="py-2 pr-3 text-slate-400">{x.metrics.join(', ') || '—'}</td><td className="py-2 pr-3 tabular-nums text-slate-400">{x.error_lines}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
              {tab === 'Metrics & logs' && (
                <div className="space-y-5">
                  <div className="card card-pad"><h3 className="mb-3 text-sm font-semibold text-white">Service metrics</h3><MetricsPanel result={r} /></div>
                  <div className="card card-pad"><h3 className="mb-3 text-sm font-semibold text-white">Error log volume per minute</h3><ErrorTimeline timeline={r.charts.error_timeline} onset={r.summary.onset} /></div>
                  <div className="card card-pad">
                    <h3 className="mb-3 text-sm font-semibold text-white">New error signatures</h3>
                    {r.evidence.log_clusters.length === 0 ? <p className="text-sm text-slate-500">None.</p> : (
                      <ul className="divide-y divide-ink-700/60">
                        {r.evidence.log_clusters.map((c, i) => (
                          <li key={i} className="py-2.5">
                            <div className="flex flex-wrap items-center gap-2 text-xs"><span className="font-medium text-indigo-300">{c.service}</span><span className="chip bg-ink-700 text-slate-300">{c.category_label}</span><span className={`chip ${c.level === 'WARN' ? 'bg-amber-500/10 text-amber-300' : 'bg-rose-500/10 text-rose-300'}`}>{c.level} ×{c.count}</span><span className="text-slate-500">first {fmtTime(c.first_seen)}</span></div>
                            <code className="mt-1 block break-all font-mono text-[12px] text-slate-400">{c.sample}</code>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <div className="card card-pad">
                    <h3 className="mb-3 text-sm font-semibold text-white">Change records</h3>
                    {r.evidence.changes.length === 0 ? <p className="text-sm text-slate-500">No change records supplied.</p> : (
                      <ul className="divide-y divide-ink-700/60">
                        {r.evidence.changes.map((c) => (
                          <li key={c.id} className="flex flex-wrap items-start gap-3 py-2.5 text-sm">
                            <span className={`font-mono text-xs ${c.id === r.summary.change_id ? 'text-rose-300' : 'text-slate-400'}`}>{c.id}</span>
                            <div className="min-w-0 flex-1"><div className="text-slate-200">{c.description}</div><div className="text-xs text-slate-500">{c.service} · {c.type} · {fmtTime(c.timestamp, true)}{c.author ? ` · ${c.author}` : ''}</div></div>
                            <span className={`chip ${c.phase === 'before_onset' ? 'bg-purple-500/10 text-purple-300' : c.likely_mitigation ? 'bg-emerald-500/10 text-emerald-300' : 'bg-ink-700 text-slate-400'}`}>
                              {c.phase === 'before_onset' ? `${c.minutes_before_onset} min before onset` : c.likely_mitigation ? 'mitigation' : c.phase.replace(/_/g, ' ')}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              )}
              {tab === 'Timeline' && <div className="card card-pad"><h3 className="mb-4 text-sm font-semibold text-white">Incident timeline (UTC)</h3><Timeline items={r.timeline} /></div>}
              {tab === 'Report' && <ReportTab inc={inc} runId={viewRun?.id} />}
              {tab === 'Evidence' && (
                <div className="space-y-5">
                  <div className="card card-pad">
                    <h3 className="text-sm font-semibold text-white">Evidence ledger</h3>
                    <p className="mb-3 text-xs text-slate-500">Every fact the tools observed, with an ID. Every check, conclusion and recommendation cites these IDs; click any ID chip elsewhere to jump here.</p>
                    <EvidenceLedger ledger={r.evidence_ledger} highlight={highlight} />
                  </div>
                  <div className="card card-pad">
                    <h3 className="text-sm font-semibold text-white">Investigation log</h3>
                    <p className="mb-3 text-xs text-slate-500">Each action the agent took, which planner chose it, why, and what it found.</p>
                    <InvestigationLog log={r.investigation_log} />
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
    </EvidenceProvider>
  )
}
