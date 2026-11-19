"""Parsers for uploaded incident artefacts.

Supported inputs
- Metrics: CSV in *long* format (timestamp,service,metric,value) or *wide* format
  (timestamp,service,<metric1>,<metric2>,...). Wide CSV without a service column is
  attributed to service "unknown".
- Logs: plain text lines ("<ISO timestamp> <LEVEL> [service] message", also tolerates
  "service:" or "service=" forms) or JSON lines ({"timestamp","level","service","message"}).
- Changes: JSON list (or {"changes": [...]}) or CSV with timestamp,service,type,description,...
- Dependencies: JSON {"services": [...], "edges": [{"from","to"}]} or {"service": ["dep", ...]}
  adjacency map, or CSV with from,to columns.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ParseError(ValueError):
    pass


# --------------------------------------------------------------------------- time
def parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip()
    if re.fullmatch(r"\d{10}(\.\d+)?|\d{13}", s):
        return parse_ts(float(s))
    s = s.replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(str(value).strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError("Unable to decode file")


# --------------------------------------------------------------------------- metrics
@dataclass
class MetricPoint:
    ts: datetime
    service: str
    metric: str
    value: float


def parse_metrics(data: bytes | str, stats: dict | None = None) -> list[MetricPoint]:
    """Parse metrics; if ``stats`` is given it is filled with row/skip counts for data-quality reports."""
    st = stats if stats is not None else {}
    st.update(rows=0, skipped_rows=0, skipped_values=0, format=None)
    text = _decode(data).strip()
    if not text:
        raise ParseError("Metrics file is empty")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ParseError("Metrics CSV has no header row")
    fields = [f.strip().lower() for f in reader.fieldnames]
    reader.fieldnames = fields
    ts_col = next((c for c in fields if c in ("timestamp", "time", "ts", "datetime", "date")), None)
    if ts_col is None:
        raise ParseError("Metrics CSV needs a 'timestamp' column")
    svc_col = next((c for c in fields if c in ("service", "service_name", "app", "component", "host")), None)
    long_format = "metric" in fields and "value" in fields
    st["format"] = "long" if long_format else "wide"
    st["has_service_column"] = svc_col is not None
    points: list[MetricPoint] = []
    for row in reader:
        st["rows"] += 1
        ts = parse_ts(row.get(ts_col))
        if ts is None:
            st["skipped_rows"] += 1
            continue
        svc = (row.get(svc_col) or "unknown").strip() if svc_col else "unknown"
        if long_format:
            try:
                points.append(MetricPoint(ts, svc, (row["metric"] or "").strip(), float(row["value"])))
            except (TypeError, ValueError, AttributeError):
                st["skipped_values"] += 1
                continue
        else:
            for col in fields:
                if col in (ts_col, svc_col):
                    continue
                try:
                    points.append(MetricPoint(ts, svc, col, float(row[col])))
                except (TypeError, ValueError):
                    st["skipped_values"] += 1
                    continue
    if not points:
        raise ParseError("No numeric metric values found in CSV")
    points.sort(key=lambda p: p.ts)
    return points


# --------------------------------------------------------------------------- logs
@dataclass
class LogEntry:
    ts: datetime | None
    level: str
    service: str
    message: str


LEVELS = {"TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERROR", "ERR", "CRITICAL", "FATAL", "SEVERE"}
_LEVEL_NORMAL = {"WARNING": "WARN", "ERR": "ERROR", "CRITICAL": "FATAL", "SEVERE": "ERROR", "NOTICE": "INFO"}

_LINE_RE = re.compile(
    r"^\s*\[?(?P<ts>\d{4}-\d{2}-\d{2}[T ][\d:.,]+(?:Z|[+-]\d{2}:?\d{2})?)\]?\s+"
    r"(?:(?P<level1>[A-Za-z]+)\s+)?"
    r"(?:\[(?P<svc1>[\w.\-/]+)\]|(?P<svc2>[\w.\-/]+):|service=(?P<svc3>[\w.\-/]+))?\s*"
    r"(?:(?P<level2>[A-Za-z]+)\s*[:\-]?\s+)?"
    r"(?P<msg>.*)$"
)


def _norm_level(lvl: str | None, msg: str) -> str:
    if lvl and lvl.upper() in LEVELS:
        u = lvl.upper()
        return _LEVEL_NORMAL.get(u, u)
    m = msg.lower()
    if any(k in m for k in ("exception", "error", "failed", "fatal", "panic")):
        return "ERROR"
    if any(k in m for k in ("warn", "slow", "retry")):
        return "WARN"
    return "INFO"


def parse_logs(data: bytes | str, stats: dict | None = None) -> list[LogEntry]:
    st = stats if stats is not None else {}
    st.update(lines=0, json_lines=0, continuation_lines=0, unstructured_lines=0)
    text = _decode(data)
    entries: list[LogEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        st["lines"] += 1
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                msg = str(obj.get("message") or obj.get("msg") or obj.get("log") or "")
                entries.append(LogEntry(
                    parse_ts(obj.get("timestamp") or obj.get("time") or obj.get("ts") or obj.get("@timestamp")),
                    _norm_level(obj.get("level") or obj.get("severity"), msg),
                    str(obj.get("service") or obj.get("app") or obj.get("logger") or "unknown"),
                    msg,
                ))
                st["json_lines"] += 1
                continue
            except json.JSONDecodeError:
                pass
        m = _LINE_RE.match(line)
        if m:
            lvl1, lvl2 = m.group("level1"), m.group("level2")
            level_raw = lvl1 if lvl1 and lvl1.upper() in LEVELS else (lvl2 if lvl2 and lvl2.upper() in LEVELS else None)
            msg = m.group("msg") or ""
            # if a token was greedily taken as level but isn't one, put it back into the message
            if lvl1 and lvl1.upper() not in LEVELS:
                msg = f"{lvl1} {msg}"
            if lvl2 and lvl2.upper() not in LEVELS:
                msg = f"{lvl2} {msg}"
            svc = m.group("svc1") or m.group("svc2") or m.group("svc3") or "unknown"
            entries.append(LogEntry(parse_ts(m.group("ts").replace(",", ".")), _norm_level(level_raw, msg), svc, msg.strip()))
        else:
            # continuation lines (stack traces) attach to previous entry
            if entries and (raw.startswith((" ", "\t")) or line.startswith(("at ", "Caused by"))):
                entries[-1].message += " | " + line
                st["continuation_lines"] += 1
            else:
                entries.append(LogEntry(None, _norm_level(None, line), "unknown", line))
                st["unstructured_lines"] += 1
    if not entries:
        raise ParseError("No log lines found")
    return entries


# --------------------------------------------------------------------------- changes
@dataclass
class Change:
    id: str
    ts: datetime
    service: str
    type: str
    description: str
    author: str | None = None
    version: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "timestamp": iso(self.ts), "service": self.service, "type": self.type,
                "description": self.description, "author": self.author, "version": self.version}


def _norm_change_type(t: str | None, desc: str) -> str:
    s = f"{t or ''} {desc}".lower()
    if any(k in s for k in ("flag", "toggle")):
        return "feature_flag"
    if any(k in s for k in ("deploy", "release", "rollout")):
        return "deployment"
    if any(k in s for k in ("config", "setting", "parameter", "timeout", "env")):
        return "config"
    if any(k in s for k in ("infra", "node", "scale", "network", "dns", "cert", "kubernetes", "cluster", "terraform")):
        return "infrastructure"
    if "schema" in s or "migration" in s:
        return "database"
    return (t or "other").lower()


def parse_changes(data: bytes | str, stats: dict | None = None) -> list[Change]:
    st = stats if stats is not None else {}
    st.update(records=0, skipped=0)
    text = _decode(data).strip()
    if not text:
        return []
    records: list[dict]
    if text[0] in "[{":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise ParseError(f"Invalid changes JSON: {e}") from e
        records = obj.get("changes", obj.get("deployments", [])) if isinstance(obj, dict) else obj
        if not isinstance(records, list):
            raise ParseError("Changes JSON must be a list, or an object with a 'changes' list")
    else:
        reader = csv.DictReader(io.StringIO(text))
        reader.fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
        records = list(reader)
    out: list[Change] = []
    for i, r in enumerate(records):
        st["records"] += 1
        if not isinstance(r, dict):
            st["skipped"] += 1
            continue
        r = {str(k).lower(): v for k, v in r.items()}
        ts = parse_ts(r.get("timestamp") or r.get("time") or r.get("deployed_at") or r.get("date"))
        if ts is None:
            st["skipped"] += 1
            continue
        desc = str(r.get("description") or r.get("summary") or r.get("message") or "")
        out.append(Change(
            id=str(r.get("id") or r.get("change_id") or f"CHG-{i + 1:04d}"),
            ts=ts,
            service=str(r.get("service") or r.get("component") or "unknown"),
            type=_norm_change_type(r.get("type") or r.get("change_type"), desc),
            description=desc,
            author=r.get("author") or r.get("user"),
            version=str(r["version"]) if r.get("version") not in (None, "") else None,
        ))
    out.sort(key=lambda c: c.ts)
    return out


# --------------------------------------------------------------------------- dependencies
@dataclass
class DependencyMap:
    services: dict[str, dict] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (caller, callee)

    def to_dict(self) -> dict:
        return {"services": [{"name": k, **v} for k, v in self.services.items()],
                "edges": [{"from": a, "to": b} for a, b in self.edges]}


def parse_dependencies(data: bytes | str, stats: dict | None = None) -> DependencyMap:
    st = stats if stats is not None else {}
    st.update(edges_raw=0, duplicate_edges=0, self_edges=0, invalid_edges=0)
    text = _decode(data).strip()
    dm = DependencyMap()
    if not text:
        return dm
    raw_edges: list[tuple] = []

    def edge_of(e) -> tuple | None:
        if not isinstance(e, dict):
            return None
        a = e.get("from") or e.get("source") or e.get("caller")
        b = e.get("to") or e.get("target") or e.get("callee")
        return (str(a).strip(), str(b).strip()) if a and b else None

    if text[0] in "[{":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise ParseError(f"Invalid dependency JSON: {e}") from e
        if isinstance(obj, dict) and ("edges" in obj or "services" in obj or "nodes" in obj):
            for s in obj.get("services", obj.get("nodes", [])) or []:
                if isinstance(s, str):
                    dm.services[s] = {}
                elif isinstance(s, dict):
                    name = s.get("name") or s.get("id")
                    if name:
                        dm.services[str(name)] = {k: v for k, v in s.items() if k not in ("name", "id")}
            raw_edges = [edge_of(e) for e in (obj.get("edges", obj.get("links", [])) or [])]
        elif isinstance(obj, dict):  # adjacency map {"caller": ["callee", ...]}
            for a, deps in obj.items():
                dm.services.setdefault(str(a), {})
                if isinstance(deps, str):
                    deps = [deps]
                if not isinstance(deps, list):
                    raise ParseError(f"Dependency map: value for '{a}' must be a list of service names")
                raw_edges += [(str(a), str(b)) for b in deps if b]
        elif isinstance(obj, list):
            raw_edges = [edge_of(e) for e in obj]
        else:
            raise ParseError("Dependency JSON must be an object or a list of edges")
    else:
        reader = csv.DictReader(io.StringIO(text))
        reader.fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
        if not {"from", "to"} <= set(reader.fieldnames) and not {"source", "target"} <= set(reader.fieldnames):
            raise ParseError("Dependency CSV needs 'from,to' (or 'source,target') columns")
        raw_edges = [edge_of(r) for r in reader]
    seen = set()
    for e in raw_edges:
        st["edges_raw"] += 1
        if e is None:
            st["invalid_edges"] += 1
            continue
        if e[0] == e[1]:
            st["self_edges"] += 1
            continue
        if e in seen:
            st["duplicate_edges"] += 1
            continue
        seen.add(e)
        dm.edges.append(e)
    for a, b in dm.edges:
        dm.services.setdefault(a, {})
        dm.services.setdefault(b, {})
    return dm
