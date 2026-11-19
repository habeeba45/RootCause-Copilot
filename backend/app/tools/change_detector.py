"""Change detection tool: finds deployments / config / infra changes correlated with incident onset."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from ..parsers import Change

TYPE_RISK = {"deployment": 1.0, "config": 0.9, "database": 0.9, "infrastructure": 0.8, "feature_flag": 0.7}
RISKY_KEYWORDS = {
    "db_connection_pool": ("pool", "connection", "jdbc", "datasource", "db."),
    "memory": ("cache", "memory", "heap", "buffer", "xmx", "limit"),
    "certificate": ("cert", "tls", "ssl", "x509"),
    "dns": ("dns", "resolver", "coredns", "hostname"),
    "timeout": ("timeout", "retry", "deadline"),
    "config": ("config", "setting", "env", "parameter"),
    "database": ("schema", "migration", "index", "query"),
    "rate_limit": ("rate", "limit", "quota", "throttle"),
    "auth": ("auth", "token", "oauth", "iam", "permission"),
}


class ChangeDetector:
    name = "change_detector"
    description = "Correlates deployments, config and infrastructure changes with the incident onset window."

    def run(self, changes: list[Change], onset: datetime | None, lookback_minutes: int = 240) -> dict:
        results = []
        for c in changes:
            delta = (onset - c.ts).total_seconds() / 60 if onset else None
            if delta is None:
                phase, proximity = "unknown", 0.3
            elif delta < -2:
                phase, proximity = "after_onset", 0.0
            elif delta <= lookback_minutes:
                phase, proximity = "before_onset", math.exp(-max(delta, 0) / 45)
            else:
                phase, proximity = "outside_window", 0.0
            text = f"{c.description} {c.type}".lower()
            hits = sorted({cat for cat, kws in RISKY_KEYWORDS.items() if any(k in text for k in kws)})
            risk = TYPE_RISK.get(c.type, 0.5) * (1.0 + 0.15 * min(len(hits), 3))
            mitigation = phase == "after_onset" and any(k in text for k in ("mitigat", "rollback", "revert", "emergency", "hotfix", "rotate"))
            results.append({
                **c.to_dict(),
                "minutes_before_onset": round(delta, 1) if delta is not None else None,
                "phase": phase,
                "proximity_score": round(proximity, 3),
                "risk_score": round(risk, 2),
                "suspicion": round(proximity * risk, 3),
                "keyword_categories": hits,
                "likely_mitigation": mitigation,
            })
        suspects = sorted([r for r in results if r["phase"] in ("before_onset", "unknown")], key=lambda r: -r["suspicion"])
        return {
            "total_changes": len(changes),
            "onset": onset.strftime("%Y-%m-%dT%H:%M:%SZ") if onset else None,
            "lookback_minutes": lookback_minutes,
            "changes": results,
            "suspects": suspects,
            "after_onset": [r for r in results if r["phase"] == "after_onset"],
        }

    @staticmethod
    def window_start(onset: datetime, lookback_minutes: int = 240) -> datetime:
        return onset - timedelta(minutes=lookback_minutes)
