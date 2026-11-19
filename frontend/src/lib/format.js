export const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)

export function fmtTime(iso, withDate = false) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const t = d.toISOString().slice(11, 16)
  return withDate ? `${d.toISOString().slice(0, 10)} ${t} UTC` : `${t}`
}

export function timeAgo(iso) {
  if (!iso) return ''
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export const bytes = (n) => (n == null ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`)

export const STATUS_STYLE = {
  uploaded: 'bg-slate-700/50 text-slate-300',
  analyzing: 'bg-indigo-500/15 text-indigo-300',
  completed: 'bg-emerald-500/15 text-emerald-300',
  failed: 'bg-rose-500/15 text-rose-300',
}
export const SEV_STYLE = {
  'SEV-1': 'bg-rose-500/15 text-rose-300 ring-1 ring-rose-500/30',
  'SEV-2': 'bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30',
  'SEV-3': 'bg-sky-500/15 text-sky-300 ring-1 ring-sky-500/30',
  'SEV-4': 'bg-slate-500/15 text-slate-300 ring-1 ring-slate-500/30',
}
export const VERDICT_STYLE = {
  supported: 'bg-emerald-500/15 text-emerald-300',
  contributing: 'bg-indigo-500/15 text-indigo-300',
  plausible: 'bg-amber-500/15 text-amber-300',
  refuted: 'bg-rose-500/15 text-rose-300',
}
export const METRIC_LABEL = {
  cpu_pct: 'CPU %', memory_pct: 'Memory %', latency_p95_ms: 'p95 latency (ms)', error_rate_pct: 'Error rate %',
  request_rate: 'Requests / min', db_pool_utilization_pct: 'DB pool utilisation %',
}
