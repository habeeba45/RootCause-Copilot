// Thin client for the FastAPI backend. In dev, Vite proxies /api to http://127.0.0.1:8000.
const BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, opts = {}) {
  let res
  try {
    res = await fetch(`${BASE}${path}`, opts)
  } catch (e) {
    throw new Error('Cannot reach the RootCause Copilot API. Is the backend running on port 8000?')
  }
  const isJson = (res.headers.get('content-type') || '').includes('application/json')
  const body = isJson ? await res.json() : await res.text()
  if (!res.ok) {
    const msg = isJson ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : body
    throw new Error(msg || `Request failed (${res.status})`)
  }
  return body
}

export const api = {
  health: () => request('/api/health'),
  stats: () => request('/api/stats'),
  samples: () => request('/api/samples'),
  incidents: () => request('/api/incidents'),
  incident: (id) => request(`/api/incidents/${id}`),
  events: (id, after = -1) => request(`/api/incidents/${id}/events?after=${after}`),
  analyze: (id) => request(`/api/incidents/${id}/analyze`, { method: 'POST' }),
  remove: (id) => request(`/api/incidents/${id}`, { method: 'DELETE' }),
  fromSample: (sampleId) => request(`/api/incidents/from-sample/${sampleId}`, { method: 'POST' }),
  upload: (formData) => request('/api/incidents', { method: 'POST', body: formData }),
  validate: (formData) => request('/api/validate', { method: 'POST', body: formData }),
  llmTest: () => request('/api/llm/test'),
  streamUrl: (id) => `${BASE}/api/incidents/${id}/stream`,
  runs: (id) => request(`/api/incidents/${id}/runs`),
  run: (id, runId) => request(`/api/incidents/${id}/runs/${runId}`),
  reportUrl: (id, format, download = true, run = null) => `${BASE}/api/incidents/${id}/report?format=${format}&download=${download}${run ? `&run=${run}` : ''}`,
  sampleFileUrl: (sampleId, kind) => `${BASE}/api/samples/${sampleId}/files/${kind}`,
}
