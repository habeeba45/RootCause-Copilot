import { useState } from 'react'
import { ChevronDown, GitCommit, Activity, FileWarning, Megaphone, Zap, Wrench, ShieldCheck, Check, X, Minus } from 'lucide-react'
import { VERDICT_STYLE, pct, fmtTime } from '../lib/format'
import { EvidenceChips, EvidenceChip, CitedText } from './Evidence'

export function CheckRow({ c }) {
  const Icon = c.result === 'supports' ? Check : c.result === 'contradicts' ? X : Minus
  const color = c.result === 'supports' ? 'text-emerald-400 bg-emerald-500/10' : c.result === 'contradicts' ? 'text-rose-400 bg-rose-500/10' : 'text-slate-400 bg-slate-500/10'
  return (
    <li className="flex gap-3 py-2">
      <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded ${color}`}><Icon size={13} /></span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-medium text-slate-200">{c.name}</span>
          <span className={`font-mono text-xs ${c.weight > 0 ? 'text-emerald-400' : c.weight < 0 ? 'text-rose-400' : 'text-slate-500'}`}>{c.weight > 0 ? '+' : ''}{c.weight}</span>
        </div>
        <p className="text-xs leading-relaxed text-slate-400">{c.detail} <EvidenceChips ids={c.evidence} /></p>
      </div>
    </li>
  )
}

export function HypothesisList({ hypotheses }) {
  const [open, setOpen] = useState(hypotheses?.[0]?.id)
  if (!hypotheses?.length) return <p className="text-sm text-slate-500">No hypotheses were generated.</p>
  const max = Math.max(...hypotheses.map((h) => h.confidence), 0.01)
  return (
    <ul className="space-y-2">
      {hypotheses.map((h) => (
        <li key={h.id} className={`rounded-lg border ${h.rank === 1 ? 'border-rose-900/70 bg-rose-950/10' : 'border-ink-700 bg-ink-950/30'}`}>
          <button onClick={() => setOpen(open === h.id ? null : h.id)} className="flex w-full items-center gap-3 px-4 py-3 text-left">
            <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold ${h.rank === 1 ? 'bg-rose-500 text-white' : 'bg-ink-700 text-slate-300'}`}>{h.rank}</span>
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                <span className="font-mono text-[11px] text-slate-500">{h.id}</span>
                <span className="min-w-0 truncate text-sm font-medium text-slate-100">{h.title}</span>
                {h.source === 'llm' && <span className="chip bg-fuchsia-500/15 text-fuchsia-300">proposed by LLM</span>}
              </div>
              <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-ink-700">
                <div className={`h-full rounded-full ${h.rank === 1 ? 'bg-rose-400' : 'bg-indigo-400/70'}`} style={{ width: `${Math.max(2, (h.confidence / max) * 100)}%` }} />
              </div>
            </div>
            <span className={`chip ${VERDICT_STYLE[h.verdict]}`}>{h.verdict}</span>
            <span className="w-12 text-right text-sm font-semibold tabular-nums text-white">{pct(h.confidence)}</span>
            <ChevronDown size={16} className={`text-slate-500 transition ${open === h.id ? 'rotate-180' : ''}`} />
          </button>
          {open === h.id && (
            <div className="border-t border-ink-700 px-4 pb-3 pt-2">
              <p className="mb-1 text-xs text-slate-400">{h.statement}</p>
              {h.rationale && <p className="mb-1 text-xs text-fuchsia-200/80">LLM rationale: <CitedText text={h.rationale} /></p>}
              <p className="mb-1 text-xs text-slate-500">Evidence score <span className="font-mono text-slate-300">{h.score}</span></p>
              <ul className="divide-y divide-ink-700/70">{h.checks.map((c, i) => <CheckRow key={i} c={c} />)}</ul>
            </div>
          )}
        </li>
      ))}
    </ul>
  )
}

const T_ICON = { change: GitCommit, anomaly: Activity, log: FileWarning, report: Megaphone }
const T_COLOR = { change: 'text-purple-300 bg-purple-500/10', anomaly: 'text-amber-300 bg-amber-500/10', log: 'text-sky-300 bg-sky-500/10', report: 'text-slate-300 bg-slate-500/10' }

export function Timeline({ items }) {
  if (!items?.length) return <p className="text-sm text-slate-500">No timeline events.</p>
  return (
    <ol className="relative ml-3 border-l border-ink-700">
      {items.map((t, i) => {
        const Icon = T_ICON[t.kind] || Activity
        return (
          <li key={i} className="mb-4 ml-6">
            <span className={`absolute -left-3 flex h-6 w-6 items-center justify-center rounded-full ring-4 ring-ink-900 ${t.highlight ? 'bg-rose-500 text-white' : T_COLOR[t.kind]}`}><Icon size={12} /></span>
            <div className="flex flex-wrap items-baseline gap-x-3">
              <time className="font-mono text-xs text-slate-500">{fmtTime(t.time, true)}</time>
              {t.service && <span className="text-xs font-medium text-indigo-300">{t.service}</span>}
              {t.highlight && <span className="chip bg-rose-500/15 text-rose-300">root cause</span>}
            </div>
            <p className={`mt-0.5 text-sm ${t.highlight ? 'font-medium text-white' : 'text-slate-300'}`}>{t.text} {t.evidence && <EvidenceChip id={t.evidence} />}</p>
          </li>
        )
      })}
    </ol>
  )
}

const REC = [
  { key: 'immediate', label: 'Immediate actions', icon: Zap, color: 'text-rose-300' },
  { key: 'short_term', label: 'Short-term fixes', icon: Wrench, color: 'text-amber-300' },
  { key: 'long_term', label: 'Long-term prevention', icon: ShieldCheck, color: 'text-emerald-300' },
]

export function Recommendations({ recs }) {
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {REC.map(({ key, label, icon: Icon, color }) => (
        <div key={key} className="rounded-lg border border-ink-700 bg-ink-950/30 p-4">
          <div className={`mb-3 flex items-center gap-2 text-sm font-semibold ${color}`}><Icon size={16} />{label}</div>
          <ol className="space-y-2.5">
            {(recs?.[key] || []).map((r, i) => (
              <li key={i} className="flex gap-2.5 text-sm text-slate-300">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-ink-700 text-[11px] font-semibold text-slate-300">{i + 1}</span>
                <span className="leading-snug"><CitedText text={r} /></span>
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  )
}
