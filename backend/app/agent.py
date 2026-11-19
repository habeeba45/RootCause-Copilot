"""RootCause Copilot investigation agent.

How it works
------------
1. Core evidence gathering - a planner picks the next analysis tool from what data exists and what
   has been learned so far (metrics -> onset -> logs -> changes -> dependency graph), and logs why.
2. Every observation a tool makes is written to an evidence ledger with an ID (E1, E2, ...).
3. Hypotheses are generated from that evidence and scored by deterministic evidence checks. Every
   check cites the ledger entries it relies on. Confidence is computed only from those scores.
4. Follow-up investigation - targeted tools (inspect_service, compare_change_window, search_logs)
   are chosen by an LLM planner when an API key is configured, or by the rule planner otherwise,
   until the leading hypothesis is verified or the budget runs out.
5. With an LLM, a final grounded analysis is produced; any statement citing evidence that does not
   exist is removed. Without strong evidence the result is reported as inconclusive.
Every step is emitted as an activity event and recorded in the investigation log.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import llm
from .evidence import EvidenceLedger
from .parsers import Change, DependencyMap, LogEntry, MetricPoint, iso
from .playbooks import MODE_LABELS, recommendations
from .tools import SYMPTOM_CATEGORIES, ChangeDetector, DependencyAnalyzer, LogAnalyzer, MetricsAnalyzer
from .tools.metrics_analyzer import _robust_stats, metric_kind

RESOURCE_MODE = {"memory": "memory", "db_pool": "db_connection_pool", "cpu": "cpu_saturation", "disk": "disk"}
MODE_KEYWORDS = {
    "db_connection_pool": ("pool", "connection", "jdbc", "datasource", "db."),
    "memory": ("cache", "memory", "heap", "buffer", "xmx"),
    "certificate": ("cert", "tls", "ssl"),
    "dns": ("dns", "resolver"),
    "config": ("config", "setting"),
    "cpu_saturation": ("thread", "worker", "cpu", "algorithm", "loop"),
    "timeout": ("timeout", "retry"),
    "rate_limit": ("rate", "limit", "quota"),
    "database": ("schema", "migration", "index", "query"),
}
TEMPERATURE = 3.0
UNKNOWN_SCORE = 4.0          # the "unexplained by any tested hypothesis" alternative
SUPPORTED_SCORE = 6.0
AMBIGUITY_GAP = 4.0
RULE_FOLLOWUP_BUDGET = 6
CHANGE_WINDOW_MIN = 20
MAX_LLM_HYPOTHESES = 4


@dataclass
class IncidentInputs:
    metrics: list[MetricPoint] | None = None
    logs: list[LogEntry] | None = None
    changes: list[Change] = field(default_factory=list)
    dependencies: DependencyMap = field(default_factory=DependencyMap)
    changes_supplied: bool | None = None  # None -> inferred from `changes`


def jsonable(obj):
    if isinstance(obj, datetime):
        return iso(obj)
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items() if not str(k).endswith("_dt")}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _lc(label: str) -> str:
    """Lower-case the first word only, keeping acronyms such as TLS / DNS intact."""
    return label[:1].lower() + label[1:] if label and not label[:2].isupper() else label


def _cite(ids) -> str:
    ids = [i for i in ids if i]
    return f" [{', '.join(ids)}]" if ids else ""


def _cited(detail: str, ids) -> str:
    """Attach a citation before the sentence's final full stop."""
    c = _cite(ids)
    return f"{detail[:-1]}{c}." if detail.endswith(".") else f"{detail}{c}"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


TOOL_SPECS = [
    {"name": "inspect_service",
     "description": "Focused inspection of one service: its anomalies, log signatures, dependencies and, crucially, whether "
                    "its affected callers failed AFTER it (consistent with it being the origin) or BEFORE it (it is a victim). "
                    "Adds evidence and re-scores hypotheses.",
     "parameters": {"type": "object", "properties": {
         "service": {"type": "string", "description": "Exact service name from the case file"},
         "reason": {"type": "string", "description": "Why this inspection helps discriminate between hypotheses"}},
         "required": ["service", "reason"]}},
    {"name": "compare_change_window",
     "description": f"Compare the changed service's metrics and error-log volume in the {CHANGE_WINDOW_MIN} minutes before vs after a "
                    "change. Shows whether the change had a measurable impact. Adds evidence and re-scores hypotheses.",
     "parameters": {"type": "object", "properties": {
         "change_id": {"type": "string", "description": "Change ID from the case file"},
         "reason": {"type": "string"}}, "required": ["change_id", "reason"]}},
    {"name": "search_logs",
     "description": "Case-insensitive substring search over all log lines (optionally one service). Returns match counts per "
                    "service, first/last time and samples. Use it to verify a specific claim. Adds evidence (also when nothing matches).",
     "parameters": {"type": "object", "properties": {
         "pattern": {"type": "string", "description": "Plain text to search for (not a regex), 2-200 characters"},
         "service": {"type": "string", "description": "Optional service name to restrict the search"},
         "reason": {"type": "string"}}, "required": ["pattern", "reason"]}},
    {"name": "add_hypothesis",
     "description": "Propose an additional root-cause hypothesis that the existing list misses. It will be scored by the same "
                    "deterministic evidence checks as every other hypothesis; you cannot set its score.",
     "parameters": {"type": "object", "properties": {
         "service": {"type": "string", "description": "Service believed to be the origin"},
         "change_id": {"type": "string", "description": "Optional triggering change ID"},
         "rationale": {"type": "string"},
         "evidence_ids": {"type": "array", "items": {"type": "string"}, "description": "Ledger IDs that motivate it (at least one)"}},
         "required": ["service", "rationale", "evidence_ids"]}},
    {"name": "finish_investigation",
     "description": "Finish with your grounded analysis. Every statement must cite ledger IDs such as [E3]. Statements citing "
                    "unknown IDs are removed. Do not state numbers that are not in the cited evidence. Confidence values are "
                    "computed by the system, do not invent them.",
     "parameters": {"type": "object", "properties": {
         "preferred_hypothesis_id": {"type": "string"},
         "explanation": {"type": "string", "description": "One paragraph: the causal chain, citing evidence like [E3][E7]"},
         "assessments": {"type": "array", "items": {"type": "object", "properties": {
             "hypothesis_id": {"type": "string"},
             "stance": {"type": "string", "enum": ["supports", "doubts", "refutes"]},
             "reasoning": {"type": "string"},
             "evidence_ids": {"type": "array", "items": {"type": "string"}}},
             "required": ["hypothesis_id", "stance", "reasoning", "evidence_ids"]}},
         "recommendations": {"type": "array", "items": {"type": "object", "properties": {
             "action": {"type": "string"},
             "horizon": {"type": "string", "enum": ["immediate", "short_term", "long_term"]},
             "evidence_ids": {"type": "array", "items": {"type": "string"}}},
             "required": ["action", "horizon", "evidence_ids"]}},
         "caveats": {"type": "string", "description": "Gaps or uncertainty in the evidence"}},
         "required": ["preferred_hypothesis_id", "explanation", "assessments", "recommendations"]}},
]

LLM_SYSTEM = """You are the lead investigator inside RootCause Copilot, an incident root-cause analysis agent.
Deterministic analysis tools have already examined the uploaded metrics, logs, change records and dependency map.
Their findings are in an evidence ledger; each entry has an ID like E12. A rule engine generated hypotheses and scored
each one with explicit evidence checks.

Your job: decide which follow-up tools to run to confirm or refute the leading hypotheses, optionally add a missed
hypothesis, then call finish_investigation with a grounded analysis.

Rules:
- Use only facts present in the ledger or returned by tools. Never invent services, changes, numbers or times.
- Cite ledger IDs for every claim, e.g. "the pool saturated right after the deploy [E4][E21]".
- You cannot set scores or confidence; they are computed from evidence checks.
- Prefer tools that discriminate between the top hypotheses. Stop when the leader is verified.
- Always include a "reason" when calling a tool. Be concise."""


class RCAAgent:
    def __init__(self, emit: Callable[[dict], None], pace: float = 0.35, use_llm: bool = True):
        self._emit = emit
        self.pace = pace
        self.use_llm = use_llm
        self.paused = 0.0
        self.metrics_tool = MetricsAnalyzer()
        self.log_tool = LogAnalyzer()
        self.change_tool = ChangeDetector()
        self.dep_tool = DependencyAnalyzer()

    # ================================================================== infrastructure
    def emit(self, kind: str, title: str, detail: str = "", data: dict | None = None, tool: str | None = None):
        self._emit({"type": kind, "title": title, "detail": detail, "tool": tool, "data": jsonable(data or {}),
                    "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")})
        if self.pace:  # deliberate display pacing so the live log is readable; excluded from duration_s
            time.sleep(self.pace)
            self.paused += self.pace

    def record(self, phase: str, action: str, reason: str, outcome: str, evidence: list[str] | None = None,
               planner: str = "rule", args: dict | None = None):
        self.log.append({"step": len(self.log) + 1, "phase": phase, "planner": planner, "action": action,
                         "args": args or {}, "reason": reason, "outcome": outcome, "evidence": evidence or []})

    def decide(self, action: str, reason: str, planner: str = "rule", args: dict | None = None):
        label = f"{action}({', '.join(f'{k}={v}' for k, v in (args or {}).items() if k != 'reason')})"
        self.emit("decision", f"{'LLM planner' if planner == 'llm' else 'Planner'} chose {label}", reason,
                  {"planner": planner, "action": action, "args": args or {}})

    # ================================================================== main loop
    def run(self, inputs: IncidentInputs, meta: dict) -> dict:
        self.t0 = time.time()
        self.paused = 0.0
        self.inputs, self.meta = inputs, meta
        self.ledger = EvidenceLedger()
        self.log: list[dict] = []
        self.m = self.l = self.d = None
        self.c = {"changes": [], "suspects": [], "after_onset": []}
        self.affected: dict[str, datetime] = {}
        self.traffic_only: list[str] = []
        self.onset: datetime | None = None
        self.hyps: list[dict] = []
        self.done: set[str] = set()
        self.ev: dict = defaultdict(dict)
        self.followups: dict[str, dict] = {}
        self.llm_analysis: dict | None = None
        self.llm_state = llm.status() if self.use_llm else {"enabled": False, "mode": "rule-based"}
        self.mode = self.llm_state.get("mode", "rule-based")
        self.changes_supplied = inputs.changes_supplied if inputs.changes_supplied is not None else bool(inputs.changes)

        available = {"metrics": bool(inputs.metrics), "logs": bool(inputs.logs), "changes": self.changes_supplied,
                     "dependencies": bool(inputs.dependencies.edges)}
        have = [k for k, v in available.items() if v]
        missing = [k for k, v in available.items() if not v]
        strategy = ("Estimate onset from metrics" if available["metrics"] else "Estimate onset from error bursts in logs")
        self.emit("plan", "Investigation plan created",
                  f"Evidence available: {', '.join(have)}. Missing: {', '.join(missing) or 'none'}. Strategy: {strategy}, "
                  f"then correlate {'changes, ' if available['changes'] else ''}"
                  f"{'the dependency graph, ' if available['dependencies'] else ''}generate and test hypotheses, and verify "
                  f"the leader with follow-up tools chosen by the "
                  f"{'LLM (' + str(self.llm_state.get('model')) + ')' if self.llm_state['enabled'] else 'rule'} planner.",
                  {"available": available, "llm": self.llm_state,
                   "steps": self._plan_steps(available)})
        for w in (meta.get("data_warnings") or [])[:6]:
            self.emit("warning", "Data quality note", w)
        self.record("plan", "plan", "Inspect which evidence types were supplied", f"Have {', '.join(have)}; missing {', '.join(missing) or 'none'}")

        # ---------------- phase 1: core evidence, planner-driven
        while True:
            step = self._next_core_step(available)
            if step is None:
                break
            tool, reason = step
            self.decide(tool, reason)
            getattr(self, f"_tool_{tool}")(reason)
            if tool in ("analyze_metrics", "analyze_logs") and not self._core_pending(("analyze_metrics", "analyze_logs"), available):
                self._establish_onset()
                if not self.affected:
                    self.emit("conclusion", "No significant anomalies detected",
                              "Neither metrics nor logs show a sustained deviation from baseline, so no root cause can be "
                              "supported by evidence.")
                    self.record("conclusion", "stop", "No affected services", "Inconclusive: no anomalies")
                    return self._empty_result()
        if not self.affected:  # nothing to analyse (no metrics or logs usable)
            self.emit("conclusion", "No usable telemetry", "Neither metrics nor logs were available to establish an onset.")
            return self._empty_result()
        # ---------------- phase 2: hypotheses
        self._generate()
        for h in self.hyps:
            self.emit("hypothesis", f"{h['id']}: {h['title']}", h["statement"], {"service": h["service"], "kind": h["kind"]})
        self.record("hypotheses", "generate_hypotheses", "Turn root candidates, suspicious changes, earliest, loudest and most "
                    "visible services into testable hypotheses", f"{len(self.hyps)} hypotheses: {', '.join(h['id'] for h in self.hyps)}")
        self._evaluate()
        self.emit("ranking", "Preliminary ranking (before follow-up)",
                  " | ".join(f"{h['id']} {h['score']} ({h['verdict']})" for h in self.hyps[:5]),
                  {"ranking": self._ranking_view()})

        # ---------------- phase 3: follow-up investigation
        if self.llm_state["enabled"]:
            try:
                self._llm_followups()
            except llm.LLMError as e:
                self.emit("warning", "LLM planner unavailable - switching to rule planner", str(e)[:300])
                self.record("follow-up", "llm_error", "LLM call failed", str(e)[:200], planner="llm")
                self.mode = "rule-based (LLM fallback)"
                self._rule_followups()
        else:
            self._rule_followups()

        # ---------------- phase 4: final testing and ranking
        self._evaluate()
        for h in self.hyps:
            sup = sum(1 for c in h["checks"] if c["result"] == "supports")
            con = sum(1 for c in h["checks"] if c["result"] == "contradicts")
            self.emit("test", f"Tested {h['id']}: {h['verdict'].upper()} (score {h['score']})",
                      f"{sup} supporting / {con} contradicting checks. " +
                      " | ".join(f"{'+' if c['result'] == 'supports' else '-' if c['result'] == 'contradicts' else '~'} {c['name']}"
                                 for c in h["checks"]),
                      {"id": h["id"], "checks": h["checks"]})
        self.emit("ranking", "Final ranking",
                  " | ".join(f"#{h['rank']} {h['id']} {int(round(h['confidence'] * 100))}%" for h in self.hyps[:5]),
                  {"ranking": self._ranking_view()})
        result = self._build_result()
        top = self.hyps[0]
        if result["summary"]["verdict"] == "inconclusive":
            self.emit("conclusion", "Inconclusive: no hypothesis is supported by enough evidence",
                      f"Leading hypothesis {top['id']} ({top['title']}) scored {top['score']}, below the {SUPPORTED_SCORE} "
                      "needed to be called supported. See data gaps in the report.", {"hypothesis": top["id"]})
        else:
            self.emit("conclusion", f"Probable root cause: {top['title']}",
                      f"Confidence {int(round(top['confidence'] * 100))}% relative to {len(self.hyps)} tested hypotheses. "
                      f"Supported by {len(result['summary']['evidence_ids'])} evidence items. RCA report generated.",
                      {"hypothesis": top["id"], "confidence": top["confidence"]})
        self.record("conclusion", "conclude", "Rank by evidence score", result["summary"]["title"],
                    result["summary"]["evidence_ids"])
        result["investigation_log"] = self.log
        result["summary"].update(self._timing())
        return jsonable(result)

    def _timing(self) -> dict:
        wall = time.time() - self.t0
        return {"duration_s": round(max(0.0, wall - self.paused), 2), "wall_clock_s": round(wall, 1),
                "display_pacing_s": round(self.paused, 1)}

    @staticmethod
    def _plan_steps(available: dict) -> list[str]:
        steps = []
        if available["metrics"]:
            steps.append("Detect metric anomalies and estimate the incident onset")
        if available["logs"]:
            steps.append("Cluster and classify log errors" + ("" if available["metrics"] else " and estimate onset from error bursts"))
        if available["changes"]:
            steps.append("Correlate change records with the onset")
        if available["dependencies"]:
            steps.append("Localise the fault in the dependency graph")
        steps += ["Generate hypotheses", "Score each hypothesis with evidence checks",
                  "Run follow-up tools to verify the leader", "Rank causes and write the RCA report"]
        return steps

    # ================================================================== phase 1: planner + core tools
    def _core_pending(self, tools, available) -> bool:
        need = {"analyze_metrics": available["metrics"], "analyze_logs": available["logs"]}
        return any(need.get(t) and t not in self.done for t in tools)

    def _next_core_step(self, available: dict) -> tuple[str, str] | None:
        if available["metrics"] and "analyze_metrics" not in self.done:
            return "analyze_metrics", ("Metrics give the most precise timing; detect sustained deviations first to locate "
                                       "the onset and the affected services.")
        if available["logs"] and "analyze_logs" not in self.done:
            if self.m and self.m["anomalies"]:
                first = self.m["anomalies"][0]
                why = (f"Metrics show the first deviation on {first['service']} at {first['onset'][11:16]}; classify log "
                       "errors around it to learn what failed and which services blame which.")
            elif self.m:
                why = "Metrics showed no sustained anomaly; logs may still reveal error bursts."
            else:
                why = "No metrics were supplied, so the onset must be estimated from error bursts in the logs."
            return "analyze_logs", why
        if not self.affected:
            return None
        if "detect_changes" not in self.done:
            if not available["changes"]:
                self.done.add("detect_changes")
                self.emit("decision", "Planner skipped change correlation",
                          "No change records were supplied, so change-triggered hypotheses cannot be tested.", {"planner": "rule"})
                self.record("core", "skip:detect_changes", "No change file", "Change hypotheses not testable")
            else:
                return "detect_changes", (f"{len(self.affected)} services degraded from {iso(self.onset)}; check which changes "
                                          "landed shortly before that.")
        if "analyze_dependencies" not in self.done:
            if not available["dependencies"]:
                self.done.add("analyze_dependencies")
                self.d = self.dep_tool.run(DependencyMap(), self.affected, self._all_services())
                self.emit("decision", "Planner skipped dependency analysis",
                          "No dependency map was supplied; localisation will rely on onset order and cross-service log references.",
                          {"planner": "rule"})
                self.record("core", "skip:analyze_dependencies", "No dependency map", "Graph checks neutral")
            else:
                return "analyze_dependencies", ("Several services are affected; walk the dependency graph to separate the "
                                                "origin (deepest failing service) from victims.")
        return None

    def _all_services(self) -> set[str]:
        s = set(self.m["services"]) if self.m else set()
        if self.l:
            s |= set(self.l["level_counts"])
        s |= set(self.inputs.dependencies.services)
        s.discard("unknown")
        return s

    def _tool_analyze_metrics(self, reason: str):
        self.emit("tool_call", "Calling metrics_analyzer", f"{len(self.inputs.metrics):,} data points", tool="metrics_analyzer")
        self.m = m = self.metrics_tool.run(self.inputs.metrics)
        self.done.add("analyze_metrics")
        ids = []
        cov = self.ledger.add("coverage", "metrics_analyzer",
                              f"Examined {m['series_count']} metric series across {len(m['services'])} services "
                              f"({m['window']['start']} to {m['window']['end']}); baseline = data up to {m['window']['baseline_end']}.",
                              data={"services": m["services"]})
        self.ev["coverage"]["metrics"] = cov
        for a in m["anomalies"]:
            eid = self.ledger.add("metric_anomaly", "metrics_analyzer",
                                  f"{a['service']} {a['metric']} {'rose' if a['direction'] == 'up' else 'fell'} from baseline "
                                  f"{a['baseline']} to {a['peak']} (z={a['z_score']}, {a['pattern']}) starting {a['onset'][11:16]} UTC.",
                                  a["service"], a["onset"], {k: a[k] for k in ("metric", "baseline", "peak", "z_score", "pattern", "kind")})
            a["evidence_id"] = eid
            self.ev["anom"][(a["service"], a["metric"])] = eid
            self.ev["anom_svc"].setdefault(a["service"], []).append(eid)
            ids.append(eid)
        top = m["anomalies"][:5]
        self.emit("tool_result", f"Found {len(m['anomalies'])} sustained anomalies across {len(m['by_service'])} services",
                  "; ".join(f"{a['evidence_id']} {a['service']}.{a['metric']} z={a['z_score']} at {a['onset'][11:16]}" for a in top)
                  or "No sustained deviations.", {"anomalies": top, "evidence": ids[:10]}, tool="metrics_analyzer")
        self.record("core", "analyze_metrics", reason, f"{len(m['anomalies'])} anomalies", [cov] + ids)

    def _tool_analyze_logs(self, reason: str):
        self.emit("tool_call", "Calling log_analyzer", f"{len(self.inputs.logs):,} log lines", tool="log_analyzer")
        prelim = self._metric_affected()
        known = (set(self.m["services"]) if self.m else set()) | set(self.inputs.dependencies.services)
        self.l = lr = self.log_tool.run(self.inputs.logs, min(prelim.values()) if prelim else None, known)
        self.done.add("analyze_logs")
        cov = self.ledger.add("coverage", "log_analyzer",
                              f"Parsed {lr['total_lines']:,} log lines from {len(lr['level_counts'])} sources into "
                              f"{len(lr['clusters'])} message templates ({lr['window']['start']} to {lr['window']['end']}).")
        self.ev["coverage"]["logs"] = cov
        ids = [cov]
        for c in lr["new_error_clusters"]:
            eid = self.ledger.add("log_signature", "log_analyzer",
                                  f"{c['service']}: {c['count']}x {c['level']} \"{c['sample'][:140]}\" ({c['category_label']}), "
                                  f"first seen {(c['first_seen'] or '')[11:19]} UTC.", c["service"], c["first_seen"],
                                  {"category": c["category"], "count": c["count"], "level": c["level"]})
            c["evidence_id"] = eid
            self.ev["cluster_svc"].setdefault(c["service"], []).append((c["category"], eid))
            ids.append(eid)
        for r in lr["references"]:
            eid = self.ledger.add("log_reference", "log_analyzer",
                                  f"{r['count']} new error/warn lines in {r['from']} mention {r['to']}.", r["from"],
                                  data={"from": r["from"], "to": r["to"], "count": r["count"]})
            self.ev["ref"][(r["from"], r["to"])] = eid
            ids.append(eid)
        for p in lr["precursors"]:
            eid = self.ledger.add("log_precursor", "log_analyzer",
                                  f"Before the incident, {p['service']} logged \"{p['sample'][:140]}\" ({p['category_label']}) at "
                                  f"{(p['first_seen'] or '')[11:19]} UTC.", p["service"], p["first_seen"], {"category": p["category"]})
            self.ev["prec_svc"].setdefault(p["service"], []).append((p["category"], eid))
            ids.append(eid)
        for v in lr["versions_seen"]:
            key = (v["service"], v["version"])
            if key not in self.ev["marker"]:
                self.ev["marker"][key] = self.ledger.add("deployment_marker", "log_analyzer",
                                                         f"{v['service']} logged version {v['version']} at {(v['timestamp'] or '')[11:19]} UTC: "
                                                         f"\"{v['message'][:120]}\"", v["service"], v["timestamp"])
                ids.append(self.ev["marker"][key])
        new = lr["new_error_clusters"]
        self.emit("tool_result", f"{len(lr['clusters'])} message templates, {len(new)} new error signatures",
                  "; ".join(f"{c['evidence_id']} [{c['service']}] {c['category_label']} x{c['count']}" for c in new[:5])
                  or "No new error bursts.",
                  {"new_error_clusters": [{k: c[k] for k in ("evidence_id", "service", "category_label", "count", "first_seen", "sample")}
                                          for c in new[:6]]}, tool="log_analyzer")
        if lr["references"]:
            r = lr["references"][0]
            self.emit("observation", "Cross-service error references detected",
                      f"Errors in {r['from']} mention {r['to']} {r['count']} times{_cite([self.ev['ref'][(r['from'], r['to'])]])}, "
                      f"suggesting failures flow from {r['to']} towards {r['from']}.", {"references": lr["references"][:5]})
        self.record("core", "analyze_logs", reason, f"{len(new)} new error signatures, {len(lr['references'])} cross-service references", ids)

    def _metric_affected(self) -> dict[str, datetime]:
        out: dict[str, datetime] = {}
        if not self.m:
            return out
        for svc in self.m["by_service"]:
            non_traffic = [a for a in self.m["anomalies"] if a["service"] == svc and a["kind"] != "traffic"]
            if non_traffic:
                out[svc] = min(a["onset_dt"] for a in non_traffic)
        return out

    def _establish_onset(self):
        self.affected = self._metric_affected()
        if self.m:
            self.traffic_only = [s for s in self.m["by_service"] if s not in self.affected]
        if self.l:
            for s, t in self.l["service_onsets"].items():
                self.affected[s] = min(self.affected.get(s, t), t)
        self.traffic_only = [s for s in self.traffic_only if s not in self.affected]
        if not self.affected:
            return
        self.onset = min(self.affected.values())
        first = min(self.affected, key=lambda s: self.affected[s])
        basis = (self.ev["anom_svc"].get(first, [])[:1] + [e for _, e in self.ev["cluster_svc"].get(first, [])][:1])
        self.ev["onset"] = self.ledger.add("onset", "agent",
                                           f"Incident onset {iso(self.onset)}: first deviation on {first}; "
                                           f"{len(self.affected)} service{'s' if len(self.affected) != 1 else ''} affected "
                                           f"({', '.join(sorted(self.affected, key=lambda s: self.affected[s]))}).",
                                           first, self.onset, {"affected": {s: iso(t) for s, t in self.affected.items()}, "basis": basis})
        for s in self.traffic_only:
            self.ledger.add("traffic_shift", "agent", f"{s} shows only a traffic-volume change, treated as a consequence, not a fault.", s)
        self.emit("observation", f"Incident onset estimated at {iso(self.onset)}",
                  f"First deviation on {first}{_cite(basis)}. {len(self.affected)} service{'s' if len(self.affected) != 1 else ''} affected"
                  + (f"; traffic-only shifts on {', '.join(self.traffic_only)} treated as secondary." if self.traffic_only else "."),
                  {"onset": self.onset, "affected": {s: iso(t) for s, t in sorted(self.affected.items(), key=lambda x: x[1])}})

    def _tool_detect_changes(self, reason: str):
        self.emit("tool_call", "Calling change_detector", f"{len(self.inputs.changes)} change records", tool="change_detector")
        self.c = cres = self.change_tool.run(self.inputs.changes, self.onset)
        self.done.add("detect_changes")
        ids = []
        for ch in cres["changes"]:
            when = ("before onset" if ch["phase"] == "before_onset" else "after onset" if ch["phase"] == "after_onset"
                    else "outside the look-back window")
            delta = abs(ch["minutes_before_onset"]) if ch["minutes_before_onset"] is not None else None
            eid = self.ledger.add("change", "change_detector",
                                  f"{ch['id']} ({ch['type'].replace('_', ' ')}) on {ch['service']} at {ch['timestamp'][11:16]} UTC, "
                                  f"{f'{delta:.0f} min ' if delta is not None else ''}{when}: \"{ch['description'][:140]}\""
                                  + (" - looks like a mitigation." if ch["likely_mitigation"] else ""),
                                  ch["service"], ch["timestamp"], {"phase": ch["phase"], "type": ch["type"]})
            ch["evidence_id"] = eid
            self.ev["change"][ch["id"]] = eid
            ids.append(eid)
        sus = cres["suspects"]
        self.emit("tool_result", f"{len(sus)} changes precede the onset, {len(cres['after_onset'])} after",
                  "; ".join(f"{c['evidence_id']} {c['id']} {c['service']} {c['type']} ({c['minutes_before_onset']} min before)" for c in sus[:4])
                  or "No changes landed inside the look-back window.", {"suspects": sus[:5]}, tool="change_detector")
        self.record("core", "detect_changes", reason, f"{len(sus)} candidate changes before onset", ids)

    def _tool_analyze_dependencies(self, reason: str):
        dm = self.inputs.dependencies
        self.emit("tool_call", "Calling dependency_analyzer", f"{len(dm.services)} services, {len(dm.edges)} edges", tool="dependency_analyzer")
        self.d = d = self.dep_tool.run(dm, self.affected, self._all_services())
        self.done.add("analyze_dependencies")
        ids = []
        for r in d["root_candidates"]:
            node = next(x for x in d["nodes"] if x["id"] == r["service"])
            eid = self.ledger.add("dependency_root", "dependency_analyzer",
                                  f"{r['service']} is failing but none of its dependencies ({', '.join(node['callees']) or 'none'}) are; "
                                  f"its callers cover {int(r['coverage'] * 100)}% of affected services.",
                                  r["service"], r["onset"], {"coverage": r["coverage"], "callees": node["callees"]})
            self.ev["root"][r["service"]] = eid
            ids.append(eid)
        for n in d["nodes"]:
            if n["status"] == "impacted":
                bad = [c for c in n["callees"] if c in self.affected]
                eid = self.ledger.add("dependency_victim", "dependency_analyzer",
                                      f"{n['id']} depends on failing service(s) {', '.join(bad)} that failed at the same time or earlier.",
                                      n["id"], n["onset"], {"failing_callees": bad})
                self.ev["victim"][n["id"]] = eid
                ids.append(eid)
        for p in d["propagation_paths"]:
            eid = self.ledger.add("propagation", "dependency_analyzer", f"Failure propagation path: {' -> '.join(p)}.", p[0])
            self.ev["path"][p[0]] = eid
            ids.append(eid)
        roots = d["root_candidates"]
        self.emit("tool_result", f"{len(roots)} root candidate(s) in the dependency graph",
                  "; ".join(f"{self.ev['root'][r['service']]} {r['service']} (explains {int(r['coverage'] * 100)}% of affected)" for r in roots[:3]),
                  {"root_candidates": roots, "paths": d["propagation_paths"]}, tool="dependency_analyzer")
        if d["propagation_paths"]:
            self.emit("observation", "Failure propagation path", " -> ".join(d["propagation_paths"][0]),
                      {"path": d["propagation_paths"][0]})
        self.record("core", "analyze_dependencies", reason, f"{len(roots)} root candidates", ids)

    # ================================================================== phase 2: hypotheses
    def failure_mode(self, svc: str) -> str:
        cats = (self.l or {}).get("dominant_categories", {}).get(svc, [])
        specific = [c["category"] for c in cats if c["category"] not in SYMPTOM_CATEGORIES]
        if specific:
            return specific[0]
        kinds = [a["kind"] for a in (self.m["anomalies"] if self.m else []) if a["service"] == svc]
        for k in kinds:
            if k in RESOURCE_MODE:
                return RESOURCE_MODE[k]
        if cats:
            return cats[0]["category"]
        if "latency" in kinds:
            return "performance_degradation"
        if "errors" in kinds:
            return "error_spike"
        return "unknown"

    def _new_hypothesis(self, kind: str, svc: str, change: dict | None, source: str = "rules", rationale: str | None = None) -> dict:
        mode = self.failure_mode(svc) if svc in self.affected else "unknown"
        label = MODE_LABELS.get(mode, mode)
        hid = f"H{len(self.hyps) + 1}"
        if change:
            what = change["type"].replace("_", " ") + (f" {change['version']}" if change.get("version") else "")
            title = f"{change['id']} {what} on {svc}" + (f" caused {_lc(label)}" if svc in self.affected else " caused the incident")
            mins = change.get("minutes_before_onset")
            statement = (f"The {change['type'].replace('_', ' ')} '{change['description']}'"
                         + (f" ({mins} min before onset)" if mins is not None else "")
                         + f" introduced a fault in {svc} that propagated to dependants.")
        else:
            title = f"{label} in {svc}"
            statement = f"{svc} failed on its own ({_lc(label)}) without a triggering change, and the failure cascaded to its callers."
        h = {"id": hid, "kind": kind, "service": svc, "change": change, "failure_mode": mode, "title": title,
             "statement": statement, "source": source}
        if rationale:
            h["rationale"] = rationale
        self.hyps.append(h)
        return h

    def _generate(self):
        affected, d, l = self.affected, self.d, self.l
        candidates: list[str] = [r["service"] for r in d["root_candidates"][:3]]
        by_onset = sorted(affected, key=lambda s: affected[s])
        if by_onset[0] not in candidates:
            candidates.append(by_onset[0])
        if l and l["new_error_clusters"]:  # the noisiest service is what a human would blame first
            loud = max(affected, key=lambda s: sum(x["count"] for x in l["new_error_clusters"] if x["service"] == s))
            if loud not in candidates:
                candidates.append(loud)
        if d["has_map"]:  # the most customer-visible service is another tempting suspect
            depth = {x["id"]: x["depth"] for x in d["nodes"]}
            edge = max(affected, key=lambda s: (depth.get(s, 0), -affected[s].timestamp()))
            if edge not in candidates:
                candidates.append(edge)
        for ch in self.c["suspects"][:5]:
            self._new_hypothesis("change", ch["service"], ch)
        for svc in candidates:
            self._new_hypothesis("service_fault", svc, None)

    # ================================================================== evidence checks
    def _test(self, h: dict) -> None:
        svc, mode = h["service"], h["failure_mode"]
        m, l, d, affected, onset = self.m, self.l, self.d, self.affected, self.onset
        checks: list[dict] = []
        ev = self.ev

        def add(name, result, weight, detail, evidence):
            checks.append({"name": name, "result": result, "weight": round(weight, 2), "detail": detail,
                           "evidence": [e for e in evidence if e]})

        is_aff = svc in affected
        s_onset = affected.get(svc)
        anom_ids = ev["anom_svc"].get(svc, [])
        clus = ev["cluster_svc"].get(svc, [])
        if is_aff:
            mets = sorted({a["metric"] for a in (m["anomalies"] if m else []) if a["service"] == svc})
            errs = sum(x["count"] for x in (l["new_error_clusters"] if l else []) if x["service"] == svc)
            add("Service shows anomalies", "supports", 2.0,
                f"{svc} deviates from baseline (metrics: {', '.join(mets) or 'none'}; {errs} new error/warn lines).",
                anom_ids[:3] + [e for _, e in clus][:2])
        else:
            add("Service shows anomalies", "contradicts", -4.0,
                f"{svc} has no metric anomalies or new error bursts during the incident - it is not in the failure path.",
                [ev["coverage"].get("metrics"), ev["coverage"].get("logs")])

        ch = h.get("change")
        if ch:
            ce = ev["change"].get(ch["id"])
            ref = s_onset or onset
            delta = (ref - _dt(ch["timestamp"])).total_seconds() / 60
            if 0 <= delta <= 90:
                add("Change precedes failure", "supports", 1 + 3 * math.exp(-delta / 40),
                    f"{ch['id']} landed {delta:.0f} min before {svc if is_aff else 'the incident'} started failing.",
                    [ce, (anom_ids or [ev.get("onset")])[0]])
            elif 90 < delta <= 240:
                add("Change precedes failure", "neutral", 0.3, f"{ch['id']} landed {delta:.0f} min earlier - weak temporal link.", [ce])
            else:
                add("Change precedes failure", "contradicts", -3.0, f"{ch['id']} does not precede the failure ({delta:.0f} min).", [ce])
            hit = [w for w in MODE_KEYWORDS.get(mode, ()) if w in ch["description"].lower()]
            if is_aff and hit:
                add("Change content matches failure mode", "supports", 1.5,
                    f"Change description mentions '{hit[0]}', consistent with {_lc(MODE_LABELS.get(mode, mode))}.",
                    [ce] + [e for c, e in clus if c == mode][:1])
            if ch.get("version") and (svc, ch["version"]) in ev["marker"]:
                add("Rollout visible in logs", "supports", 1.0, f"{svc} logged version {ch['version']} starting.",
                    [ev["marker"][(svc, ch["version"])]])
            imp = self.followups.get(f"compare_change_window:{ch['id']}")
            if imp:
                if not imp["testable"]:
                    add("Measurable impact after the change", "neutral", 0.0, imp["summary"], [imp["evidence_id"]])
                elif imp["significant"]:
                    add("Measurable impact after the change", "supports", 1.5, imp["summary"], [imp["evidence_id"]])
                else:
                    add("Measurable impact after the change", "contradicts", -1.5, imp["summary"], [imp["evidence_id"]])

        if is_aff:
            diff = (s_onset - onset).total_seconds() / 60
            first_ev = (anom_ids[:1] or [e for _, e in clus][:1]) + [ev.get("onset")]
            if diff <= 0.5:
                add("Earliest failing service", "supports", 2.0, f"{svc} is among the first services to fail ({iso(s_onset)}).", first_ev)
            elif diff <= 5:
                add("Earliest failing service", "neutral", 0.5, f"{svc} started failing {diff:.0f} min after the first signal.", first_ev)
            else:
                add("Earliest failing service", "contradicts", -1.5, f"{svc} started failing {diff:.0f} min after onset - likely a victim.", first_ev)

        if d["has_map"] and is_aff:
            node = next((x for x in d["nodes"] if x["id"] == svc), None)
            if node and node["status"] == "root_candidate":
                add("Deepest affected node in dependency graph", "supports", 2.5,
                    f"None of {svc}'s dependencies ({', '.join(node['callees']) or 'none'}) are failing.", [ev["root"].get(svc)])
            elif node:
                bad = [c for c in node["callees"] if c in affected]
                add("Deepest affected node in dependency graph", "contradicts", -2.5,
                    f"{svc} depends on failing service(s) {', '.join(bad)} that failed at the same time or earlier.",
                    [ev["victim"].get(svc)] + [(ev["anom_svc"].get(b) or [None])[0] for b in bad[:2]])
            up = set(d["upstream_of"].get(svc, []))
            covered = (len(up & set(affected)) + 1) / len(affected)
            add("Explains blast radius", "supports" if covered >= 0.6 else "neutral", 2.5 * covered - 0.5,
                f"A fault in {svc} explains {int(covered * 100)}% of affected services via its callers.",
                [ev["root"].get(svc), ev["path"].get(svc), ev.get("onset")])
        elif not d["has_map"]:
            add("Deepest affected node in dependency graph", "neutral", 0.0, "No dependency map supplied.", [])

        if l and is_aff:
            cats = l["dominant_categories"].get(svc, [])
            specific = [c for c in cats if c["category"] not in SYMPTOM_CATEGORIES]
            if specific:
                add("Local failure signature in logs", "supports", 1.5,
                    f"{svc} logs show {_lc(specific[0]['label'])} errors (not just upstream timeouts).",
                    [e for c, e in clus if c == specific[0]["category"]][:3])
            elif cats:
                add("Local failure signature in logs", "contradicts", -1.0,
                    f"{svc} errors are symptoms ({_lc(cats[0]['label'])}), pointing at another service.", [e for _, e in clus][:3])
            refs = [r for r in l["references"] if r["to"] == svc]
            if refs:
                add("Blamed by other services' errors", "supports", 1.5,
                    "; ".join(f"{r['from']} errors reference {svc} x{r['count']}" for r in refs[:3]),
                    [ev["ref"].get((r["from"], svc)) for r in refs[:3]])
            pre = [e for c, e in ev["prec_svc"].get(svc, []) if c == mode]
            if pre:
                add("Early warning signals", "supports", 1.0, f"{svc} logged a {_lc(MODE_LABELS.get(mode, mode))} warning before the onset.", pre[:2])

        if m and is_aff:
            res = [a for a in m["anomalies"] if a["service"] == svc and a["kind"] in RESOURCE_MODE]
            if res:
                a = res[0]
                add("Resource saturation", "supports", 1.0,
                    f"{a['metric']} moved from {a['baseline']} to {a['peak']} (z={a['z_score']}, {a['pattern']}).", [a.get("evidence_id")])

        lag = self.followups.get(f"inspect_service:{svc}")
        if lag and is_aff:
            add("Callers failed after it", {"supports": "supports", "contradicts": "contradicts"}.get(lag["result"], "neutral"),
                {"supports": 1.0, "contradicts": -1.5}.get(lag["result"], 0.0), lag["summary"], [lag["evidence_id"]])

        h["checks"] = checks
        h["score"] = round(sum(c["weight"] for c in checks), 2)
        h["evidence"] = sorted({e for c in checks for e in c["evidence"]}, key=lambda x: int(x[1:]))

    def _evaluate(self):
        for h in self.hyps:
            h.pop("related_to", None)
            self._test(h)
        changed = {h["service"]: h for h in self.hyps if h["kind"] == "change" and h["score"] >= SUPPORTED_SCORE}
        for h in self.hyps:
            if h["kind"] == "service_fault" and h["service"] in changed:
                other = changed[h["service"]]
                h["checks"].append({"name": "Better explained by a change", "result": "contradicts", "weight": -2.0,
                                    "detail": f"{other['id']} explains the same failure and also its timing.",
                                    "evidence": [other["change"].get("evidence_id") or self.ev["change"].get(other["change"]["id"])]})
                h["score"] = round(h["score"] - 2.0, 2)
                h["related_to"] = other["id"]
        for h in self.hyps:
            h["verdict"] = "supported" if h["score"] >= SUPPORTED_SCORE else ("plausible" if h["score"] >= 2 else "refuted")
            if h.get("related_to") and h["verdict"] == "supported":
                h["verdict"] = "contributing"
        exps = [math.exp(h["score"] / TEMPERATURE) for h in self.hyps]
        z = sum(exps) + math.exp(UNKNOWN_SCORE / TEMPERATURE)
        for h, e in zip(self.hyps, exps):
            h["confidence"] = round(e / z, 3)
        self.unexplained = round(math.exp(UNKNOWN_SCORE / TEMPERATURE) / z, 3)
        self.hyps.sort(key=lambda h: (-h["score"], int(h["id"][1:])))
        for i, h in enumerate(self.hyps, 1):
            h["rank"] = i

    def _ranking_view(self) -> list[dict]:
        return [{"id": h["id"], "title": h["title"], "score": h["score"], "verdict": h["verdict"], "confidence": h["confidence"]}
                for h in self.hyps]

    def _ambiguous(self) -> bool:
        top = self.hyps[0]
        if top["verdict"] != "supported":
            return True
        rivals = [h for h in self.hyps[1:] if not h.get("related_to")]
        return bool(rivals) and top["score"] - rivals[0]["score"] < AMBIGUITY_GAP

    # ================================================================== follow-up tools
    def _tool_compare_change_window(self, change_id: str) -> dict:
        key = f"compare_change_window:{change_id}"
        if key in self.followups:
            return self.followups[key]
        ch = next((c for c in self.c["changes"] if c["id"] == change_id), None)
        if ch is None:
            raise ValueError(f"Unknown change_id {change_id}")
        t, svc, W = _dt(ch["timestamp"]), ch["service"], timedelta(minutes=CHANGE_WINDOW_MIN)
        self.emit("tool_call", f"Calling compare_change_window({change_id})",
                  f"{svc}: {CHANGE_WINDOW_MIN} min before vs after {ch['timestamp'][11:16]} UTC", tool="compare_change_window")
        shifts, compared = [], 0
        series = defaultdict(list)
        for p in self.inputs.metrics or []:
            if p.service == svc and t - W <= p.ts <= t + W:
                series[p.metric].append((p.ts, p.value))
        for metric, pts in series.items():
            before = [v for ts, v in pts if ts < t]
            after = [v for ts, v in pts if ts > t]
            if len(before) < 3 or len(after) < 3:
                continue
            compared += 1
            med_b, sigma = _robust_stats(before, metric)
            med_a = statistics.median(after)
            z = (med_a - med_b) / sigma
            if abs(z) >= 4:
                shifts.append({"metric": metric, "before": round(med_b, 2), "after": round(med_a, 2), "z": round(z, 1),
                               "traffic": metric_kind(metric) == "traffic"})
        eb = ea = 0
        log_testable = False
        for e in self.inputs.logs or []:
            if e.service == svc and e.ts and e.level in ("ERROR", "FATAL"):
                if t - W <= e.ts < t:
                    eb += 1
                elif t < e.ts <= t + W:
                    ea += 1
        if self.inputs.logs and any(e.service == svc and e.ts and t - W <= e.ts <= t + W for e in self.inputs.logs):
            log_testable = True
        real = [s for s in shifts if not s["traffic"]]
        error_jump = log_testable and ea >= max(5, 3 * (eb + 1))
        testable = compared > 0 or log_testable
        significant = bool(real) or error_jump
        if not testable:
            summary = f"No metrics or logs for {svc} around {change_id}; its impact cannot be measured."
        elif significant:
            parts = [f"{s['metric']} {s['before']} -> {s['after']} (z={s['z']})" for s in real[:3]]
            if error_jump:
                parts.append(f"error lines {eb} -> {ea}")
            summary = f"Within {CHANGE_WINDOW_MIN} min after {change_id}, {svc} shifted: {'; '.join(parts)}."
        else:
            summary = (f"No measurable shift on {svc} within {CHANGE_WINDOW_MIN} min after {change_id} "
                       f"({compared} metrics compared; error lines {eb} -> {ea}).")
        eid = self.ledger.add("change_impact", "compare_change_window", summary, svc, ch["timestamp"],
                              {"shifts": shifts, "errors_before": eb, "errors_after": ea, "metrics_compared": compared})
        res = {"testable": testable, "significant": significant, "summary": summary, "evidence_id": eid, "shifts": shifts,
               "errors_before": eb, "errors_after": ea}
        self.followups[key] = res
        self.emit("tool_result", ("Measurable impact" if significant else "No measurable impact" if testable else "Not testable")
                  + f" after {change_id}", f"{eid}: {summary}", {"result": res}, tool="compare_change_window")
        return res

    def _tool_inspect_service(self, svc: str) -> dict:
        key = f"inspect_service:{svc}"
        if key in self.followups:
            return self.followups[key]
        if svc not in self._all_services():
            raise ValueError(f"Unknown service {svc}")
        self.emit("tool_call", f"Calling inspect_service({svc})", "Checks whether its affected callers failed after it",
                  tool="inspect_service")
        callers = []
        if self.d and self.d["has_map"]:
            node = next((n for n in self.d["nodes"] if n["id"] == svc), None)
            callers = [c for c in (node["callers"] if node else []) if c in self.affected]
            basis = "dependency map"
        else:
            callers = sorted({r["from"] for r in (self.l["references"] if self.l else []) if r["to"] == svc and r["from"] in self.affected})
            basis = "log references"
        s_on = self.affected.get(svc)
        if s_on is None or not callers:
            result = "neutral"
            summary = (f"{svc} is not affected." if s_on is None else
                       f"{svc} has no affected callers (by {basis}) to compare onset times with.")
            lags = []
        else:
            lags = [(c, round((self.affected[c] - s_on).total_seconds() / 60, 1)) for c in callers]
            earlier = [(c, lag) for c, lag in lags if lag < -1]
            if earlier:
                result = "contradicts"
                summary = (f"Caller(s) {', '.join(f'{c} ({-lag:g} min earlier)' for c, lag in earlier)} failed before {svc}, "
                           "so it is unlikely to be the origin.")
            else:
                result = "supports"
                summary = (f"All {len(lags)} affected caller(s) of {svc} failed at the same time or after it "
                           f"({', '.join(f'{c} +{lag:g} min' for c, lag in lags)}), consistent with it being the origin.")
        eid = self.ledger.add("caller_lag", "inspect_service", summary, svc, s_on, {"lags": lags, "basis": basis})
        res = {"result": result, "summary": summary, "evidence_id": eid, "lags": lags,
               "profile": {"anomalies": self.ev["anom_svc"].get(svc, []), "log_signatures": [e for _, e in self.ev["cluster_svc"].get(svc, [])],
                           "failure_mode": self.failure_mode(svc)}}
        self.followups[key] = res
        self.emit("tool_result", f"inspect_service({svc}): {result}", f"{eid}: {summary}", {"result": res}, tool="inspect_service")
        return res

    def _tool_search_logs(self, pattern: str, service: str | None = None) -> dict:
        pattern = (pattern or "").strip()
        if not 2 <= len(pattern) <= 200:
            raise ValueError("pattern must be 2-200 characters")
        if not self.inputs.logs:
            raise ValueError("No logs were supplied")
        if service and service not in self._all_services():
            raise ValueError(f"Unknown service {service}")
        self.emit("tool_call", f"Calling search_logs('{pattern[:60]}'{', ' + service if service else ''})", "", tool="search_logs")
        p = pattern.lower()
        hits = [e for e in self.inputs.logs if (not service or e.service == service) and p in e.message.lower()]
        by_svc = defaultdict(int)
        for e in hits:
            by_svc[e.service] += 1
        timed = sorted(e.ts for e in hits if e.ts)
        if hits:
            summary = (f"Log search '{pattern}'{' in ' + service if service else ''}: {len(hits)} matches "
                       f"({', '.join(f'{s} {n}' for s, n in sorted(by_svc.items(), key=lambda x: -x[1])[:4])}), "
                       f"first {iso(timed[0]) if timed else 'n/a'}, last {iso(timed[-1]) if timed else 'n/a'}.")
        else:
            summary = f"Log search '{pattern}'{' in ' + service if service else ''}: no matching lines."
        eid = self.ledger.add("log_search", "search_logs", summary, service, timed[0] if timed else None,
                              {"pattern": pattern, "matches": len(hits), "by_service": dict(by_svc)})
        res = {"matches": len(hits), "by_service": dict(by_svc), "first": iso(timed[0]) if timed else None,
               "last": iso(timed[-1]) if timed else None, "samples": [f"[{e.service}] {e.message[:160]}" for e in hits[:3]],
               "summary": summary, "evidence_id": eid}
        self.emit("tool_result", f"search_logs: {len(hits)} matches", f"{eid}: {summary}", tool="search_logs")
        return res

    # ================================================================== phase 3a: rule planner
    def _rule_followups(self):
        for _ in range(RULE_FOLLOWUP_BUDGET):
            amb = self._ambiguous()
            targets = [self.hyps[0]]
            if amb:
                targets += [h for h in self.hyps[1:] if not h.get("related_to")][:1]
            action = None
            for h in targets:
                why = ("Verify the leading hypothesis with targeted evidence before concluding" if h is self.hyps[0] else
                       f"The top hypotheses are separated by less than {AMBIGUITY_GAP:g} points; gather discriminating evidence")
                if h.get("change") and f"compare_change_window:{h['change']['id']}" not in self.followups and self.c["changes"]:
                    action = ("compare_change_window", {"change_id": h["change"]["id"]}, f"{why} ({h['id']}): did {h['change']['id']} have a measurable impact?")
                    break
                if h["service"] in self.affected and f"inspect_service:{h['service']}" not in self.followups:
                    action = ("inspect_service", {"service": h["service"]}, f"{why} ({h['id']}): did {h['service']}'s callers fail after it?")
                    break
            if action is None:
                break
            self._run_followup(*action, planner="rule")
        top = self.hyps[0]
        rivals = [h for h in self.hyps[1:] if not h.get("related_to")]
        gap = top["score"] - rivals[0]["score"] if rivals else None
        msg = (f"{top['id']} leads by {gap:.1f} points with verified evidence." if gap is not None and not self._ambiguous()
               else f"{top['id']} remains the leader but the evidence does not clearly separate it from alternatives." if self._ambiguous()
               else f"{top['id']} is the only viable hypothesis.")
        self.emit("decision", "Planner stopped follow-up investigation", msg, {"planner": "rule"})
        self.record("follow-up", "stop", "Leader verified or no discriminating tool left", msg)

    def _run_followup(self, tool: str, args: dict, reason: str, planner: str) -> dict:
        self.decide(tool, reason, planner, args)
        before = {h["id"]: h["score"] for h in self.hyps}
        if tool == "compare_change_window":
            res = self._tool_compare_change_window(args["change_id"])
        elif tool == "inspect_service":
            res = self._tool_inspect_service(args["service"])
        elif tool == "search_logs":
            res = self._tool_search_logs(args.get("pattern", ""), args.get("service") or None)
        else:
            raise ValueError(f"Unknown tool {tool}")
        self._evaluate()
        moved = [f"{h['id']} {before.get(h['id'])} -> {h['score']}" for h in self.hyps if before.get(h["id"]) != h["score"]]
        if moved:
            self.emit("observation", "Scores updated with new evidence", "; ".join(moved), {"ranking": self._ranking_view()})
        self.record("follow-up", tool, reason, res["summary"], [res["evidence_id"]], planner, {k: v for k, v in args.items() if k != "reason"})
        return res

    # ================================================================== phase 3b: LLM planner
    def _case_file(self) -> dict:
        return jsonable({
            "onset": self.onset, "affected_services": {s: iso(t) for s, t in sorted(self.affected.items(), key=lambda x: x[1])},
            "all_services": sorted(self._all_services()),
            "changes": [{"id": c["id"], "service": c["service"], "type": c["type"], "time": c["timestamp"], "phase": c["phase"]}
                        for c in self.c["changes"]],
            "hypotheses": [{"id": h["id"], "title": h["title"], "score": h["score"], "verdict": h["verdict"],
                            "checks": [{"name": c["name"], "weight": c["weight"], "evidence": c["evidence"]} for c in h["checks"]]}
                           for h in self.hyps],
            "evidence_ledger": self.ledger.compact(),
            "followups_done": sorted(self.followups),
            "logs_available": bool(self.inputs.logs), "metrics_available": bool(self.inputs.metrics),
        })

    def _llm_followups(self):
        session = llm.LLMSession(LLM_SYSTEM, TOOL_SPECS)
        budget = llm.max_steps()
        self.emit("llm", f"LLM planner engaged ({self.llm_state['provider']}/{self.llm_state['model']})",
                  f"Sending the case file ({len(self.ledger.items)} evidence items, {len(self.hyps)} hypotheses); budget {budget} tool calls.")
        pending: list[tuple[str, dict]] = []
        text = ("Here is the case file. Investigate with follow-up tools, then call finish_investigation.\n"
                + json.dumps(self._case_file(), default=str))
        finished = False
        used = 0
        while used < budget and not finished:
            reply = session.send(user_text=text, tool_results=pending)
            text, pending = None, []
            if reply.text:
                self.emit("llm", "LLM reasoning", reply.text[:600])
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                used += 1
                if call.name == "finish_investigation":
                    pending.append((call.id, self._accept_finish(call.args)))
                    finished = True
                    continue
                pending.append((call.id, self._llm_tool(call)))
        if not finished:
            reply = session.send(user_text="Tool budget reached. Call finish_investigation now with your grounded analysis.",
                                 tool_results=pending, force_tool="finish_investigation")
            calls = [c for c in reply.tool_calls if c.name == "finish_investigation"]
            if not calls:
                raise llm.LLMError("LLM did not return finish_investigation")
            self._accept_finish(calls[0].args)
        self.llm_usage = {"requests": session.calls, **session.usage}
        self.record("follow-up", "llm_session", "LLM-directed investigation",
                    f"{session.calls} requests, {used} tool calls", planner="llm")

    def _llm_tool(self, call) -> dict:
        args = call.args or {}
        reason = str(args.get("reason") or "(no reason given)")[:400]
        try:
            if call.name in ("inspect_service", "compare_change_window", "search_logs"):
                res = self._run_followup(call.name, args, reason, planner="llm")
                return {"ok": True, "summary": res["summary"], "evidence_id": res["evidence_id"],
                        "ranking": self._ranking_view()[:6]}
            if call.name == "add_hypothesis":
                return self._llm_add_hypothesis(args)
            return {"ok": False, "error": f"Unknown tool {call.name}"}
        except (ValueError, KeyError) as e:
            self.emit("warning", f"Rejected LLM tool call {call.name}", str(e))
            self.record("follow-up", f"rejected:{call.name}", reason, str(e), planner="llm", args=args)
            return {"ok": False, "error": str(e), "valid_services": sorted(self._all_services())[:40],
                    "valid_change_ids": [c["id"] for c in self.c["changes"]]}

    def _llm_add_hypothesis(self, args: dict) -> dict:
        svc = str(args.get("service", "")).strip()
        rationale = str(args.get("rationale", "")).strip()[:400]
        ids = self.ledger.valid(args.get("evidence_ids"))
        if svc not in self._all_services():
            raise ValueError(f"Unknown service {svc}")
        if not ids:
            raise ValueError("add_hypothesis needs at least one existing evidence ID")
        if sum(1 for h in self.hyps if h["source"] == "llm") >= MAX_LLM_HYPOTHESES:
            raise ValueError("Hypothesis limit reached")
        change = None
        if args.get("change_id"):
            change = next((c for c in self.c["changes"] if c["id"] == args["change_id"]), None)
            if change is None:
                raise ValueError(f"Unknown change_id {args['change_id']}")
        if any(h["service"] == svc and ((h.get("change") or {}).get("id") == (change or {}).get("id")) for h in self.hyps):
            raise ValueError("An equivalent hypothesis already exists")
        h = self._new_hypothesis("change" if change else "service_fault", svc, change, source="llm",
                                 rationale=f"{rationale}{_cite(ids)}")
        self._evaluate()
        self.emit("hypothesis", f"{h['id']} (proposed by LLM): {h['title']}", f"{rationale}{_cite(ids)} - scored {h['score']} "
                  f"by evidence checks ({h['verdict']}).", {"service": svc, "kind": h["kind"], "source": "llm"})
        self.record("follow-up", "add_hypothesis", rationale, f"{h['id']} scored {h['score']} ({h['verdict']})", ids, "llm",
                    {"service": svc, "change_id": (change or {}).get("id")})
        return {"ok": True, "hypothesis_id": h["id"], "score": h["score"], "verdict": h["verdict"], "ranking": self._ranking_view()[:6]}

    def _accept_finish(self, args: dict) -> dict:
        """Validate the LLM's final analysis against the ledger; drop anything ungrounded."""
        dropped = {"citations": [], "assessments": 0, "recommendations": 0}
        expl, valid, invalid = self.ledger.ground_text(str(args.get("explanation", ""))[:3000])
        dropped["citations"] += invalid
        hyp_ids = {h["id"] for h in self.hyps}
        assessments = []
        for a in args.get("assessments") or []:
            if not isinstance(a, dict) or a.get("hypothesis_id") not in hyp_ids:
                dropped["assessments"] += 1
                continue
            ids = self.ledger.valid(a.get("evidence_ids"))
            reasoning, _, inv = self.ledger.ground_text(str(a.get("reasoning", ""))[:800])
            dropped["citations"] += inv
            if not ids:
                dropped["assessments"] += 1
                continue
            assessments.append({"hypothesis_id": a["hypothesis_id"], "stance": a.get("stance") if a.get("stance") in
                                ("supports", "doubts", "refutes") else "doubts", "reasoning": reasoning, "evidence": ids})
        recs = []
        for r in args.get("recommendations") or []:
            ids = self.ledger.valid((r or {}).get("evidence_ids"))
            action, _, inv = self.ledger.ground_text(str((r or {}).get("action", ""))[:400])
            dropped["citations"] += inv
            if not ids or not action:
                dropped["recommendations"] += 1
                continue
            recs.append({"action": action, "horizon": r.get("horizon") if r.get("horizon") in
                         ("immediate", "short_term", "long_term") else "short_term", "evidence": ids})
        caveats, _, inv = self.ledger.ground_text(str(args.get("caveats", ""))[:800])
        dropped["citations"] += inv
        preferred = args.get("preferred_hypothesis_id")
        self.llm_analysis = {
            "provider": self.llm_state.get("provider"), "model": self.llm_state.get("model"),
            "explanation": expl if valid else None, "explanation_evidence": valid,
            "explanation_rejected": not valid,
            "preferred_hypothesis_id": preferred if preferred in hyp_ids else None,
            "assessments": assessments, "recommendations": recs, "caveats": caveats or None, "dropped": dropped,
        }
        self.emit("llm", "LLM analysis received and grounded",
                  f"Kept {len(assessments)} assessments and {len(recs)} recommendations; removed "
                  f"{len(dropped['citations'])} invalid citations, {dropped['assessments']} unsupported assessments, "
                  f"{dropped['recommendations']} unsupported recommendations."
                  + ("" if valid else " Explanation rejected: it cited no valid evidence."))
        self.record("follow-up", "finish_investigation", "LLM final analysis",
                    f"preferred {preferred}; {len(assessments)} assessments kept", valid, "llm")
        return {"accepted": True, "kept": {"assessments": len(assessments), "recommendations": len(recs)}, "dropped": dropped}

    # ================================================================== result
    def _data_gaps(self) -> list[str]:
        gaps = []
        if not self.inputs.metrics:
            gaps.append("No metrics were supplied: onset timing and resource saturation checks rely on logs only.")
        if not self.inputs.logs:
            gaps.append("No logs were supplied: failure signatures and cross-service blame could not be checked.")
        if not self.changes_supplied:
            gaps.append("No change records were supplied: change-triggered causes could not be tested.")
        if not self.inputs.dependencies.edges:
            gaps.append("No dependency map was supplied: origin vs victim was inferred from timing and log references only.")
        return gaps

    def _build_result(self) -> dict:
        m, l, c, d, affected, onset = self.m, self.l, self.c, self.d, self.affected, self.onset
        hyps = self.hyps
        top = hyps[0]
        svc, mode = top["service"], top["failure_mode"]
        conclusive = top["verdict"] == "supported"
        callers = [s for s in d["upstream_of"].get(svc, []) if s in affected]
        recs = recommendations(mode, svc, top.get("change"), callers)
        support_ids = [e for ck in top["checks"] if ck["result"] == "supports" for e in ck["evidence"]]
        support_ids = sorted(set(support_ids), key=lambda x: int(x[1:]))
        gaps = self._data_gaps()
        if not conclusive:
            recs["immediate"].insert(0, f"Treat {top['id']} as a lead, not a confirmed cause: verify it manually before acting.")
            recs["short_term"] = [f"Provide the missing evidence: {g}" for g in gaps] + recs["short_term"]
        if self.llm_analysis:
            for r in self.llm_analysis["recommendations"]:
                recs[r["horizon"]].append(f"{r['action']} (LLM, based on {', '.join(r['evidence'])})")

        timeline = []
        for ch in c["changes"]:
            timeline.append({"time": ch["timestamp"], "kind": "change", "service": ch["service"],
                             "text": f"{ch['id']} {ch['type']}: {ch['description']}", "evidence": ch.get("evidence_id"),
                             "highlight": bool(top.get("change") and top["change"]["id"] == ch["id"])})
        for s, t in sorted(affected.items(), key=lambda x: x[1]):
            sig = [a["metric"] for a in m["anomalies"] if a["service"] == s][:3] if m else []
            timeline.append({"time": iso(t), "kind": "anomaly", "service": s, "evidence": (self.ev["anom_svc"].get(s) or [None])[0],
                             "text": f"{s} starts failing" + (f" ({', '.join(sig)})" if sig else ""), "highlight": s == svc})
        if l:
            seen = set()
            for cl in sorted(l["new_error_clusters"], key=lambda x: x["first_seen"] or ""):
                if cl["service"] in seen:
                    continue
                seen.add(cl["service"])
                timeline.append({"time": cl["first_seen"], "kind": "log", "service": cl["service"], "evidence": cl.get("evidence_id"),
                                 "text": f"First {cl['category_label'].lower()}: {cl['sample'][:120]}", "highlight": False})
        if self.meta.get("reported_at"):
            timeline.append({"time": self.meta["reported_at"], "kind": "report", "service": None, "evidence": None,
                             "text": "Incident reported / declared", "highlight": False})
        timeline.sort(key=lambda x: x["time"] or "")

        impact_services = []
        for s, t in sorted(affected.items(), key=lambda x: x[1]):
            node = next((x for x in d["nodes"] if x["id"] == s), {})
            impact_services.append({
                "service": s, "onset": iso(t),
                "role": ("root cause" if conclusive else "leading suspect") if s == svc else node.get("status", "impacted"),
                "metrics": sorted({a["metric"] for a in (m["anomalies"] if m else []) if a["service"] == s}),
                "error_lines": sum(x["count"] for x in (l["new_error_clusters"] if l else []) if x["service"] == s),
            })
        label = MODE_LABELS.get(mode, mode)
        change_txt = (f"{top['change']['id']} ({top['change']['type'].replace('_', ' ')}"
                      f"{' ' + top['change']['version'] if top['change'].get('version') else ''}: "
                      f"{top['change']['description']})" if top.get("change") else "")
        path = next((p for p in d["propagation_paths"] if p and p[0] == svc), [svc] + callers)
        n_ref = sum(1 for h in hyps if h["verdict"] == "refuted")
        pct = int(round(top["confidence"] * 100))
        if conclusive:
            n_aff = f"{len(affected)} service{'s' if len(affected) != 1 else ''}"
            exec_summary = (f"Starting at {iso(onset)}, {n_aff} degraded{_cite([self.ev.get('onset')])}. The most probable "
                            f"root cause is {_lc(label)} in {svc}" + (f", introduced by {change_txt}" if change_txt else "") +
                            f" ({pct}% relative confidence among {len(hyps)} tested hypotheses){_cite(support_ids[:4])}. "
                            f"The failure propagated along {' -> '.join(path)}. {len(hyps) - 1} alternative hypotheses were tested; "
                            f"{n_ref} {'was' if n_ref == 1 else 'were'} refuted by the evidence.")
        else:
            exec_summary = (f"Starting at {iso(onset)}, {len(affected)} service{'s' if len(affected) != 1 else ''} degraded{_cite([self.ev.get('onset')])}. The evidence is "
                            f"not strong enough to confirm a root cause: the leading hypothesis, {top['title']}, scored {top['score']} "
                            f"(below {SUPPORTED_SCORE:g} needed){_cite(support_ids[:3])}. "
                            + (" ".join(gaps[:2]) if gaps else "Additional data would be needed to discriminate between hypotheses."))
        supports = [ck for ck in top["checks"] if ck["result"] == "supports"]
        explanation = ((f"{change_txt} was applied to {svc} {top['change']['minutes_before_onset']:.0f} minutes before the first "
                        f"anomaly{_cite([top['change'].get('evidence_id')])}. " if change_txt and top['change'].get('minutes_before_onset') is not None
                        else f"No change correlates with the failure of {svc}; the evidence points to a latent condition. ")
                       + " ".join(_cited(ck["detail"], ck["evidence"]) for ck in supports[:6]))
        contradicting = [ck for ck in top["checks"] if ck["result"] == "contradicts"]
        if contradicting:
            explanation += " Evidence against: " + " ".join(_cited(ck["detail"], ck["evidence"]) for ck in contradicting[:3])

        narrative = {"executive_summary": exec_summary, "root_cause_explanation": explanation, "source": "rules"}
        if self.llm_analysis and self.llm_analysis.get("explanation"):
            narrative["llm_explanation"] = self.llm_analysis["explanation"]
        title = top["title"] if conclusive else f"Inconclusive - leading hypothesis: {top['title']}"
        charts_services = sorted(set(affected) | set(self.traffic_only))
        return {
            "summary": {
                "title": title, "hypothesis_id": top["id"], "service": svc, "failure_mode": mode,
                "failure_mode_label": label, "confidence": top["confidence"], "verdict": "confirmed" if conclusive else "inconclusive",
                "hypothesis_verdict": top["verdict"], "score": top["score"], "unexplained_probability": self.unexplained,
                "confidence_basis": (f"Relative likelihood among {len(hyps)} tested hypotheses plus an 'unexplained' alternative, "
                                     f"computed only from evidence-check scores (softmax, T={TEMPERATURE:g}). "
                                     f"{top['id']} scored {top['score']}; 'unexplained' holds {int(round(self.unexplained * 100))}%."),
                "evidence_ids": support_ids,
                "change_id": top["change"]["id"] if top.get("change") else None,
                "onset": iso(onset), "data_end": m["window"]["end"] if m else (l["window"]["end"] if l else None),
                "affected_count": len(affected), "analysis_mode": self.mode,
                "llm_agrees": (self.llm_analysis["preferred_hypothesis_id"] == top["id"])
                if self.llm_analysis and self.llm_analysis.get("preferred_hypothesis_id") else None,
            },
            "narrative": narrative,
            "hypotheses": hyps,
            "timeline": timeline,
            "impact": {"services": impact_services, "secondary_traffic_shifts": self.traffic_only,
                       "blast_radius": sorted(set(d["upstream_of"].get(svc, [])) | {svc}), "propagation_path": path},
            "recommendations": recs,
            "recommendation_basis": support_ids,
            "data_gaps": gaps,
            "evidence_ledger": self.ledger.items,
            "evidence": {
                "metric_anomalies": (m["anomalies"] if m else [])[:40],
                "log_clusters": (l["new_error_clusters"] if l else [])[:20],
                "log_precursors": (l["precursors"] if l else []),
                "log_references": (l["references"] if l else []),
                "changes": c["changes"],
            },
            "graph": {"nodes": d["nodes"], "edges": d["edges"], "root": svc, "path": path},
            "charts": {
                "series": {s: v for s, v in (m["chart_series"].items() if m else []) if s in charts_services},
                "all_services": sorted(m["chart_series"]) if m else [],
                "error_timeline": (l["error_timeline"] if l else []),
                "baseline_end": m["window"]["baseline_end"] if m else None,
            },
            "tool_stats": {
                "metrics_points": len(self.inputs.metrics or []), "log_lines": len(self.inputs.logs or []),
                "changes": len(self.inputs.changes), "services_in_map": len(self.inputs.dependencies.services),
                "anomalies": len(m["anomalies"]) if m else 0, "error_signatures": len(l["new_error_clusters"]) if l else 0,
                "evidence_items": len(self.ledger.items), "followups": len(self.followups),
            },
            "llm_analysis": self.llm_analysis,
            "llm_usage": getattr(self, "llm_usage", None),
        }

    def _empty_result(self) -> dict:
        inputs = self.inputs
        return jsonable({
            "summary": {"title": "No root cause identified: no anomalies detected", "hypothesis_id": None, "service": None,
                        "failure_mode": "unknown", "failure_mode_label": "No anomaly detected", "confidence": None,
                        "verdict": "no_anomaly", "change_id": None, "onset": None, "affected_count": 0, "evidence_ids": [],
                        "analysis_mode": self.mode, **self._timing(),
                        "confidence_basis": "No hypothesis could be formed because no service deviated from its baseline."},
            "narrative": {"executive_summary": "The supplied telemetry shows no sustained deviation from baseline"
                                               f"{_cite([self.ev['coverage'].get('metrics'), self.ev['coverage'].get('logs')])}, "
                                               "so no root cause can be supported by evidence.",
                          "root_cause_explanation": "Provide a wider time window that includes the incident, or additional signals.",
                          "source": "rules"},
            "hypotheses": [], "timeline": [], "impact": {"services": [], "blast_radius": [], "propagation_path": [],
                                                         "secondary_traffic_shifts": []},
            "recommendations": {"immediate": [], "short_term": ["Verify the incident window and data sources."], "long_term": []},
            "recommendation_basis": [], "data_gaps": self._data_gaps(), "evidence_ledger": self.ledger.items,
            "evidence": {"metric_anomalies": [], "log_clusters": [], "log_precursors": [], "log_references": [],
                         "changes": [c.to_dict() for c in inputs.changes]},
            "graph": {"nodes": [{"id": s, "status": "healthy", "callers": [], "callees": [], "depth": 0}
                                for s in inputs.dependencies.services],
                      "edges": [{"from": a, "to": b, "propagation": False} for a, b in inputs.dependencies.edges],
                      "root": None, "path": []},
            "charts": {"series": (self.m or {}).get("chart_series", {}), "all_services": [], "error_timeline": [], "baseline_end": None},
            "tool_stats": {"metrics_points": len(inputs.metrics or []), "log_lines": len(inputs.logs or []),
                           "changes": len(inputs.changes), "services_in_map": len(inputs.dependencies.services),
                           "anomalies": 0, "error_signatures": 0, "evidence_items": len(self.ledger.items), "followups": 0},
            "llm_analysis": None, "llm_usage": None, "investigation_log": self.log,
        })
