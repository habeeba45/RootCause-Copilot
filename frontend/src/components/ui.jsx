import { Loader2 } from 'lucide-react'
import { STATUS_STYLE, SEV_STYLE } from '../lib/format'

export const Spinner = ({ size = 16, className = '' }) => <Loader2 size={size} className={`animate-spin ${className}`} />

export function StatusBadge({ status }) {
  return (
    <span className={`chip ${STATUS_STYLE[status] || STATUS_STYLE.uploaded}`}>
      {status === 'analyzing' && <Spinner size={11} />}{status}
    </span>
  )
}

export const SevBadge = ({ sev }) => <span className={`chip ${SEV_STYLE[sev] || SEV_STYLE['SEV-4']}`}>{sev || 'n/a'}</span>

export function StatCard({ label, value, sub, icon: Icon, accent = 'text-indigo-300' }) {
  return (
    <div className="card card-pad">
      <div className="flex items-start justify-between">
        <div className="label">{label}</div>
        {Icon && <Icon size={18} className={accent} />}
      </div>
      <div className="mt-2 text-3xl font-semibold tabular-nums text-white">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  )
}

export function ConfidenceRing({ value = 0, size = 96, label = 'confidence' }) {
  const r = size / 2 - 8
  const c = 2 * Math.PI * r
  const v = Math.max(0, Math.min(1, value || 0))
  const color = v >= 0.7 ? '#fb7185' : v >= 0.4 ? '#fbbf24' : '#94a3b8'
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} stroke="#1c2740" strokeWidth="8" fill="none" />
        <circle cx={size / 2} cy={size / 2} r={r} stroke={color} strokeWidth="8" fill="none" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - v)} style={{ transition: 'stroke-dashoffset 1s ease' }} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl font-semibold text-white">{Math.round(v * 100)}%</span>
        <span className="text-[10px] uppercase tracking-wider text-slate-500">{label}</span>
      </div>
    </div>
  )
}

export function Empty({ icon: Icon, title, children }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-ink-600 px-6 py-12 text-center">
      {Icon && <Icon size={28} className="mb-3 text-slate-500" />}
      <div className="font-medium text-slate-200">{title}</div>
      {children && <div className="mt-1 max-w-md text-sm text-slate-500">{children}</div>}
    </div>
  )
}

export function PageHeader({ title, subtitle, children }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-white">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
  )
}

export function ErrorBox({ error }) {
  if (!error) return null
  return <div className="rounded-lg border border-rose-900/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-200">{String(error)}</div>
}
