import { useEffect, useState } from 'react'
import { NavLink, Link } from 'react-router-dom'
import { LayoutDashboard, UploadCloud, ListChecks, FileText, Cpu, Sparkles, CircleAlert, PlugZap } from 'lucide-react'
import { Spinner } from './ui'
import { api } from '../lib/api'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/incidents/new', label: 'New incident', icon: UploadCloud },
  { to: '/incidents', label: 'Incidents', icon: ListChecks, end: true },
  { to: '/reports', label: 'RCA reports', icon: FileText },
]

export default function Layout({ children }) {
  const [health, setHealth] = useState(null)
  const [down, setDown] = useState(false)
  const [test, setTest] = useState(null)
  const [testing, setTesting] = useState(false)
  const runTest = async () => {
    setTesting(true); setTest(null)
    try { setTest(await api.llmTest()) } catch (e) { setTest({ ok: false, error: e.message }) } finally { setTesting(false) }
  }
  useEffect(() => {
    let alive = true
    const check = () => api.health().then((h) => { if (alive) { setHealth(h); setDown(false) } }).catch(() => alive && setDown(true))
    check()
    const t = setInterval(check, 15000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-20 flex w-60 flex-col border-r border-ink-700 bg-ink-900">
        <Link to="/" className="flex items-center gap-2.5 px-5 py-5">
          <img src="/favicon.svg" alt="" className="h-8 w-8" />
          <div>
            <div className="text-[15px] font-semibold leading-tight text-white">RootCause</div>
            <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-indigo-300">Copilot</div>
          </div>
        </Link>
        <nav className="mt-2 flex-1 space-y-1 px-3">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) => `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition ${isActive ? 'bg-indigo-600/15 text-indigo-200' : 'text-slate-400 hover:bg-ink-800 hover:text-slate-100'}`}>
              <Icon size={17} /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="m-3 rounded-lg border border-ink-700 bg-ink-850 p-3 text-xs">
          <div className="label mb-1.5">Reasoning engine</div>
          {down ? (
            <div className="flex items-center gap-1.5 text-rose-300"><CircleAlert size={14} /> API offline</div>
          ) : health?.llm?.enabled ? (
            <div className="flex items-center gap-1.5 text-emerald-300"><Sparkles size={14} /> LLM · {health.llm.model}</div>
          ) : (
            <div className="flex items-center gap-1.5 text-slate-300"><Cpu size={14} /> Rule-based engine</div>
          )}
          {!down && !health?.llm?.enabled && <p className="mt-1 text-slate-500">Add ANTHROPIC_API_KEY or OPENAI_API_KEY to backend/.env to enable the LLM planner.</p>}
          {!down && health?.llm?.enabled && (
            <button onClick={runTest} disabled={testing} className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-ink-600 px-2 py-1 text-[11px] text-slate-300 hover:bg-ink-800">
              {testing ? <Spinner size={11} /> : <PlugZap size={12} />} Test connection
            </button>
          )}
          {test && <p className={`mt-1.5 break-words ${test.ok ? 'text-emerald-300' : 'text-rose-300'}`}>{test.ok ? `Connected · ${test.latency_ms} ms` : test.error}</p>}
        </div>
      </aside>
      <main className="ml-60 flex-1 px-8 py-7">
        {down && (
          <div className="mb-5 rounded-lg border border-rose-900/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-200">
            The backend API is not reachable. Start it with <code className="font-mono">uvicorn app.main:app --port 8000</code> in the <code className="font-mono">backend</code> folder.
          </div>
        )}
        {children}
      </main>
    </div>
  )
}
