# RootCause Copilot

AI-powered root cause analysis for IT incidents. Upload metrics, logs, deployment changes and a service
dependency map. An investigation agent calls four analysis tools, forms hypotheses, tests each one against
the evidence, ranks the probable causes and writes an RCA report with recommended fixes.

**Stack:** React 18 + Vite + Tailwind CSS (frontend) · Python + FastAPI (backend) · optional Anthropic / OpenAI LLM.

## Quick start (Windows)

Prerequisites: **Python 3.10+** and **Node.js 18+** on your PATH.

1. Double-click **`setup.bat`** (one time). It creates `backend\.venv`, installs the Python packages and runs `npm install`.
2. Double-click **`start.bat`**. It opens the API (http://localhost:8000) and the UI (http://localhost:5173) in two windows and launches your browser.
3. On the dashboard, click **Run investigation** on any sample incident.

### Manual start (any OS)

```bash
# terminal 1 – backend
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000 --reload

# terminal 2 – frontend
cd frontend
npm install
npm run dev                        # http://localhost:5173 (proxies /api to :8000)
```

**Single-port mode:** run `npm run build` in `frontend`. FastAPI then serves the built UI at http://localhost:8000.

## How the agent works

The agent runs in four phases. Every step appears live in the **Agent activity** panel and is saved in the
**Investigation log** (Evidence tab and report appendix).

1. **Planned evidence gathering.** A planner picks the next tool based on what data was uploaded and what has been
   learned so far: metrics first to find the onset, then logs (to classify failures), then changes (once the onset is known),
   then the dependency graph. Missing inputs are skipped, and the planner says so.
2. **Evidence ledger.** Every observation becomes a numbered evidence item (E1, E2, …): anomalies, log signatures,
   cross-service blame, changes, dependency findings, onset. **Nothing downstream may claim something without citing these IDs.**
3. **Hypotheses and evidence checks.** Hypotheses are generated from the ledger. Each one is scored by deterministic
   checks, and each check cites ledger IDs. **Confidence is computed only from those scores**, with an "unexplained" alternative so
   thin evidence never looks certain. A leader scoring below 6 is reported as **Inconclusive** rather than as a root cause.
4. **Follow-up investigation.** Targeted tools verify the leader and separate close rivals:
   | Tool | What it does |
   |---|---|
   | `compare_change_window` | Compares the changed service's metrics and error volume 20 min before and after a change, to show whether it had measurable impact |
   | `inspect_service` | Checks whether a service's affected callers failed *after* it (origin) or *before* it (victim) |
   | `search_logs` | Substring search to verify a specific claim |
   | `add_hypothesis` (LLM only) | Proposes a missed hypothesis; it is scored by the same checks |
   With an API key, the **LLM chooses these tools** in a real tool-calling loop. Without a key, the rule planner does.

## Real AI (LLM) integration

Copy `backend/.env.example` to `backend/.env`, set a key, and restart the backend. Keys come only from environment variables.
They are never logged, never stored with incidents, and never returned by the API.

```
ANTHROPIC_API_KEY=...                         # or
OPENAI_API_KEY=...  (+ optional OPENAI_BASE_URL for Azure / OpenRouter / Ollama)
```

The sidebar shows the active engine and has a **Test connection** button (`GET /api/llm/test`).

With a key, the LLM:
- receives the case file (ledger, hypotheses, scores), chooses follow-up tools, and may propose hypotheses;
- finishes with `finish_investigation`: an explanation, per-hypothesis assessments and recommendations, **each citing ledger IDs**.

**Grounding rules**, enforced in code:
- citations to evidence that doesn't exist are removed;
- assessments or recommendations with no valid evidence are dropped;
- an explanation with no valid citation is rejected;
- the LLM cannot change scores or confidence. If it prefers a different hypothesis, that disagreement is shown, and the evidence ranking stands.

If the LLM call fails, the agent logs a warning and continues with the rule planner.

## Uploading your own data

On **New incident**, files are validated on the server as you add them (`POST /api/validate`). For each file you see:
record counts, services, metrics, time range, sample interval, and warnings (unreadable rows, short series, missing
service names, no error lines). Cross-file checks flag metrics and logs that don't overlap in time, changes outside the window, and
services missing from the dependency map. Files with errors block submission; warnings are stored with the incident and
shown on its page.

## Persistence and investigation history

All data is stored in **SQLite** at `backend/data/rootcause.db`. That covers incidents, the uploaded files, every analysis run with its full result,
and the agent's activity log, written step by step. Re-running an incident adds a new run instead of overwriting the old one.
Pick any past run from **Investigation history** on the incident page to view it or download its report.
API: `GET /api/incidents/{id}/runs`, `GET /api/incidents/{id}/runs/{run_id}`, `GET /api/incidents/{id}/report?format=html&run={run_id}`.
If the server stops during an analysis, that run is marked *failed (interrupted)* at the next start. Data saved by older JSON-file versions is
imported automatically. To start fresh, stop the backend and delete `backend/data/`.

`AGENT_STEP_DELAY` (default 0.3 s) only slows the live activity log so people can follow it. Reported analysis times exclude it.

See **AUDIT.md** for the latest end-to-end technical audit, including the parts that are heuristic or simulated.

## Sample incidents (`backend/sample_data`)

| Sample | Real root cause | Red herrings |
|---|---|---|
| `checkout-failures` | payment-service v2.14.0 cut the DB pool 50 → 10 | inventory feature flag 27 min earlier |
| `product-page-latency` | memory leak in recommendation-service v3.2.0 cache (OOM kills) | Elasticsearch node scaling |
| `login-outage` | expired TLS certificate on auth-service (**no change involved**) | Kafka config change, consumer-lag warnings |

Regenerate them with `python scripts/generate_samples.py`. You can download each file from the **New incident** page to see the formats.

## Input formats

- **Metrics CSV:** long `timestamp,service,metric,value` or wide `timestamp,service,cpu_pct,latency_ms,…`
- **Logs:** `2026-11-18T14:07:02Z ERROR [payment-service] message`. Also accepts `service:`, `service=` and JSON lines (`timestamp, level, service, message`). Stack-trace continuation lines are attached to the entry above.
- **Changes:** JSON list or CSV with `timestamp, service, type, description, author, version`
- **Dependencies:** `{"services":[…], "edges":[{"from":"caller","to":"callee"}]}`, an adjacency map `{"a":["b"]}`, or CSV `from,to`

Metrics **or** logs are required. Changes and dependencies make the analysis sharper.

## API

`GET /api/health` · `GET /api/stats` · `GET /api/samples` · `POST /api/incidents/from-sample/{id}` ·
`POST /api/incidents` (multipart: title, description, severity, metrics, logs, changes, dependencies) ·
`GET /api/incidents` · `GET|DELETE /api/incidents/{id}` · `POST /api/incidents/{id}/analyze` ·
`GET /api/incidents/{id}/events?after=N` · `GET /api/incidents/{id}/stream` (SSE) ·
`GET /api/incidents/{id}/report?format=html|md|json` · `POST /api/validate` · `GET /api/llm/test`. Interactive docs are at http://localhost:8000/docs.

## Tests

```bash
cd backend
python -m pytest -q
```

26 tests cover: agent accuracy on all samples; that every cited evidence ID exists; that confidences are derived from scores;
decoy refutation; planner adaptation to missing inputs; inconclusive results; the validation endpoint; upload, re-run and delete; SSE;
reports; and the LLM integration, using a mocked HTTP server for Anthropic and OpenAI tool calling. The LLM tests cover
grounding filters, forced finish, failure fallback, and that the key never leaks.

## Project layout

```
backend/
  app/main.py            FastAPI routes, background analysis, SSE
  app/agent.py           investigation agent (planner → tools → evidence ledger → hypotheses → checks → follow-ups → report)
  app/evidence.py        evidence ledger + citation grounding
  app/validation.py      upload validation / data-quality report
  app/tools/             metrics_analyzer, log_analyzer, change_detector, dependency_analyzer
  app/parsers.py         tolerant input parsers
  app/playbooks.py       failure-mode knowledge base & recommendations
  app/llm.py             Anthropic / OpenAI-compatible tool-calling client
  app/report.py          Markdown + HTML RCA reports
  app/storage.py         SQLite store: incidents, input files, runs (history), events
  sample_data/           3 realistic incidents
  tests/                 pytest suite
frontend/src/
  pages/                 Dashboard, NewIncident, Incidents, IncidentDetail, Reports
  components/            ActivityLog, DependencyGraph, MetricsPanel, Analysis, FileDrop, ui
```
