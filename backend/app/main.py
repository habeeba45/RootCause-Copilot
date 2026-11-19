"""RootCause Copilot API."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import traceback
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import llm
from .agent import IncidentInputs, RCAAgent
from .parsers import DependencyMap, ParseError, parse_changes, parse_dependencies, parse_logs, parse_metrics
from .report import to_html, to_markdown
from .validation import inspect_files
from .storage import SAMPLE_DIR, SAMPLE_FILES, Store, list_samples, now_iso

llm.load_env()
AGENT_PACE = float(os.getenv("AGENT_STEP_DELAY", "0.3"))  # display pacing only; excluded from reported analysis time
MAX_UPLOAD_MB = 50

app = FastAPI(title="RootCause Copilot API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
store = Store()
_running: set[str] = set()
_run_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers
def summary_view(inc: dict) -> dict:
    r = inc.get("result") or {}
    s = r.get("summary") or {}
    return {k: inc.get(k) for k in ("id", "title", "description", "severity", "source", "sample_id", "created_at",
                                     "status", "error", "analyzed_at", "files")} | {
        "root_cause": s.get("title"), "root_service": s.get("service"), "confidence": s.get("confidence"),
        "failure_mode": s.get("failure_mode"), "failure_mode_label": s.get("failure_mode_label"),
        "affected_count": s.get("affected_count"), "onset": s.get("onset"), "analysis_mode": s.get("analysis_mode"),
        "duration_s": s.get("duration_s"), "verdict": s.get("verdict"), "run_count": inc.get("run_count", 0),
        "data_warnings": len((inc.get("validation") or {}).get("warnings", [])),
    }


def _get(iid: str) -> dict:
    inc = store.get(iid)
    if not inc:
        raise HTTPException(404, f"Incident {iid} not found")
    return inc


def load_inputs(iid: str) -> IncidentInputs:
    raw = {k: store.read_input(iid, k) for k in ("metrics", "logs", "changes", "dependencies")}
    return IncidentInputs(
        metrics=parse_metrics(raw["metrics"]) if raw["metrics"] else None,
        logs=parse_logs(raw["logs"]) if raw["logs"] else None,
        changes=parse_changes(raw["changes"]) if raw["changes"] else [],
        dependencies=parse_dependencies(raw["dependencies"]) if raw["dependencies"] else DependencyMap(),
        changes_supplied=bool(raw["changes"]),
    )


def flatten_warnings(report: dict) -> list[str]:
    out = []
    for kind, f in report.get("files", {}).items():
        out += [f"{kind}: {w}" for w in f.get("warnings", [])]
    out += [c["message"] for c in report.get("cross_checks", []) if c["level"] == "warning"]
    return out


def run_analysis(iid: str, run_id: int) -> None:
    inc = store.get(iid)
    status, result, error = "failed", None, None
    try:
        inputs = load_inputs(iid)
        agent = RCAAgent(lambda ev: store.add_event(iid, ev), pace=AGENT_PACE)
        meta = {"reported_at": None, "data_warnings": (inc.get("validation") or {}).get("warnings", [])}
        if inc.get("sample_id"):
            sm = next((s for s in list_samples() if s["id"] == inc["sample_id"]), {})
            meta["reported_at"] = sm.get("reported_at")
        result = agent.run(inputs, meta)
        status = "completed"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        error = f"{type(e).__name__}: {e}"
        store.add_event(iid, {"type": "error", "title": "Analysis failed", "detail": error, "tool": None, "data": {}, "ts": now_iso()})
    # release the run slot *before* publishing the final status so an immediate re-run is accepted
    with _run_lock:
        _running.discard(iid)
    store.finish_run(iid, run_id, status, result, error)


def start_analysis(iid: str) -> None:
    with _run_lock:
        if iid in _running:
            raise HTTPException(409, "Analysis already running")
        _running.add(iid)
    try:
        run_id = store.start_run(iid, llm.status().get("mode"))
    except Exception:
        with _run_lock:
            _running.discard(iid)
        raise
    threading.Thread(target=run_analysis, args=(iid, run_id), daemon=True).start()


# --------------------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {"status": "ok", "llm": llm.status(), "incidents": store.count(), "storage": {"engine": "sqlite", "path": str(store.path.name)}}


@app.get("/api/llm/test")
def llm_test():
    """Verify the configured LLM key/model with one tiny request. The key itself is never returned."""
    return llm.test_connection()


@app.get("/api/samples")
def samples():
    return list_samples()


@app.get("/api/samples/{sample_id}/files/{kind}")
def sample_file(sample_id: str, kind: str):
    if kind not in SAMPLE_FILES or "/" in sample_id or ".." in sample_id:
        raise HTTPException(404, "Unknown file")
    p = SAMPLE_DIR / sample_id / SAMPLE_FILES[kind]
    if not p.exists():
        raise HTTPException(404, "Sample file not found")
    return FileResponse(p, filename=f"{sample_id}-{SAMPLE_FILES[kind]}")


@app.post("/api/incidents/from-sample/{sample_id}")
def create_from_sample(sample_id: str, analyze: bool = True):
    meta = next((s for s in list_samples() if s["id"] == sample_id), None)
    if not meta:
        raise HTTPException(404, f"Sample {sample_id} not found")
    d = SAMPLE_DIR / sample_id
    files = {k: (v, (d / v).read_bytes()) for k, v in SAMPLE_FILES.items() if (d / v).exists()}
    inc = store.create(meta["title"], meta["description"], meta.get("severity", "SEV-2"), "sample", files, sample_id)
    report = inspect_files(files)
    store.update(inc["id"], validation={"warnings": flatten_warnings(report), "report": report})
    if analyze:
        start_analysis(inc["id"])
    return summary_view(store.get(inc["id"]))


async def _read_uploads(**uploads: UploadFile | None) -> dict[str, tuple[str, bytes]]:
    files: dict[str, tuple[str, bytes]] = {}
    for kind, up in uploads.items():
        if up is not None and up.filename:
            data = await up.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
            files[kind] = (up.filename, data)
    return files


@app.post("/api/validate")
async def validate_upload(
    metrics: UploadFile | None = File(None), logs: UploadFile | None = File(None),
    changes: UploadFile | None = File(None), dependencies: UploadFile | None = File(None),
):
    """Parse and summarise uploaded files without creating an incident (used for live validation in the UI)."""
    files = await _read_uploads(metrics=metrics, logs=logs, changes=changes, dependencies=dependencies)
    return inspect_files(files)


@app.post("/api/incidents")
async def create_incident(
    title: str = Form(...),
    description: str = Form(""),
    severity: str = Form("SEV-2"),
    analyze: bool = Form(True),
    metrics: UploadFile | None = File(None),
    logs: UploadFile | None = File(None),
    changes: UploadFile | None = File(None),
    dependencies: UploadFile | None = File(None),
):
    if not title.strip():
        raise HTTPException(400, "Title is required")
    if severity not in ("SEV-1", "SEV-2", "SEV-3", "SEV-4"):
        raise HTTPException(400, "Severity must be SEV-1 to SEV-4")
    files = await _read_uploads(metrics=metrics, logs=logs, changes=changes, dependencies=dependencies)
    if "metrics" not in files and "logs" not in files:
        raise HTTPException(400, "Upload at least a metrics CSV or a log file")
    report = inspect_files(files)
    bad = [f"{k} file '{f['name']}': {f['error']}" for k, f in report["files"].items() if not f["ok"]]
    if bad:
        raise HTTPException(400, "; ".join(bad))
    if not report["can_analyze"]:
        raise HTTPException(400, "; ".join(c["message"] for c in report["cross_checks"] if c["level"] == "error"))
    inc = store.create(title.strip()[:200], description.strip()[:2000], severity, "upload", files)
    for k, f in report["files"].items():
        inc["files"][k]["summary"] = f["summary"]
    store.update(inc["id"], files=inc["files"], validation={"warnings": flatten_warnings(report), "report": report})
    if analyze:
        start_analysis(inc["id"])
    return summary_view(store.get(inc["id"]))


@app.get("/api/incidents")
def list_incidents():
    return [summary_view(i) for i in store.list()]


@app.get("/api/incidents/{iid}")
def get_incident(iid: str):
    return _get(iid)


@app.delete("/api/incidents/{iid}")
def delete_incident(iid: str):
    if iid in _running:
        raise HTTPException(409, "Cannot delete while analysis is running")
    if not store.delete(iid):
        raise HTTPException(404, "Incident not found")
    return {"deleted": iid}


@app.post("/api/incidents/{iid}/analyze")
def analyze(iid: str):
    _get(iid)
    start_analysis(iid)
    return {"status": "analyzing", "id": iid}


@app.get("/api/incidents/{iid}/events")
def events(iid: str, after: int = -1):
    inc = _get(iid)
    return {"status": inc["status"], "events": store.get_events(iid, after)}


@app.get("/api/incidents/{iid}/stream")
async def stream(iid: str):
    _get(iid)

    async def gen():
        last = -1
        idle = 0
        while True:
            evs = store.get_events(iid, last)
            for ev in evs:
                last = ev["seq"]
                yield f"event: activity\ndata: {json.dumps(ev)}\n\n"
            inc = store.get(iid)
            if inc is None:
                break
            if inc["status"] in ("completed", "failed", "uploaded") and not evs:
                yield f"event: done\ndata: {json.dumps({'status': inc['status']})}\n\n"
                break
            idle = 0 if evs else idle + 1
            if idle and idle % 60 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/incidents/{iid}/runs")
def runs(iid: str):
    """Investigation history: every analysis run of this incident, newest first."""
    _get(iid)
    return store.list_runs(iid)


@app.get("/api/incidents/{iid}/runs/{run_id}")
def run_detail(iid: str, run_id: int):
    _get(iid)
    run = store.get_run(iid, run_id)
    if not run:
        raise HTTPException(404, f"Run {run_id} not found for {iid}")
    return run


@app.get("/api/incidents/{iid}/report")
def report(iid: str, format: str = "html", download: bool = True, run: int | None = None):
    inc = _get(iid)
    if run is not None:
        r = store.get_run(iid, run)
        if not r:
            raise HTTPException(404, f"Run {run} not found for {iid}")
        if not r["result"]:
            raise HTTPException(409, f"Run {run} has no result ({r['status']})")
        inc = {**inc, "result": r["result"], "run_number": r["number"]}
    if not inc.get("result"):
        raise HTTPException(409, "Incident has not been analyzed yet")
    fname = f"RCA-{iid}" + (f"-run{inc['run_number']}" if inc.get("run_number") else "")
    disp = "attachment" if download else "inline"
    if format == "md":
        return Response(to_markdown(inc), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'{disp}; filename="{fname}.md"'})
    if format == "json":
        body = json.dumps({k: inc[k] for k in ("id", "title", "description", "severity", "created_at", "analyzed_at")}
                          | {"result": {k: v for k, v in inc["result"].items() if k != "charts"}}, indent=2)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'{disp}; filename="{fname}.json"'})
    if format == "html":
        return Response(to_html(inc), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'{disp}; filename="{fname}.html"'})
    raise HTTPException(400, "format must be html, md or json")


@app.get("/api/stats")
def stats():
    incs = store.list()
    done = [i for i in incs if i["status"] == "completed" and i.get("result")]
    modes = Counter(i["result"]["summary"].get("failure_mode_label") for i in done if i["result"]["summary"].get("verdict") == "confirmed")
    services = Counter(i["result"]["summary"].get("service") for i in done if i["result"]["summary"].get("verdict") == "confirmed")
    confirmed = [i for i in done if i["result"]["summary"].get("verdict") == "confirmed"]
    confs = [i["result"]["summary"]["confidence"] for i in confirmed]
    durs = [i["result"]["summary"].get("duration_s") or 0 for i in done]
    change_caused = sum(1 for i in confirmed if i["result"]["summary"].get("change_id"))
    return {
        "total": len(incs), "completed": len(done),
        "analyzing": sum(1 for i in incs if i["status"] == "analyzing"),
        "failed": sum(1 for i in incs if i["status"] == "failed"),
        "avg_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "avg_duration_s": round(sum(durs) / len(durs), 2) if durs else None,
        "change_induced_pct": round(change_caused / len(confirmed), 3) if confirmed else None,
        "inconclusive": sum(1 for i in done if i["result"]["summary"].get("verdict") in ("inconclusive", "no_anomaly")),
        "by_failure_mode": [{"label": k, "count": v} for k, v in modes.most_common()],
        "by_service": [{"service": k, "count": v} for k, v in services.most_common(8)],
        "llm": llm.status(),
    }


# --------------------------------------------------------------------------- serve built frontend (optional)
DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        f = DIST / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
