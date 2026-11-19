import { useEffect, useRef, useState } from 'react'
import { Compass, ListTodo, Wrench, CheckCircle2, Eye, Lightbulb, FlaskConical, Trophy, Sparkles, AlertTriangle, Info, XCircle, Flag, ChevronRight } from 'lucide-react'
import { Spinner } from './ui'
import { CitedText, EvidenceChips } from './Evidence'

const TYPE = {
  plan: { icon: ListTodo, color: 'text-sky-300', bg: 'bg-sky-500/10', label: 'Plan' },
  decision: { icon: Compass, color: 'text-teal-300', bg: 'bg-teal-500/10', label: 'Decision' },
  tool_call: { icon: Wrench, color: 'text-violet-300', bg: 'bg-violet-500/10', label: 'Tool call' },
  tool_result: { icon: CheckCircle2, color: 'text-emerald-300', bg: 'bg-emerald-500/10', label: 'Tool result' },
  observation: { icon: Eye, color: 'text-cyan-300', bg: 'bg-cyan-500/10', label: 'Observation' },
  hypothesis: { icon: Lightbulb, color: 'text-amber-300', bg: 'bg-amber-500/10', label: 'Hypothesis' },
  test: { icon: FlaskConical, color: 'text-indigo-300', bg: 'bg-indigo-500/10', label: 'Test' },
  ranking: { icon: Trophy, color: 'text-yellow-300', bg: 'bg-yellow-500/10', label: 'Ranking' },
  llm: { icon: Sparkles, color: 'text-fuchsia-300', bg: 'bg-fuchsia-500/10', label: 'LLM' },
  warning: { icon: AlertTriangle, color: 'text-amber-300', bg: 'bg-amber-500/10', label: 'Warning' },
  info: { icon: Info, color: 'text-slate-300', bg: 'bg-slate-500/10', label: 'Info' },
  error: { icon: XCircle, color: 'text-rose-300', bg: 'bg-rose-500/10', label: 'Error' },
  conclusion: { icon: Flag, color: 'text-rose-300', bg: 'bg-rose-500/10', label: 'Conclusion' },
}

function Checks({ checks }) {
  return (
    <ul className="mt-2 space-y-1">
      {checks.map((c, i) => (
        <li key={i} className="flex gap-2 text-[12px] leading-snug">
          <span className={`mt-px w-9 shrink-0 text-right font-mono ${c.weight > 0 ? 'text-emerald-400' : c.weight < 0 ? 'text-rose-400' : 'text-slate-500'}`}>
            {c.weight > 0 ? '+' : ''}{c.weight}
          </span>
          <span className="text-slate-400"><span className="text-slate-200">{c.name}.</span> {c.detail} <EvidenceChips ids={c.evidence} /></span>
        </li>
      ))}
    </ul>
  )
}

function Event({ ev }) {
  const [open, setOpen] = useState(false)
  const t = TYPE[ev.type] || TYPE.info
  const Icon = t.icon
  const checks = ev.type === 'test' ? ev.data?.checks : null
  const steps = ev.type === 'plan' ? ev.data?.steps : null
  const expandable = !!(checks || steps)
  return (
    <li className="relative animate-fadein pl-9">
      <span className={`absolute left-0 top-0.5 flex h-6 w-6 items-center justify-center rounded-md ${t.bg}`}><Icon size={14} className={t.color} /></span>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className={`text-[10px] font-semibold uppercase tracking-wider ${t.color}`}>{t.label}</span>
        {ev.tool && <span className="rounded bg-ink-700 px-1.5 py-px font-mono text-[10px] text-slate-300">{ev.tool}</span>}
        <span className="ml-auto font-mono text-[10px] text-slate-600">{ev.ts?.slice(11, 19)}</span>
      </div>
      <button type="button" disabled={!expandable} onClick={() => setOpen(!open)}
        className={`mt-0.5 flex w-full items-start gap-1 text-left text-[13px] font-medium text-slate-100 ${expandable ? 'hover:text-white' : 'cursor-default'}`}>
        {expandable && <ChevronRight size={14} className={`mt-0.5 shrink-0 transition ${open ? 'rotate-90' : ''}`} />}
        <span>{ev.title}</span>
      </button>
      {ev.detail && !open && <p className="mt-0.5 line-clamp-4 text-[12px] leading-relaxed text-slate-400"><CitedText text={ev.detail} /></p>}
      {ev.data?.planner === 'llm' && <span className="chip mt-1 bg-fuchsia-500/15 text-fuchsia-300">LLM planner</span>}
      {open && checks && <Checks checks={checks} />}
      {open && steps && <ol className="mt-1.5 list-decimal space-y-0.5 pl-5 text-[12px] text-slate-400">{steps.map((s) => <li key={s}>{s}</li>)}</ol>}
    </li>
  )
}

export default function ActivityLog({ events, running, className = '' }) {
  const ref = useRef(null)
  const [follow, setFollow] = useState(true)
  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [events.length, follow])
  return (
    <div className={`card flex flex-col ${className}`}>
      <div className="flex items-center justify-between border-b border-ink-700 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${running ? 'animate-pulse bg-emerald-400' : 'bg-slate-600'}`} />
          <h3 className="text-sm font-semibold text-white">Agent activity</h3>
          <span className="text-xs text-slate-500">{events.length} steps</span>
        </div>
        <label className="flex items-center gap-1.5 text-xs text-slate-500">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-indigo-500" /> auto-scroll
        </label>
      </div>
      <div ref={ref} className="flex-1 overflow-y-auto px-4 py-4">
        {events.length === 0 && !running && <p className="text-sm text-slate-500">No agent activity yet. Run the analysis to start the investigation.</p>}
        <ol className="space-y-4 border-l border-ink-700/0">
          {events.map((ev) => <Event key={ev.seq} ev={ev} />)}
        </ol>
        {running && <div className="mt-4 flex items-center gap-2 pl-9 text-xs text-indigo-300"><Spinner size={13} /> Agent is reasoning…</div>}
      </div>
    </div>
  )
}
