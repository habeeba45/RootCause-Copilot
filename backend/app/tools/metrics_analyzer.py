"""Metrics analysis tool: robust baseline + sustained anomaly detection per service/metric."""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from ..parsers import MetricPoint, iso

# Metrics where only an increase is bad; anything else is checked in both directions.
UPWARD_BAD = ("latency", "error", "cpu", "memory", "mem_", "pool", "db_conn", "connections", "queue", "lag",
              "disk", "gc", "restart", "5xx", "timeout", "saturation", "wait")
RESOURCE_HINTS = {
    "memory": ("memory", "mem_", "heap", "rss", "gc"),
    "cpu": ("cpu", "load"),
    "db_pool": ("pool", "db_conn", "connections"),
    "disk": ("disk", "iops", "inode"),
    "latency": ("latency", "duration", "response_time", "p95", "p99"),
    "errors": ("error", "5xx", "fail"),
    "traffic": ("request_rate", "rps", "throughput", "qps"),
}

Z_THRESHOLD = 4.0
SUSTAIN = 3


def metric_kind(metric: str) -> str:
    m = metric.lower()
    for kind, keys in RESOURCE_HINTS.items():
        if any(k in m for k in keys):
            return kind
    return "other"


def _robust_stats(values: list[float], metric: str) -> tuple[float, float]:
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) if len(values) > 1 else 0.0
    sigma = 1.4826 * mad
    floor = max(abs(med) * 0.03, 0.05)
    if "error" in metric.lower():
        floor = max(floor, 0.1)
    return med, max(sigma, floor)


class MetricsAnalyzer:
    name = "metrics_analyzer"
    description = "Builds robust baselines per service/metric and detects sustained statistical anomalies."

    def run(self, points: list[MetricPoint], hint_start: datetime | None = None) -> dict:
        series: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
        for p in points:
            series[(p.service, p.metric)].append((p.ts, p.value))

        t_min = min(p.ts for p in points)
        t_max = max(p.ts for p in points)
        span = (t_max - t_min).total_seconds()
        baseline_end = t_min + timedelta(seconds=span * 0.35)
        if hint_start is not None:
            hinted = hint_start - timedelta(minutes=10)
            if t_min + timedelta(minutes=10) < hinted < baseline_end:
                baseline_end = hinted

        anomalies = []
        chart_series: dict[str, dict[str, list]] = defaultdict(dict)
        for (svc, metric), pts in series.items():
            pts.sort()
            base_vals = [v for t, v in pts if t <= baseline_end]
            if len(base_vals) < 5:
                base_vals = [v for _, v in pts[: max(5, len(pts) // 3)]]
            med, sigma = _robust_stats(base_vals, metric)
            upward_only = any(k in metric.lower() for k in UPWARD_BAD)

            # downsample for charts (max ~200 points)
            step = max(1, len(pts) // 200)
            chart_series[svc][metric] = [[iso(t), round(v, 2)] for t, v in pts[::step]]

            run, onset_idx = 0, None
            for i, (t, v) in enumerate(pts):
                if t <= baseline_end:
                    continue
                z = (v - med) / sigma
                hit = z > Z_THRESHOLD if upward_only else abs(z) > Z_THRESHOLD
                if hit:
                    run += 1
                    if run == SUSTAIN:
                        onset_idx = i - SUSTAIN + 1
                        break
                else:
                    run = 0
            if onset_idx is None:
                continue
            after = pts[onset_idx:]
            zs = [((v - med) / sigma, t, v) for t, v in after]
            peak_z, peak_t, peak_v = max(zs, key=lambda x: abs(x[0]))
            direction = "up" if peak_z > 0 else "down"
            change_pct = ((peak_v - med) / med * 100) if med else None
            # sawtooth / restart detection: big drops after the onset
            drops = sum(1 for (_, a), (_, b) in zip(after, after[1:]) if a - b > 6 * sigma and a > med + 6 * sigma)
            anomalies.append({
                "service": svc,
                "metric": metric,
                "kind": metric_kind(metric),
                "onset": iso(pts[onset_idx][0]),
                "onset_dt": pts[onset_idx][0],
                "baseline": round(med, 2),
                "peak": round(peak_v, 2),
                "peak_at": iso(peak_t),
                "z_score": round(peak_z, 1),
                "direction": direction,
                "change_pct": round(change_pct, 1) if change_pct is not None else None,
                "pattern": "sawtooth/restarts" if drops >= 1 and metric_kind(metric) == "memory" else
                           ("trend" if self._is_trend(after) else "step"),
                "severity": round(min(1.0, abs(peak_z) / 25), 2),
            })
        anomalies.sort(key=lambda a: (a["onset_dt"], -abs(a["z_score"])))

        by_service: dict[str, dict] = {}
        for a in anomalies:
            s = by_service.setdefault(a["service"], {"onset_dt": a["onset_dt"], "metrics": [], "max_severity": 0})
            s["onset_dt"] = min(s["onset_dt"], a["onset_dt"])
            s["metrics"].append(a["metric"])
            s["max_severity"] = max(s["max_severity"], a["severity"])

        return {
            "window": {"start": iso(t_min), "end": iso(t_max), "baseline_end": iso(baseline_end)},
            "series_count": len(series),
            "services": sorted({s for s, _ in series}),
            "anomalies": anomalies,
            "by_service": by_service,
            "earliest_onset": anomalies[0]["onset_dt"] if anomalies else None,
            "chart_series": chart_series,
            "baseline_end_dt": baseline_end,
        }

    @staticmethod
    def _is_trend(after: list[tuple[datetime, float]]) -> bool:
        if len(after) < 8:
            return False
        vals = [v for _, v in after[: min(len(after), 30)]]
        rises = sum(1 for a, b in zip(vals, vals[1:]) if b > a)
        return rises / (len(vals) - 1) > 0.7
