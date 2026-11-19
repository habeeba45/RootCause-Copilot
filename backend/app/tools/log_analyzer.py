"""Log analysis tool: template mining, error clustering, failure classification, cross-service references."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from ..parsers import LogEntry, iso

# Ordered: first match wins, so specific signatures come before generic ones.
FAILURE_CATEGORIES: list[tuple[str, str, tuple[str, ...]]] = [
    ("certificate", "TLS / certificate failure",
     ("x509", "certificate has expired", "certificate expired", "bad certificate", "tls handshake", "ssl handshake",
      "certificate", "cert ")),
    ("db_connection_pool", "Database connection pool exhaustion",
     ("hikaripool", "connection is not available", "unable to acquire jdbc", "too many connections",
      "connection pool", "pool exhausted", "pool at max", "remaining connection slots")),
    ("memory", "Memory exhaustion / leak",
     ("outofmemoryerror", "out of memory", "oomkilled", "heap space", "gc overhead", "memory limit", "heap usage",
      "full gc")),
    ("crash", "Process crash / restart loop",
     ("exit code 137", "crashloopbackoff", "segfault", "panic:", "killed", "restarting", "core dumped")),
    ("dns", "DNS resolution failure",
     ("no such host", "name resolution", "nxdomain", "dns", "could not resolve", "unknownhostexception")),
    ("disk", "Disk / storage exhaustion",
     ("no space left", "disk full", "disk quota", "read-only file system", "inode")),
    ("rate_limit", "Rate limiting / throttling",
     ("429", "rate limit", "throttl", "too many requests", "quota exceeded")),
    ("auth", "Authentication / authorization failure",
     ("401", "403", "unauthorized", "forbidden", "invalid token", "token validation", "permission denied")),
    ("database", "Database error",
     ("deadlock", "sqlexception", "sqlstate", "duplicate key", "relation does not exist", "lock wait timeout",
      "query timeout")),
    ("config", "Configuration error",
     ("missing config", "invalid configuration", "config error", "property not found", "env var", "misconfigur",
      "no such file")),
    ("dependency_failure", "Upstream / dependency failure",
     ("bad gateway", "502", "503", "504", "service unavailable", "upstream", "circuit breaker", "connection refused",
      "fallback")),
    ("timeout", "Timeouts / slow responses",
     ("timed out", "timeout", "deadline exceeded", "slow")),
    ("network", "Network failure",
     ("connection reset", "broken pipe", "network unreachable", "no route to host", "econnreset")),
    ("exception", "Unhandled exception",
     ("exception", "nullpointer", "traceback", "stack trace", "error")),
]
CATEGORY_LABELS = {k: label for k, label, _ in FAILURE_CATEGORIES}
CATEGORY_LABELS["unknown"] = "Unclassified error"
# categories that describe a *consequence* of someone else's failure rather than a local fault
SYMPTOM_CATEGORIES = {"dependency_failure", "timeout", "exception", "unknown"}

_TEMPLATE_SUBS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{10,}\b", re.I), "<hex>"),
    (re.compile(r"(?<![\w.])v?\d+(?:\.\d+)+\b"), "<ver>"),
    (re.compile(r"\b\d+(?:ms|s|m|kb|mb|gb|%)?\b", re.I), "<num>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
]
_VERSION_RE = re.compile(r"(?<![\w.])v\d+(?:\.\d+){1,3}\b")


def templatize(msg: str) -> str:
    t = msg
    for rx, rep in _TEMPLATE_SUBS:
        t = rx.sub(rep, t)
    return re.sub(r"\s+", " ", t).strip()[:220]


def classify(msg: str) -> str:
    m = msg.lower()
    for key, _, needles in FAILURE_CATEGORIES:
        if any(n in m for n in needles):
            return key
    return "unknown"


class LogAnalyzer:
    name = "log_analyzer"
    description = "Clusters log messages into templates, classifies failure signatures and finds error bursts."

    def run(self, entries: list[LogEntry], onset: datetime | None = None,
            known_services: set[str] | None = None) -> dict:
        known = set(known_services or set()) | {e.service for e in entries if e.service != "unknown"}
        timed = [e for e in entries if e.ts]
        t_min = min((e.ts for e in timed), default=None)
        t_max = max((e.ts for e in timed), default=None)
        if onset is None and timed:
            onset = self._estimate_onset(timed)
        new_cutoff = onset - timedelta(minutes=30) if onset else None

        level_counts: dict[str, Counter] = defaultdict(Counter)
        clusters: dict[tuple[str, str], dict] = {}
        per_minute: dict[str, Counter] = defaultdict(Counter)
        versions_seen: list[dict] = []

        for e in entries:
            level_counts[e.service][e.level] += 1
            for v in _VERSION_RE.findall(e.message):
                versions_seen.append({"service": e.service, "version": v, "timestamp": iso(e.ts), "message": e.message[:200]})
            if e.level not in ("ERROR", "FATAL", "WARN"):
                continue
            if e.ts:
                per_minute[e.service][e.ts.replace(second=0, microsecond=0)] += 1 if e.level != "WARN" else 0
            tpl = templatize(e.message)
            key = (e.service, tpl)
            c = clusters.get(key)
            if c is None:
                c = clusters[key] = {
                    "service": e.service, "template": tpl, "sample": e.message[:300], "level": e.level,
                    "category": classify(e.message), "count": 0, "first_dt": e.ts, "last_dt": e.ts,
                    "mentions": sorted(s for s in known if s != e.service and s.lower() in e.message.lower()),
                }
            c["count"] += 1
            if e.level in ("ERROR", "FATAL"):
                c["level"] = e.level
            if e.ts:
                c["first_dt"] = min(c["first_dt"], e.ts) if c["first_dt"] else e.ts
                c["last_dt"] = max(c["last_dt"], e.ts) if c["last_dt"] else e.ts

        cluster_list = []
        for c in clusters.values():
            first = c["first_dt"]
            if new_cutoff and first:
                c["status"] = "new" if first >= new_cutoff else ("precursor" if c["count"] <= 3 else "recurring")
            else:
                c["status"] = "new"
            if c["status"] == "new" and first and c["last_dt"]:
                minutes = max(1.0, (c["last_dt"] - first).total_seconds() / 60)
                if c["count"] < 20 and c["count"] / minutes < 0.15:
                    c["status"] = "sporadic"  # background noise, not a burst
            c["category_label"] = CATEGORY_LABELS.get(c["category"], c["category"])
            c["first_seen"] = iso(first)
            c["last_seen"] = iso(c["last_dt"])
            cluster_list.append(c)
        cluster_list.sort(key=lambda c: (c["status"] != "new", -c["count"]))

        # error-burst onset per service from new ERROR clusters
        service_onsets: dict[str, datetime] = {}
        service_categories: dict[str, Counter] = defaultdict(Counter)
        references: Counter = Counter()
        for c in cluster_list:
            if c["status"] != "new" or c["count"] < 3:
                continue
            if c["level"] == "WARN" and c["count"] < 10:
                continue  # a handful of warnings is not an error burst
            svc = c["service"]
            if c["first_dt"]:
                first_min = c["first_dt"].replace(second=0, microsecond=0)  # align with 1-min metric resolution
                service_onsets[svc] = min(service_onsets.get(svc, first_min), first_min)
            service_categories[svc][c["category"]] += c["count"] * (1.0 if c["level"] in ("ERROR", "FATAL") else 0.6)
            for m in c["mentions"]:
                references[(svc, m)] += c["count"]

        precursors = [c for c in cluster_list if c["status"] == "precursor"
                      and c["category"] not in SYMPTOM_CATEGORIES | {"network"}]

        error_timeline = []
        for svc, counter in per_minute.items():
            for t, n in sorted(counter.items()):
                if n:
                    error_timeline.append({"service": svc, "minute": iso(t), "errors": n})

        for c in cluster_list:
            c.pop("first_dt", None)
            c.pop("last_dt", None)

        return {
            "total_lines": len(entries),
            "window": {"start": iso(t_min), "end": iso(t_max)},
            "level_counts": {s: dict(c) for s, c in level_counts.items()},
            "clusters": cluster_list[:60],
            "new_error_clusters": [c for c in cluster_list if c["status"] == "new" and c["count"] >= 3][:30],
            "precursors": precursors[:10],
            "service_onsets": service_onsets,
            "dominant_categories": {
                s: [{"category": k, "label": CATEGORY_LABELS.get(k, k), "weight": round(v, 1)} for k, v in cnt.most_common()]
                for s, cnt in service_categories.items()
            },
            "references": [{"from": a, "to": b, "count": n} for (a, b), n in references.most_common()],
            "versions_seen": versions_seen[:20],
            "error_timeline": error_timeline,
        }

    @staticmethod
    def _estimate_onset(entries: list[LogEntry]) -> datetime | None:
        errs = sorted(e.ts for e in entries if e.level in ("ERROR", "FATAL"))
        if not errs:
            return None
        per_min = Counter(t.replace(second=0, microsecond=0) for t in errs)
        mean = sum(per_min.values()) / max(1, len(per_min))
        for t in sorted(per_min):
            if per_min[t] >= max(3, mean * 0.5):
                return t
        return errs[0]
