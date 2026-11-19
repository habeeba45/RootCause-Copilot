"""Failure-mode knowledge base used by the rule-based reasoning engine."""
from __future__ import annotations

MODE_LABELS = {
    "db_connection_pool": "Database connection pool exhaustion",
    "memory": "Memory leak / out-of-memory",
    "certificate": "Expired or invalid TLS certificate",
    "crash": "Process crash / restart loop",
    "dns": "DNS resolution failure",
    "disk": "Disk / storage exhaustion",
    "rate_limit": "Rate limiting / quota exhaustion",
    "auth": "Authentication / authorization failure",
    "database": "Database errors (locks, schema, queries)",
    "config": "Misconfiguration",
    "network": "Network connectivity failure",
    "cpu_saturation": "CPU saturation",
    "dependency_failure": "Upstream dependency failure",
    "timeout": "Timeouts / slow responses",
    "performance_degradation": "Performance degradation",
    "error_spike": "Error-rate spike",
    "exception": "Unhandled application exceptions",
    "unknown": "Unclassified failure",
}

PLAYBOOKS: dict[str, dict[str, list[str]]] = {
    "db_connection_pool": {
        "immediate": [
            "Restore the previous connection-pool size (or roll back the offending release) on {service}.",
            "Restart {service} pods after the rollback to drain queued requests and stale connections.",
        ],
        "short_term": [
            "Alert on pool utilisation > 80% and on connection-acquire wait time for {service}.",
            "Size the pool from measured peak concurrency (threads x avg query time) and confirm the database max_connections budget.",
        ],
        "long_term": [
            "Validate datasource/pool settings in CI with a config-diff check that flags reductions in capacity.",
            "Add a load test stage that exercises peak checkout traffic before production rollout.",
        ],
    },
    "memory": {
        "immediate": [
            "Roll back {service} to the last known-good version, or disable the newly added in-memory cache via flag.",
            "Temporarily raise memory limits / replica count for {service} to reduce OOM kill frequency.",
        ],
        "short_term": [
            "Capture a heap dump from an affected {service} instance and identify the retaining object graph.",
            "Bound in-process caches with max size + TTL (LRU/W-TinyLFU) and expose cache size metrics.",
        ],
        "long_term": [
            "Add a soak test that tracks heap growth over 60+ minutes as a release gate.",
            "Alert on memory growth slope and on container OOMKilled / restart events.",
        ],
    },
    "certificate": {
        "immediate": [
            "Rotate the expired TLS certificate for {service} and reload the listeners.",
            "Verify the full chain from every caller (openssl s_client) before closing the incident.",
        ],
        "short_term": [
            "Inventory all internal certificates and their expiry dates; renew anything expiring in < 30 days.",
            "Alert at 30/14/7/1 days before certificate expiry, routed to the owning team.",
        ],
        "long_term": [
            "Automate issuance and renewal (cert-manager / ACME / internal PKI) with short-lived certificates.",
            "Add a synthetic TLS handshake probe for every internal endpoint.",
        ],
    },
    "crash": {
        "immediate": ["Roll back {service} or pin the previous image; collect crash logs and exit codes."],
        "short_term": ["Add liveness/readiness tuning and crash-loop alerting for {service}."],
        "long_term": ["Add canary analysis that blocks rollouts on elevated restart counts."],
    },
    "dns": {
        "immediate": ["Revert recent DNS / resolver changes and flush resolver caches for {service}."],
        "short_term": ["Add DNS resolution latency/error metrics and synthetic lookups."],
        "long_term": ["Introduce staged rollout and validation for DNS configuration."],
    },
    "disk": {
        "immediate": ["Free disk space on {service} hosts (rotate logs, purge temp files) and expand volumes."],
        "short_term": ["Alert at 75%/90% disk usage and inode exhaustion."],
        "long_term": ["Enforce log retention policies and capacity forecasting."],
    },
    "rate_limit": {
        "immediate": ["Request a quota increase or reduce request fan-out from {service}; enable backoff."],
        "short_term": ["Add client-side rate limiting with jittered exponential retries."],
        "long_term": ["Track quota headroom as an SLO with alerting."],
    },
    "auth": {
        "immediate": ["Check identity provider health, signing keys and token validation config for {service}."],
        "short_term": ["Add alerting on 401/403 rate by route and client."],
        "long_term": ["Automate key rotation tests in staging."],
    },
    "database": {
        "immediate": ["Identify blocking queries / locks on the database used by {service}; kill or roll back the migration."],
        "short_term": ["Add slow query and lock-wait monitoring."],
        "long_term": ["Run schema migrations through online-migration tooling with rehearsal in staging."],
    },
    "config": {
        "immediate": ["Revert the configuration change on {service}."],
        "short_term": ["Validate configuration against a schema before rollout."],
        "long_term": ["Manage configuration through version-controlled, reviewed, progressively-delivered pipelines."],
    },
    "cpu_saturation": {
        "immediate": ["Scale out {service} and roll back any recent change that increased per-request CPU."],
        "short_term": ["Profile {service} (flame graphs) to find hot paths."],
        "long_term": ["Add autoscaling on CPU and a performance regression gate in CI."],
    },
    "network": {
        "immediate": ["Check network policies, load balancers and security groups for {service}."],
        "short_term": ["Add connection error metrics per upstream."],
        "long_term": ["Use staged rollouts for network policy changes."],
    },
}
GENERIC = {
    "immediate": ["Mitigate impact on {service}: roll back the most recent change, shift traffic, or scale out."],
    "short_term": ["Add targeted alerting for the failing signals observed on {service}."],
    "long_term": ["Schedule a blameless post-incident review and track follow-up actions to completion."],
}
CASCADE_RECS = [
    "Add timeouts, circuit breakers and graceful fallbacks in callers ({callers}) so a single dependency cannot cascade.",
]
CHANGE_RECS = [
    "Roll back {change_id} ({change_desc}) if not already done, and confirm metrics return to baseline.",
    "Require canary analysis with automatic rollback for {change_type} changes on {service}.",
]


def recommendations(mode: str, service: str, change: dict | None, callers: list[str]) -> dict[str, list[str]]:
    pb = PLAYBOOKS.get(mode, GENERIC)
    out = {k: [s.format(service=service) for s in pb.get(k, [])] for k in ("immediate", "short_term", "long_term")}
    if change:
        out["immediate"].insert(0, CHANGE_RECS[0].format(change_id=change["id"], change_desc=change["description"][:90]))
        out["long_term"].insert(0, CHANGE_RECS[1].format(change_type=change["type"].replace("_", " "), service=service))
    if callers:
        out["short_term"].append(CASCADE_RECS[0].format(callers=", ".join(callers[:4])))
    out["long_term"].append(GENERIC["long_term"][0].format(service=service))
    return out
