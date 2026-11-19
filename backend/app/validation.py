"""Upload validation: parse every file, summarise what it contains and flag data-quality problems.

Returns a report the UI shows before analysis starts. Nothing here is guessed: every number is
counted from the uploaded bytes.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime

from .parsers import (ParseError, iso, parse_changes, parse_dependencies, parse_logs, parse_metrics)

MAX_FILE_MB = 50
MIN_POINTS_PER_SERIES = 20


def _range(ts: list[datetime]) -> dict | None:
    if not ts:
        return None
    lo, hi = min(ts), max(ts)
    return {"start": iso(lo), "end": iso(hi), "minutes": round((hi - lo).total_seconds() / 60, 1)}


def _metrics(data: bytes) -> tuple[dict, list[str], object]:
    st: dict = {}
    pts = parse_metrics(data, st)
    warnings = []
    series: dict = defaultdict(list)
    for p in pts:
        series[(p.service, p.metric)].append(p.ts)
    per_series = [len(v) for v in series.values()]
    intervals = []
    for ts in list(series.values())[:20]:
        ts = sorted(ts)
        intervals += [(b - a).total_seconds() for a, b in zip(ts, ts[1:]) if b > a]
    step = statistics.median(intervals) if intervals else None
    services = sorted({p.service for p in pts})
    metrics = sorted({p.metric for p in pts})
    if st["skipped_rows"]:
        warnings.append(f"{st['skipped_rows']} rows had an unreadable timestamp and were skipped.")
    if st["skipped_values"]:
        warnings.append(f"{st['skipped_values']} non-numeric values were skipped.")
    if not st.get("has_service_column"):
        warnings.append("No 'service' column: all metrics are attributed to service 'unknown', so fault localisation is limited.")
    short = sum(1 for n in per_series if n < MIN_POINTS_PER_SERIES)
    if short:
        warnings.append(f"{short} of {len(series)} series have fewer than {MIN_POINTS_PER_SERIES} points; "
                        "baselines for those series are unreliable.")
    summary = {"points": len(pts), "series": len(series), "services": services, "metrics": metrics,
               "format": st["format"], "time_range": _range([p.ts for p in pts]),
               "sample_interval_s": round(step, 1) if step else None,
               "min_points_per_series": min(per_series) if per_series else 0}
    return summary, warnings, pts


def _logs(data: bytes) -> tuple[dict, list[str], object]:
    st: dict = {}
    entries = parse_logs(data, st)
    warnings = []
    levels = Counter(e.level for e in entries)
    timed = [e.ts for e in entries if e.ts]
    services = sorted({e.service for e in entries if e.service != "unknown"})
    unknown_svc = sum(1 for e in entries if e.service == "unknown")
    if len(timed) < len(entries) * 0.8:
        warnings.append(f"{len(entries) - len(timed)} of {len(entries)} log lines have no readable timestamp; "
                        "they are counted but cannot be placed on the timeline.")
    if unknown_svc > len(entries) * 0.2:
        warnings.append(f"{unknown_svc} lines have no service name; expected '[service]', 'service:' or a JSON 'service' field.")
    if not (levels.get("ERROR", 0) + levels.get("FATAL", 0) + levels.get("WARN", 0)):
        warnings.append("No ERROR or WARN lines found, so log evidence will be weak.")
    summary = {"lines": len(entries), "levels": dict(levels), "services": services,
               "time_range": _range(timed), "json_lines": st["json_lines"],
               "unstructured_lines": st["unstructured_lines"], "continuation_lines": st["continuation_lines"]}
    return summary, warnings, entries


def _changes(data: bytes) -> tuple[dict, list[str], object]:
    st: dict = {}
    changes = parse_changes(data, st)
    warnings = []
    if st["skipped"]:
        warnings.append(f"{st['skipped']} change records had no readable timestamp and were skipped.")
    if not changes:
        warnings.append("No usable change records found.")
    summary = {"records": len(changes), "types": dict(Counter(c.type for c in changes)),
               "services": sorted({c.service for c in changes}), "time_range": _range([c.ts for c in changes])}
    return summary, warnings, changes


def _deps(data: bytes) -> tuple[dict, list[str], object]:
    st: dict = {}
    dm = parse_dependencies(data, st)
    warnings = []
    if not dm.edges:
        warnings.append("The dependency map has no edges, so fault localisation cannot use it.")
    if st["invalid_edges"]:
        warnings.append(f"{st['invalid_edges']} edges were missing 'from'/'to' and were skipped.")
    if st["duplicate_edges"]:
        warnings.append(f"{st['duplicate_edges']} duplicate edges were ignored.")
    if st["self_edges"]:
        warnings.append(f"{st['self_edges']} self-referencing edges were ignored.")
    if _has_cycle(dm.edges):
        warnings.append("The dependency graph contains a cycle; propagation direction may be ambiguous.")
    summary = {"services": sorted(dm.services), "edges": len(dm.edges)}
    return summary, warnings, dm


def _has_cycle(edges: list[tuple[str, str]]) -> bool:
    graph = defaultdict(list)
    for a, b in edges:
        graph[a].append(b)
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for m in graph.get(n, ()):
            if state.get(m) == 1 or (m not in state and visit(m)):
                return True
        state[n] = 2
        return False
    return any(n not in state and visit(n) for n in list(graph))


HANDLERS = {"metrics": _metrics, "logs": _logs, "changes": _changes, "dependencies": _deps}


def inspect_files(files: dict[str, tuple[str, bytes]]) -> dict:
    """Validate uploaded files. ``files`` maps kind -> (filename, bytes)."""
    report: dict = {"files": {}, "cross_checks": [], "ok": True}
    parsed: dict = {}
    for kind, (name, data) in files.items():
        entry = {"name": name, "bytes": len(data), "ok": True, "error": None, "warnings": [], "summary": None}
        if kind not in HANDLERS:
            entry.update(ok=False, error=f"Unknown file kind '{kind}'")
        elif len(data) > MAX_FILE_MB * 1024 * 1024:
            entry.update(ok=False, error=f"File is larger than {MAX_FILE_MB} MB")
        elif not data.strip():
            entry.update(ok=False, error="File is empty")
        else:
            try:
                summary, warnings, obj = HANDLERS[kind](data)
                entry.update(summary=summary, warnings=warnings)
                parsed[kind] = obj
            except ParseError as e:
                entry.update(ok=False, error=str(e))
            except Exception as e:  # noqa: BLE001 - report, never crash on user input
                entry.update(ok=False, error=f"Could not read file: {type(e).__name__}: {e}")
        report["files"][kind] = entry
        report["ok"] &= entry["ok"]

    has_signal = any(report["files"].get(k, {}).get("ok") for k in ("metrics", "logs"))
    if not has_signal:
        report["ok"] = False
        report["cross_checks"].append({"level": "error", "message": "Upload a valid metrics CSV or log file; the agent needs at least one of them."})

    cross = report["cross_checks"]
    m, lg = report["files"].get("metrics", {}), report["files"].get("logs", {})
    mr = (m.get("summary") or {}).get("time_range")
    lr = (lg.get("summary") or {}).get("time_range")
    if mr and lr and (lr["end"] < mr["start"] or lr["start"] > mr["end"]):
        cross.append({"level": "warning", "message": f"Metrics ({mr['start']} to {mr['end']}) and logs ({lr['start']} to {lr['end']}) "
                                                     "do not overlap in time, so they cannot be correlated."})
    window = [r for r in (mr, lr) if r]
    ch = report["files"].get("changes", {})
    if window and ch.get("ok") and "changes" in parsed:
        lo = min(r["start"] for r in window)
        hi = max(r["end"] for r in window)
        inside = [c for c in parsed["changes"] if lo <= iso(c.ts) <= hi]
        before = [c for c in parsed["changes"] if iso(c.ts) < lo]
        if not inside and parsed["changes"]:
            cross.append({"level": "warning", "message": "No change record falls inside the telemetry window "
                                                         f"({lo} to {hi}); {len(before)} are earlier."})
    data_services = set((m.get("summary") or {}).get("services", [])) | set((lg.get("summary") or {}).get("services", []))
    data_services.discard("unknown")
    dp = report["files"].get("dependencies", {})
    if dp.get("ok") and data_services:
        mapped = set(dp["summary"]["services"])
        missing = sorted(data_services - mapped)
        if missing:
            cross.append({"level": "warning", "message": f"{len(missing)} services in the telemetry are missing from the dependency map: "
                                                         f"{', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}."})
        if not (data_services & mapped):
            cross.append({"level": "warning", "message": "None of the dependency-map service names match the telemetry service names."})
    for kind in ("changes", "dependencies"):
        if kind not in files:
            cross.append({"level": "info", "message": f"No {kind} file: "
                          + ("change-based hypotheses cannot be tested." if kind == "changes"
                             else "fault localisation will rely on onset order and log references.")})
    report["can_analyze"] = report["ok"]
    return report
