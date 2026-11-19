"""Generate realistic sample incident datasets for RootCause Copilot.

Run:  python scripts/generate_samples.py
Writes to backend/sample_data/<scenario_id>/ :
    meta.json, metrics.csv, app.log, changes.json, dependencies.json
The output is deterministic (fixed random seed per scenario).
"""
from __future__ import annotations

import csv
import json
import math
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "sample_data"



def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- shapes
def effect_value(shape: str, minute: int, start: int, mag: float, end: int | None, period: int = 20) -> float:
    """Return the additive/override value contribution of an effect at a given minute."""
    if minute < start or (end is not None and minute > end):
        return 0.0
    t = minute - start
    if shape == "step":  # ramps to full magnitude over 3 minutes
        return mag * min(1.0, (t + 1) / 3)
    if shape == "linear":  # grows by mag per minute
        return mag * t
    if shape == "sawtooth":  # grows then drops (restart / OOM kill)
        return mag * (t % period)
    if shape == "spike":
        return mag if t < 3 else 0.0
    return 0.0


def build_metrics(rng, start, minutes, baselines, effects, caps):
    rows = []
    for m in range(minutes):
        ts = start + timedelta(minutes=m)
        for svc, base in baselines.items():
            for metric, (mean, noise) in base.items():
                diurnal = 1 + 0.04 * math.sin(m / 30)
                v = mean * diurnal + rng.gauss(0, noise)
                for eff in effects:
                    if eff["service"] == svc and eff["metric"] == metric:
                        v += effect_value(eff["shape"], m, eff["start"], eff["mag"], eff.get("end"), eff.get("period", 20))
                cap = caps.get((svc, metric))
                if cap is not None:
                    v = min(v, cap + rng.uniform(-0.2, 0))
                if metric.endswith("_pct"):
                    v = max(0.0, min(100.0, v))
                v = max(0.0, v)
                rows.append([iso(ts), svc, metric, round(v, 2)])
    return rows


def write_scenario(sid: str, meta: dict, start: datetime, minutes: int, baselines: dict, effects: list,
                   caps: dict, log_events: list, changes: list, deps: dict, seed: int):
    rng = random.Random(seed)
    d = OUT / sid
    d.mkdir(parents=True, exist_ok=True)

    rows = build_metrics(rng, start, minutes, baselines, effects, caps)
    with open(d / "metrics.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "service", "metric", "value"])
        w.writerows(rows)

    # ---- logs
    lines = []
    services = list(baselines.keys())
    info_msgs = [
        "GET /health 200 {ms}ms",
        "Request completed status=200 duration={ms}ms",
        "Processed batch size={n} in {ms}ms",
        "Cache refresh completed entries={n}",
    ]
    warn_noise = [
        "Slow request detected duration={big}ms path=/api/v1/items",
        "Retrying request attempt=2 reason=connection reset",
    ]
    for m in range(minutes):
        ts0 = start + timedelta(minutes=m)
        for svc in services:
            for _ in range(rng.randint(1, 2)):
                ts = ts0 + timedelta(seconds=rng.randint(0, 59))
                msg = rng.choice(info_msgs).format(ms=rng.randint(4, 90), n=rng.randint(10, 500))
                lines.append((ts, "INFO", svc, msg))
            if rng.random() < 0.03:
                ts = ts0 + timedelta(seconds=rng.randint(0, 59))
                lines.append((ts, "WARN", svc, rng.choice(warn_noise).format(big=rng.randint(900, 1600))))
    for ev in log_events:
        for m in range(ev["start"], ev.get("end", minutes)):
            ts0 = start + timedelta(minutes=m)
            rate = ev["rate"] if isinstance(ev["rate"], (int, float)) else ev["rate"](m - ev["start"])
            rate = max(0.0, rng.gauss(rate, rate * 0.3)) if rate >= 2 else rate  # bursty, not constant
            n = int(rate) + (1 if rng.random() < (rate - int(rate)) else 0)
            for _ in range(n):
                ts = ts0 + timedelta(seconds=rng.randint(0, 59))
                msg = ev["msg"].format(
                    n=rng.randint(1, 9999), ms=rng.randint(5000, 30000), id=uuid.UUID(int=rng.getrandbits(128)).hex[:12],
                    ip=f"10.0.{rng.randint(1, 9)}.{rng.randint(2, 250)}",
                )
                lines.append((ts, ev["level"], ev["service"], msg))
    lines.sort(key=lambda x: x[0])
    with open(d / "app.log", "w") as f:
        for ts, lvl, svc, msg in lines:
            f.write(f"{iso(ts)} {lvl:<5} [{svc}] {msg}\n")

    with open(d / "changes.json", "w") as f:
        json.dump(changes, f, indent=2)
    with open(d / "dependencies.json", "w") as f:
        json.dump(deps, f, indent=2)
    with open(d / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"wrote {sid}: {len(rows)} metric rows, {len(lines)} log lines")


def base(cpu, mem, lat, err, rps, db=None):
    b = {
        "cpu_pct": (cpu, 2.5), "memory_pct": (mem, 1.2), "latency_p95_ms": (lat, lat * 0.06),
        "error_rate_pct": (err, 0.08), "request_rate": (rps, rps * 0.04),
    }
    if db is not None:
        b["db_pool_utilization_pct"] = (db, 2.0)
    return b


# =========================================================================== scenario 1
def scenario_checkout():
    start = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)
    T = 65  # minute of the faulty deploy (14:05)
    baselines = {
        "web-frontend": base(28, 46, 180, 0.3, 920),
        "api-gateway": base(35, 52, 120, 0.2, 1400),
        "auth-service": base(22, 41, 45, 0.1, 610),
        "checkout-service": base(31, 55, 210, 0.3, 340),
        "inventory-service": base(26, 49, 60, 0.2, 520),
        "payment-service": base(30, 58, 260, 0.2, 180, db=36),
        "redis-cache": base(12, 63, 3, 0.0, 2100),
        "postgres-payments": base(24, 61, 9, 0.0, 950),
    }
    effects = [
        {"service": "payment-service", "metric": "db_pool_utilization_pct", "shape": "step", "start": T + 2, "mag": 70},
        {"service": "payment-service", "metric": "latency_p95_ms", "shape": "step", "start": T + 2, "mag": 4600},
        {"service": "payment-service", "metric": "error_rate_pct", "shape": "step", "start": T + 3, "mag": 18},
        {"service": "payment-service", "metric": "cpu_pct", "shape": "step", "start": T + 3, "mag": 9},
        {"service": "checkout-service", "metric": "latency_p95_ms", "shape": "step", "start": T + 4, "mag": 5100},
        {"service": "checkout-service", "metric": "error_rate_pct", "shape": "step", "start": T + 5, "mag": 14},
        {"service": "api-gateway", "metric": "error_rate_pct", "shape": "step", "start": T + 5, "mag": 6.5},
        {"service": "api-gateway", "metric": "latency_p95_ms", "shape": "step", "start": T + 5, "mag": 1300},
        {"service": "web-frontend", "metric": "error_rate_pct", "shape": "step", "start": T + 6, "mag": 4.2},
        {"service": "web-frontend", "metric": "request_rate", "shape": "step", "start": T + 8, "mag": -180},
        {"service": "postgres-payments", "metric": "request_rate", "shape": "step", "start": T + 2, "mag": -520},
    ]
    caps = {}
    log_events = [
        {"service": "payment-service", "level": "INFO", "start": T, "end": T + 1, "rate": 2,
         "msg": "Starting payment-service v2.14.0 (config profile=prod, db.pool.maxSize=10)"},
        {"service": "payment-service", "level": "ERROR", "start": T + 2, "rate": 9,
         "msg": "HikariPool-1 - Connection is not available, request timed out after 30000ms. (total=10, active=10, idle=0, waiting={n})"},
        {"service": "payment-service", "level": "WARN", "start": T + 2, "rate": 4,
         "msg": "HikariPool-1 - Thread starvation or clock leap detected; pool at max size 10"},
        {"service": "payment-service", "level": "ERROR", "start": T + 3, "rate": 3,
         "msg": "Failed to authorize payment txn={id}: SQLTransientConnectionException: Unable to acquire JDBC Connection"},
        {"service": "checkout-service", "level": "ERROR", "start": T + 4, "rate": 7,
         "msg": "Upstream payment-service request timed out after {ms}ms order={id}"},
        {"service": "checkout-service", "level": "WARN", "start": T + 4, "rate": 3,
         "msg": "Circuit breaker payment-client state=HALF_OPEN failures={n}"},
        {"service": "api-gateway", "level": "ERROR", "start": T + 5, "rate": 5,
         "msg": "502 Bad Gateway from upstream checkout-service path=/api/v1/checkout"},
        {"service": "web-frontend", "level": "ERROR", "start": T + 6, "rate": 3,
         "msg": "Checkout submission failed: HTTP 502 session={id}"},
        # red herring: inventory feature flag noise, low rate
        {"service": "inventory-service", "level": "WARN", "start": 40, "end": 44, "rate": 1,
         "msg": "Feature flag 'bulk-reservations' evaluated with fallback value for tenant={n}"},
    ]
    changes = [
        {"id": "CHG-4812", "timestamp": iso(start + timedelta(minutes=40)), "service": "inventory-service",
         "type": "feature_flag", "description": "Enabled 'bulk-reservations' feature flag for 10% of tenants",
         "author": "m.chen", "version": None},
        {"id": "CHG-4817", "timestamp": iso(start + timedelta(minutes=T)), "service": "payment-service",
         "type": "deployment", "description": "Deploy payment-service v2.14.0 - refactor connection handling; db.pool.maxSize changed 50 -> 10",
         "author": "ci-pipeline", "version": "v2.14.0"},
        {"id": "CHG-4819", "timestamp": iso(start + timedelta(minutes=T + 22)), "service": "checkout-service",
         "type": "config", "description": "Increase payment-client timeout 5000ms -> 8000ms (mitigation attempt)",
         "author": "s.okafor", "version": None},
        {"id": "CHG-4801", "timestamp": iso(start - timedelta(hours=3)), "service": "auth-service",
         "type": "deployment", "description": "Deploy auth-service v5.3.1 - dependency upgrades", "author": "ci-pipeline",
         "version": "v5.3.1"},
    ]
    deps = {
        "services": [
            {"name": "web-frontend", "tier": "edge", "owner": "web-team"},
            {"name": "api-gateway", "tier": "edge", "owner": "platform"},
            {"name": "auth-service", "tier": "core", "owner": "identity"},
            {"name": "checkout-service", "tier": "core", "owner": "commerce"},
            {"name": "inventory-service", "tier": "core", "owner": "commerce"},
            {"name": "payment-service", "tier": "core", "owner": "payments"},
            {"name": "redis-cache", "tier": "data", "owner": "platform"},
            {"name": "postgres-payments", "tier": "data", "owner": "dba"},
        ],
        "edges": [
            {"from": "web-frontend", "to": "api-gateway"},
            {"from": "api-gateway", "to": "auth-service"},
            {"from": "api-gateway", "to": "checkout-service"},
            {"from": "api-gateway", "to": "inventory-service"},
            {"from": "checkout-service", "to": "payment-service"},
            {"from": "checkout-service", "to": "inventory-service"},
            {"from": "inventory-service", "to": "redis-cache"},
            {"from": "auth-service", "to": "redis-cache"},
            {"from": "payment-service", "to": "postgres-payments"},
        ],
    }
    meta = {
        "id": "checkout-failures", "title": "Checkout failures and elevated 502s",
        "description": "Customers report failed checkouts. API gateway 502 rate spiked shortly after 14:05 UTC.",
        "severity": "SEV-1", "reported_at": iso(start + timedelta(minutes=T + 12)),
        "expected_root_cause": "payment-service v2.14.0 reduced the DB connection pool (50 -> 10), exhausting connections",
    }
    write_scenario("checkout-failures", meta, start, 150, baselines, effects, caps, log_events, changes, deps, seed=7)


# =========================================================================== scenario 2
def scenario_memory_leak():
    start = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
    T = 60  # 09:00 release
    baselines = {
        "storefront-web": base(25, 44, 240, 0.2, 780),
        "api-gateway": base(33, 50, 110, 0.2, 1250),
        "catalog-service": base(29, 53, 95, 0.2, 640),
        "recommendation-service": base(34, 48, 140, 0.2, 410),
        "search-service": base(41, 57, 85, 0.1, 380),
        "elasticsearch": base(38, 66, 22, 0.0, 900),
        "feature-store": base(18, 59, 12, 0.0, 700),
    }
    effects = [
        {"service": "recommendation-service", "metric": "memory_pct", "shape": "sawtooth", "start": T + 3, "mag": 2.3, "period": 22},
        {"service": "recommendation-service", "metric": "cpu_pct", "shape": "sawtooth", "start": T + 12, "mag": 1.9, "period": 22},
        {"service": "recommendation-service", "metric": "latency_p95_ms", "shape": "sawtooth", "start": T + 12, "mag": 120, "period": 22},
        {"service": "recommendation-service", "metric": "error_rate_pct", "shape": "step", "start": T + 21, "mag": 7},
        {"service": "catalog-service", "metric": "latency_p95_ms", "shape": "step", "start": T + 22, "mag": 1650},
        {"service": "catalog-service", "metric": "error_rate_pct", "shape": "step", "start": T + 24, "mag": 3.6},
        {"service": "storefront-web", "metric": "latency_p95_ms", "shape": "step", "start": T + 24, "mag": 1400},
        {"service": "api-gateway", "metric": "latency_p95_ms", "shape": "step", "start": T + 24, "mag": 700},
    ]
    caps = {("recommendation-service", "memory_pct"): 97.0}
    log_events = [
        {"service": "recommendation-service", "level": "INFO", "start": T, "end": T + 1, "rate": 2,
         "msg": "Starting recommendation-service v3.2.0 with in-memory embedding cache enabled"},
        {"service": "recommendation-service", "level": "WARN", "start": T + 10, "rate": 2.5,
         "msg": "GC overhead high: full GC pause {n}ms, heap usage above 85%"},
        {"service": "recommendation-service", "level": "ERROR", "start": T + 21, "rate": 2,
         "msg": "java.lang.OutOfMemoryError: Java heap space at EmbeddingCache.put(EmbeddingCache.java:{n})"},
        {"service": "recommendation-service", "level": "ERROR", "start": T + 21, "rate": 0.25,
         "msg": "Container recommendation-service-7f9c killed: OOMKilled (exit code 137), restarting"},
        {"service": "catalog-service", "level": "ERROR", "start": T + 22, "rate": 4,
         "msg": "Timeout calling recommendation-service /v1/recs after {ms}ms; serving fallback"},
        {"service": "storefront-web", "level": "WARN", "start": T + 24, "rate": 3,
         "msg": "Product page render slow: {ms}ms (recommendations widget pending)"},
        # red herring: search reindex warnings before release
        {"service": "search-service", "level": "WARN", "start": 25, "end": 35, "rate": 1,
         "msg": "Index refresh lagging behind by {n} documents"},
    ]
    changes = [
        {"id": "CHG-7710", "timestamp": iso(start + timedelta(minutes=20)), "service": "search-service",
         "type": "infrastructure", "description": "Elasticsearch node pool scaled 6 -> 8 nodes; shard rebalance",
         "author": "infra-bot", "version": None},
        {"id": "CHG-7714", "timestamp": iso(start + timedelta(minutes=T)), "service": "recommendation-service",
         "type": "deployment", "description": "Release recommendation-service v3.2.0 - add in-memory embedding cache for personalised recs",
         "author": "ci-pipeline", "version": "v3.2.0"},
        {"id": "CHG-7722", "timestamp": iso(start + timedelta(minutes=T + 50)), "service": "catalog-service",
         "type": "config", "description": "Lower recommendation call timeout to 800ms (mitigation)",
         "author": "j.alvarez", "version": None},
    ]
    deps = {
        "services": [
            {"name": "storefront-web", "tier": "edge", "owner": "web-team"},
            {"name": "api-gateway", "tier": "edge", "owner": "platform"},
            {"name": "catalog-service", "tier": "core", "owner": "catalog"},
            {"name": "recommendation-service", "tier": "core", "owner": "ml-platform"},
            {"name": "search-service", "tier": "core", "owner": "search"},
            {"name": "elasticsearch", "tier": "data", "owner": "search"},
            {"name": "feature-store", "tier": "data", "owner": "ml-platform"},
        ],
        "edges": [
            {"from": "storefront-web", "to": "api-gateway"},
            {"from": "api-gateway", "to": "catalog-service"},
            {"from": "api-gateway", "to": "search-service"},
            {"from": "catalog-service", "to": "recommendation-service"},
            {"from": "recommendation-service", "to": "feature-store"},
            {"from": "search-service", "to": "elasticsearch"},
        ],
    }
    meta = {
        "id": "product-page-latency", "title": "Product pages slow, intermittent recommendation outages",
        "description": "Product detail pages p95 latency above 2s since ~09:25 UTC. Recommendation widget often empty.",
        "severity": "SEV-2", "reported_at": iso(start + timedelta(minutes=T + 30)),
        "expected_root_cause": "Memory leak in recommendation-service v3.2.0 embedding cache causing OOM kills",
    }
    write_scenario("product-page-latency", meta, start, 140, baselines, effects, caps, log_events, changes, deps, seed=11)


# =========================================================================== scenario 3
def scenario_tls():
    start = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
    T = 60  # 00:00 cert expiry - no change involved
    baselines = {
        "mobile-bff": base(30, 47, 150, 0.2, 860),
        "api-gateway": base(36, 51, 110, 0.2, 1500),
        "auth-service": base(24, 43, 40, 0.1, 720),
        "orders-service": base(28, 54, 130, 0.2, 300),
        "notification-service": base(15, 39, 70, 0.3, 120),
        "users-db": base(21, 62, 8, 0.0, 800),
        "kafka": base(33, 58, 6, 0.0, 2400),
    }
    effects = [
        {"service": "auth-service", "metric": "error_rate_pct", "shape": "step", "start": T, "mag": 0.4},
        {"service": "api-gateway", "metric": "error_rate_pct", "shape": "step", "start": T, "mag": 38},
        {"service": "api-gateway", "metric": "latency_p95_ms", "shape": "step", "start": T, "mag": 90},
        {"service": "auth-service", "metric": "request_rate", "shape": "step", "start": T, "mag": -640},
        {"service": "mobile-bff", "metric": "error_rate_pct", "shape": "step", "start": T + 1, "mag": 31},
        {"service": "orders-service", "metric": "request_rate", "shape": "step", "start": T + 1, "mag": -230},
        {"service": "mobile-bff", "metric": "request_rate", "shape": "step", "start": T + 6, "mag": 420},  # client retries
    ]
    log_events = [
        {"service": "api-gateway", "level": "ERROR", "start": T, "rate": 12,
         "msg": "TLS handshake with upstream auth-service.internal:8443 failed: x509: certificate has expired or is not yet valid: current time 2026-11-17T00:{n} is after 2026-11-17T00:00:00Z"},
        {"service": "api-gateway", "level": "ERROR", "start": T, "rate": 6,
         "msg": "401 Unauthorized - token validation unavailable (auth upstream error) path=/v2/orders"},
        {"service": "auth-service", "level": "WARN", "start": T, "rate": 2,
         "msg": "http: TLS handshake error from {ip}:443: remote error: tls: bad certificate"},
        {"service": "mobile-bff", "level": "ERROR", "start": T + 1, "rate": 6,
         "msg": "Login failed for session={id}: upstream returned 401 Unauthorized"},
        {"service": "notification-service", "level": "WARN", "start": 15, "end": 22, "rate": 1,
         "msg": "Kafka consumer lag above threshold partition={n}"},
        {"service": "auth-service", "level": "WARN", "start": 0, "end": 1, "rate": 1,
         "msg": "Certificate for auth-service.internal expires in 59 minutes (not_after=2026-11-17T00:00:00Z)"},
    ]
    changes = [
        {"id": "CHG-9902", "timestamp": iso(start + timedelta(minutes=-300)), "service": "orders-service",
         "type": "deployment", "description": "Deploy orders-service v1.41.0 - pagination improvements", "author": "ci-pipeline",
         "version": "v1.41.0"},
        {"id": "CHG-9905", "timestamp": iso(start + timedelta(minutes=10)), "service": "notification-service",
         "type": "config", "description": "Increase Kafka consumer max.poll.records 200 -> 500", "author": "r.singh", "version": None},
        {"id": "CHG-9910", "timestamp": iso(start + timedelta(minutes=T + 35)), "service": "auth-service",
         "type": "infrastructure", "description": "Emergency rotation of auth-service.internal TLS certificate (mitigation)",
         "author": "oncall-sre", "version": None},
    ]
    deps = {
        "services": [
            {"name": "mobile-bff", "tier": "edge", "owner": "mobile"},
            {"name": "api-gateway", "tier": "edge", "owner": "platform"},
            {"name": "auth-service", "tier": "core", "owner": "identity"},
            {"name": "orders-service", "tier": "core", "owner": "orders"},
            {"name": "notification-service", "tier": "core", "owner": "engagement"},
            {"name": "users-db", "tier": "data", "owner": "dba"},
            {"name": "kafka", "tier": "data", "owner": "platform"},
        ],
        "edges": [
            {"from": "mobile-bff", "to": "api-gateway"},
            {"from": "api-gateway", "to": "auth-service"},
            {"from": "api-gateway", "to": "orders-service"},
            {"from": "auth-service", "to": "users-db"},
            {"from": "orders-service", "to": "kafka"},
            {"from": "notification-service", "to": "kafka"},
        ],
    }
    meta = {
        "id": "login-outage", "title": "Mobile login outage - widespread 401s",
        "description": "Mobile users unable to log in since midnight UTC. Gateway returning 401/5xx for authenticated routes.",
        "severity": "SEV-1", "reported_at": iso(start + timedelta(minutes=T + 9)),
        "expected_root_cause": "Expired TLS certificate on auth-service.internal broke gateway -> auth handshake",
    }
    write_scenario("login-outage", meta, start, 120, baselines, effects, {}, log_events, changes, deps, seed=23)


if __name__ == "__main__":
    scenario_checkout()
    scenario_memory_leak()
    scenario_tls()
