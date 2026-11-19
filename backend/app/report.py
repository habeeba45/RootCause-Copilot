"""RCA report rendering: Markdown and standalone HTML."""
from __future__ import annotations

import html
from datetime import datetime, timezone

SECTIONS = (("immediate", "Immediate actions"), ("short_term", "Short-term fixes"), ("long_term", "Long-term prevention"))


def _pct(x) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _ids(ids) -> str:
    return ", ".join(ids or []) or "-"


VERDICT_TEXT = {"confirmed": "Root cause supported by evidence", "inconclusive": "INCONCLUSIVE - not enough evidence to confirm a root cause",
                "no_anomaly": "No anomaly detected"}


def to_markdown(incident: dict) -> str:
    r = incident["result"]
    s = r["summary"]
    out = [
        f"# RCA Report: {incident['title']}",
        "",
        f"- **Incident ID:** {incident['id']}" + (f" (analysis run #{incident['run_number']})" if incident.get("run_number") else ""),
        f"- **Severity:** {incident.get('severity') or 'n/a'}",
        f"- **Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by RootCause Copilot ({s.get('analysis_mode')})",
        f"- **Incident onset:** {s.get('onset') or 'n/a'}",
        f"- **Services affected:** {s.get('affected_count')}",
        "",
        "## Executive summary",
        "",
        r["narrative"]["executive_summary"],
        "",
        "## Probable root cause",
        "",
        f"> **{VERDICT_TEXT.get(s.get('verdict'), s.get('verdict'))}**",
        "",
        f"**{s['title']}**  ",
        f"Service: `{s.get('service')}` | Failure mode: {s.get('failure_mode_label')} | Confidence: **{_pct(s.get('confidence'))}**"
        + (f" | Triggering change: `{s['change_id']}`" if s.get("change_id") else ""),
        "",
        r["narrative"]["root_cause_explanation"],
        "",
        f"_Confidence basis: {s.get('confidence_basis', '')}_",
        "",
    ]
    if r.get("data_gaps"):
        out += ["### Data gaps", ""] + [f"- {g}" for g in r["data_gaps"]] + [""]
    la = r.get("llm_analysis")
    if la:
        out += [f"## LLM analysis ({la.get('provider')}/{la.get('model')})", ""]
        if la.get("explanation"):
            out += [la["explanation"], ""]
        else:
            out += ["_The LLM explanation was rejected because it cited no valid evidence._", ""]
        if la.get("preferred_hypothesis_id"):
            out += [f"LLM preferred hypothesis: **{la['preferred_hypothesis_id']}**"
                    + (" (agrees with the evidence ranking)" if s.get("llm_agrees") else " (differs from the evidence ranking)"), ""]
        for a in la.get("assessments", []):
            out.append(f"- **{a['hypothesis_id']}** {a['stance']}: {a['reasoning']} [{_ids(a['evidence'])}]")
        if la.get("caveats"):
            out += ["", f"Caveats: {la['caveats']}"]
        d = la.get("dropped", {})
        out += ["", f"_Grounding: removed {len(d.get('citations', []))} invalid citations, {d.get('assessments', 0)} unsupported "
                    f"assessments and {d.get('recommendations', 0)} unsupported recommendations._", ""]
    hyps = r.get("hypotheses", [])
    if hyps:
        top = hyps[0]
        out += ["### Supporting evidence", ""]
        out += [f"- {'✅' if c['result'] == 'supports' else '❌' if c['result'] == 'contradicts' else '➖'} **{c['name']}** "
                f"({c['weight']:+}) - {c['detail']} [{_ids(c.get('evidence'))}]"
                for c in top["checks"]]
        out += ["", "## Hypotheses tested", "", "| Rank | ID | Hypothesis | Verdict | Score | Confidence |",
                "|---|---|---|---|---|---|"]
        out += [f"| {h['rank']} | {h['id']} | {h['title']} | {h['verdict']} | {h['score']} | {_pct(h['confidence'])} |" for h in hyps]
        out.append("")
    imp = r["impact"]
    if imp["services"]:
        out += ["## Impact", "", f"Propagation path: {' → '.join(imp['propagation_path'])}", "",
                "| Service | Onset | Role | Anomalous metrics | New error lines |", "|---|---|---|---|---|"]
        out += [f"| {x['service']} | {x['onset']} | {x['role']} | {', '.join(x['metrics']) or '-'} | {x['error_lines']} |"
                for x in imp["services"]]
        if imp.get("secondary_traffic_shifts"):
            out += ["", f"Secondary traffic shifts (consequences): {', '.join(imp['secondary_traffic_shifts'])}"]
        out.append("")
    if r["timeline"]:
        out += ["## Timeline", "", "| Time (UTC) | Type | Service | Event |", "|---|---|---|---|"]
        out += [f"| {t['time']} | {t['kind']} | {t.get('service') or '-'} | {'**' if t['highlight'] else ''}"
                f"{t['text'].replace('|', '/')}{'**' if t['highlight'] else ''} |" for t in r["timeline"]]
        out.append("")
    out += ["## Recommended solutions", ""]
    for key, label in SECTIONS:
        items = r["recommendations"].get(key, [])
        if items:
            out += [f"### {label}", ""] + [f"{i}. {x}" for i, x in enumerate(items, 1)] + [""]
    if r.get("llm_review"):
        lr = r["llm_review"]
        out += ["## LLM reviewer notes", "", lr.get("reviewer_notes") or "-", ""]
    led = r.get("evidence_ledger") or []
    if led:
        out += ["## Appendix A: Evidence ledger", "", "| ID | Type | Tool | Service | Time (UTC) | Finding |", "|---|---|---|---|---|---|"]
        out += [f"| {e['id']} | {e['kind']} | {e['tool']} | {e.get('service') or '-'} | {e.get('time') or '-'} | "
                f"{e['summary'].replace('|', '/')} |" for e in led]
        out.append("")
    log = r.get("investigation_log") or []
    if log:
        out += ["## Appendix B: Investigation log", "", "| # | Phase | Planner | Action | Reason | Outcome | Evidence |", "|---|---|---|---|---|---|---|"]
        out += [f"| {x['step']} | {x['phase']} | {x['planner']} | {x['action']} | {x['reason'].replace('|', '/')} | "
                f"{x['outcome'].replace('|', '/')} | {_ids(x.get('evidence'))[:80]} |" for x in log]
        out.append("")
    ts = r["tool_stats"]
    out += ["## Methodology", "",
            "RootCause Copilot's agent ran four tools - metrics_analyzer (robust baselines, sustained z-score anomalies), "
            "log_analyzer (template mining, failure classification, cross-service references), change_detector "
            "(change/onset correlation) and dependency_analyzer (fault localisation, blast radius) - then generated "
            "hypotheses and tested each one against explicit evidence checks before ranking them.", "",
            f"Inputs: {ts['metrics_points']:,} metric points, {ts['log_lines']:,} log lines, {ts['changes']} changes, "
            f"{ts['services_in_map']} mapped services. {ts.get('evidence_items', 0)} evidence items, "
            f"{ts.get('followups', 0)} follow-up tool runs.", "",
            "Every finding is recorded in the evidence ledger (Appendix A) and every check cites ledger IDs. Confidence values "
            "are computed only from evidence-check scores.", ""]
    return "\n".join(out)


CSS = """
body{font-family:Inter,Segoe UI,system-ui,sans-serif;color:#0f172a;max-width:960px;margin:40px auto;padding:0 24px;line-height:1.55}
h1{font-size:26px;margin-bottom:4px}h2{font-size:19px;margin-top:32px;border-bottom:2px solid #e2e8f0;padding-bottom:6px}
h3{font-size:15px;margin-top:20px}.meta{color:#475569;font-size:13px}.card{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 18px}
.root{border-left:5px solid #dc2626}.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}
.supported{background:#dcfce7;color:#166534}.contributing{background:#e0e7ff;color:#3730a3}.plausible{background:#fef9c3;color:#854d0e}.refuted{background:#fee2e2;color:#991b1b}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}th,td{border:1px solid #e2e8f0;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f1f5f9}tr.hl td{background:#fef2f2;font-weight:600}.ok{color:#16a34a}.bad{color:#dc2626}.neu{color:#64748b}
code{background:#f1f5f9;padding:1px 5px;border-radius:4px}.conf{font-size:28px;font-weight:700;color:#dc2626}
@media print{body{margin:0}}
"""


def to_html(incident: dict) -> str:
    e = html.escape
    r = incident["result"]
    s = r["summary"]
    parts = [f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>RCA - {e(incident['title'])}</title><style>{CSS}</style></head><body>",
             f"<h1>RCA Report: {e(incident['title'])}</h1>",
             f"<div class='meta'>Incident {e(incident['id'])}{' · run #' + str(incident['run_number']) if incident.get('run_number') else ''} · Severity {e(incident.get('severity') or 'n/a')} · Onset {e(s.get('onset') or 'n/a')} · "
             f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by RootCause Copilot ({e(s.get('analysis_mode') or '')})</div>",
             "<h2>Executive summary</h2>", f"<p>{e(r['narrative']['executive_summary'])}</p>",
             "<h2>Probable root cause</h2>",
             (f"<div class='card' style='border-left:5px solid #d97706;margin-bottom:12px'><b>{e(VERDICT_TEXT.get(s.get('verdict'), ''))}</b></div>"
              if s.get("verdict") != "confirmed" else ""),
             f"<div class='card root'><div style='display:flex;justify-content:space-between;gap:16px'><div><b>{e(s['title'])}</b><br>"
             f"<span class='meta'>Service <code>{e(str(s.get('service')))}</code> · {e(s.get('failure_mode_label') or '')}"
             + (f" · change <code>{e(s['change_id'])}</code>" if s.get("change_id") else "") +
             f"</span></div><div class='conf'>{_pct(s.get('confidence'))}</div></div>"
             f"<p>{e(r['narrative']['root_cause_explanation'])}</p>"
             f"<p class='meta'>Confidence basis: {e(s.get('confidence_basis', ''))}</p></div>"]
    if r.get("data_gaps"):
        parts.append("<h3>Data gaps</h3><ul>" + "".join(f"<li>{e(g)}</li>" for g in r["data_gaps"]) + "</ul>")
    la = r.get("llm_analysis")
    if la:
        parts.append(f"<h2>LLM analysis <span class='meta'>({e(str(la.get('provider')))}/{e(str(la.get('model')))})</span></h2>")
        parts.append(f"<p>{e(la['explanation'])}</p>" if la.get("explanation") else
                     "<p class='meta'>The LLM explanation was rejected because it cited no valid evidence.</p>")
        if la.get("preferred_hypothesis_id"):
            parts.append(f"<p>Preferred hypothesis: <b>{e(la['preferred_hypothesis_id'])}</b> "
                         f"{'(agrees with the evidence ranking)' if s.get('llm_agrees') else '(differs from the evidence ranking)'}</p>")
        if la.get("assessments"):
            parts.append("<ul>" + "".join(f"<li><b>{e(a['hypothesis_id'])}</b> {e(a['stance'])}: {e(a['reasoning'])} "
                                          f"<span class='meta'>[{e(_ids(a['evidence']))}]</span></li>" for a in la["assessments"]) + "</ul>")
        if la.get("caveats"):
            parts.append(f"<p class='meta'>Caveats: {e(la['caveats'])}</p>")
        dd = la.get("dropped", {})
        parts.append(f"<p class='meta'>Grounding: removed {len(dd.get('citations', []))} invalid citations, {dd.get('assessments', 0)} "
                     f"unsupported assessments, {dd.get('recommendations', 0)} unsupported recommendations.</p>")
    hyps = r.get("hypotheses", [])
    if hyps:
        parts.append("<h3>Evidence for the leading hypothesis</h3><table><tr><th></th><th>Check</th><th>Detail</th><th>Evidence</th><th>Weight</th></tr>")
        for c in hyps[0]["checks"]:
            cls, sym = {"supports": ("ok", "✔"), "contradicts": ("bad", "✘")}.get(c["result"], ("neu", "–"))
            parts.append(f"<tr><td class='{cls}'>{sym}</td><td>{e(c['name'])}</td><td>{e(c['detail'])}</td>"
                         f"<td><code>{e(_ids(c.get('evidence')))}</code></td><td>{c['weight']:+}</td></tr>")
        parts.append("</table><h2>Hypotheses tested</h2><table><tr><th>#</th><th>Hypothesis</th><th>Verdict</th><th>Score</th><th>Confidence</th></tr>")
        for h in hyps:
            parts.append(f"<tr><td>{h['rank']}</td><td><b>{e(h['id'])}</b> {e(h['title'])}<br><span class='meta'>{e(h['statement'])}</span></td>"
                         f"<td><span class='badge {e(h['verdict'])}'>{e(h['verdict'])}</span></td><td>{h['score']}</td><td>{_pct(h['confidence'])}</td></tr>")
        parts.append("</table>")
    imp = r["impact"]
    if imp["services"]:
        parts.append(f"<h2>Impact</h2><p>Propagation path: <b>{' → '.join(e(x) for x in imp['propagation_path'])}</b></p>"
                     "<table><tr><th>Service</th><th>Onset</th><th>Role</th><th>Anomalous metrics</th><th>New error lines</th></tr>")
        for x in imp["services"]:
            parts.append(f"<tr{' class=hl' if x['role'] == 'root cause' else ''}><td>{e(x['service'])}</td><td>{e(x['onset'] or '')}</td>"
                         f"<td>{e(x['role'])}</td><td>{e(', '.join(x['metrics']) or '-')}</td><td>{x['error_lines']}</td></tr>")
        parts.append("</table>")
    if r["timeline"]:
        parts.append("<h2>Timeline (UTC)</h2><table><tr><th>Time</th><th>Type</th><th>Service</th><th>Event</th></tr>")
        for t in r["timeline"]:
            parts.append(f"<tr{' class=hl' if t['highlight'] else ''}><td>{e(t['time'] or '')}</td><td>{e(t['kind'])}</td>"
                         f"<td>{e(t.get('service') or '-')}</td><td>{e(t['text'])}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>Recommended solutions</h2>")
    for key, label in SECTIONS:
        items = r["recommendations"].get(key, [])
        if items:
            parts.append(f"<h3>{label}</h3><ol>" + "".join(f"<li>{e(x)}</li>" for x in items) + "</ol>")
    if r.get("llm_review"):
        parts.append(f"<h2>LLM reviewer notes</h2><p>{e(r['llm_review'].get('reviewer_notes') or '-')}</p>")
    led = r.get("evidence_ledger") or []
    if led:
        parts.append("<h2>Appendix A: Evidence ledger</h2><table><tr><th>ID</th><th>Type</th><th>Tool</th><th>Service</th><th>Time</th><th>Finding</th></tr>")
        parts += [f"<tr><td><code>{x['id']}</code></td><td>{e(x['kind'])}</td><td>{e(x['tool'])}</td><td>{e(x.get('service') or '-')}</td>"
                  f"<td>{e(x.get('time') or '-')}</td><td>{e(x['summary'])}</td></tr>" for x in led]
        parts.append("</table>")
    log = r.get("investigation_log") or []
    if log:
        parts.append("<h2>Appendix B: Investigation log</h2><table><tr><th>#</th><th>Phase</th><th>Planner</th><th>Action</th><th>Reason</th><th>Outcome</th><th>Evidence</th></tr>")
        parts += [f"<tr><td>{x['step']}</td><td>{e(x['phase'])}</td><td>{e(x['planner'])}</td><td>{e(x['action'])}</td><td>{e(x['reason'])}</td>"
                  f"<td>{e(x['outcome'])}</td><td><code>{e(_ids(x.get('evidence'))[:80])}</code></td></tr>" for x in log]
        parts.append("</table>")
    ts = r["tool_stats"]
    parts.append("<h2>Methodology</h2><p class='meta'>The agent ran metrics_analyzer, log_analyzer, change_detector and "
                 "dependency_analyzer, generated hypotheses from their findings and tested each against explicit evidence checks "
                 f"before ranking. Inputs: {ts['metrics_points']:,} metric points, {ts['log_lines']:,} log lines, "
                 f"{ts['changes']} changes, {ts['services_in_map']} mapped services.</p></body></html>")
    return "".join(parts)
