import { useRef, useState } from 'react'
import { UploadCloud, FileCheck2, X, AlertTriangle, XCircle } from 'lucide-react'
import { Spinner } from './ui'
import { bytes } from '../lib/format'

export default function FileDrop({ label, hint, accept, file, onChange, icon: Icon = UploadCloud, required, check, checking }) {
  const bad = file && check && !check.ok
  const ref = useRef(null)
  const [over, setOver] = useState(false)
  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setOver(true) }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); if (e.dataTransfer.files[0]) onChange(e.dataTransfer.files[0]) }}
      onClick={() => ref.current?.click()}
      className={`group relative cursor-pointer rounded-xl border-2 border-dashed p-4 transition ${over ? 'border-indigo-400 bg-indigo-500/10' : bad ? 'border-rose-700/70 bg-rose-500/5' : file ? 'border-emerald-700/70 bg-emerald-500/5' : 'border-ink-600 hover:border-ink-600 hover:bg-ink-850'}`}>
      <input ref={ref} type="file" accept={accept} className="hidden" onChange={(e) => { if (e.target.files[0]) onChange(e.target.files[0]); e.target.value = '' }} />
      <div className="flex items-start gap-3">
        <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${bad ? 'bg-rose-500/15 text-rose-300' : file ? 'bg-emerald-500/15 text-emerald-300' : 'bg-ink-700 text-slate-400 group-hover:text-indigo-300'}`}>
          {bad ? <XCircle size={20} /> : file ? <FileCheck2 size={20} /> : <Icon size={20} />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-slate-100">{label} {required && <span className="text-xs font-normal text-slate-500">(metrics or logs required)</span>}</div>
          {file ? (
            <>
              <div className={`mt-0.5 truncate text-xs ${bad ? 'text-rose-300' : 'text-emerald-300'}`}>{file.name} · {bytes(file.size)}</div>
              {checking && !check && <div className="mt-1 flex items-center gap-1 text-[11px] text-slate-500"><Spinner size={11} /> Validating…</div>}
              {check?.error && <div className="mt-1 text-[11px] text-rose-300">{check.error}</div>}
              {check?.text && <div className="mt-1 text-[11px] text-slate-400">{check.text}</div>}
              {check?.warnings?.map((w) => <div key={w} className="mt-1 flex gap-1 text-[11px] text-amber-300"><AlertTriangle size={11} className="mt-0.5 shrink-0" />{w}</div>)}
            </>
          ) : (
            <div className="mt-0.5 text-xs text-slate-500">{hint}</div>
          )}
        </div>
        {file && (
          <button type="button" onClick={(e) => { e.stopPropagation(); onChange(null) }} className="rounded p-1 text-slate-500 hover:bg-ink-700 hover:text-white" aria-label="Remove file"><X size={15} /></button>
        )}
      </div>
    </div>
  )
}
