"""End-to-end and unit tests for RootCause Copilot. Run from backend/:  python -m pytest -q"""
import json
import os
import tempfile
import time

os.environ["RCA_DATA_DIR"] = tempfile.mkdtemp(prefix="rca-test-")
os.environ["AGENT_STEP_DELAY"] = "0"
os.environ["LLM_PROVIDER"] = "none"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import llm  # noqa: E402
from app.agent import IncidentInputs, RCAAgent  # noqa: E402
from app.main import app  # noqa: E402
from app.parsers import parse_changes, parse_dependencies, parse_logs, parse_metrics  # noqa: E402
from app.storage import SAMPLE_DIR  # noqa: E402

client = TestClient(app)

EXPECTED = {
    "checkout-failures": ("payment-service", "db_connection_pool", "CHG-4817"),
    "product-page-latency": ("recommendation-service", "memory", "CHG-7714"),
    "login-outage": ("auth-service", "certificate", None),
}
FILES = (("metrics", "metrics.csv"), ("logs", "app.log"), ("changes", "changes.json"), ("dependencies", "dependencies.json"))


def load_sample(sid):
    d = SAMPLE_DIR / sid
    return IncidentInputs(
        parse_metrics((d / "metrics.csv").read_bytes()), parse_logs((d / "app.log").read_bytes()),
        parse_changes((d / "changes.json").read_bytes()), parse_dependencies((d / "dependencies.json").read_bytes()))


def sample_files(sid):
    d = SAMPLE_DIR / sid
    return {k: (n, (d / n).read_bytes()) for k, n in FILES}


def wait_done(iid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        inc = client.get(f"/api/incidents/{iid}").json()
        if inc["status"] in ("completed", "failed"):
            return inc
        time.sleep(0.1)
    raise AssertionError("analysis did not finish")


def run_agent(inp, use_llm=False):
    events = []
    return RCAAgent(events.append, pace=0, use_llm=use_llm).run(inp, {}), events


def assert_grounded(r):
    """Every evidence ID referenced anywhere must exist in the ledger."""
    ids = {e["id"] for e in r["evidence_ledger"]}
    for h in r["hypotheses"]:
        for c in h["checks"]:
            assert set(c["evidence"]) <= ids, (h["id"], c)
    assert set(r["summary"]["evidence_ids"]) <= ids
    for x in r["investigation_log"]:
        assert set(x["evidence"]) <= ids
    import re
    for text in (r["narrative"]["executive_summary"], r["narrative"]["root_cause_explanation"]):
        assert set(re.findall(r"\bE\d+\b", text)) <= ids


# ------------------------------------------------------------------ agent accuracy & grounding
@pytest.mark.parametrize("sid", list(EXPECTED))
def test_agent_finds_expected_root_cause(sid):
    r, events = run_agent(load_sample(sid))
    svc, mode, change = EXPECTED[sid]
    s = r["summary"]
    assert (s["service"], s["failure_mode"], s["change_id"]) == (svc, mode, change)
    assert s["verdict"] == "confirmed" and s["confidence"] > 0.6
    assert any(h["verdict"] == "refuted" for h in r["hypotheses"]), "decoys should be refuted"
    assert_grounded(r)
    types = {e["type"] for e in events}
    assert {"plan", "decision", "tool_call", "tool_result", "hypothesis", "test", "ranking", "conclusion"} <= types
    tools = {e["tool"] for e in events if e["tool"]}
    assert {"metrics_analyzer", "log_analyzer", "change_detector", "dependency_analyzer"} <= tools
    assert tools & {"inspect_service", "compare_change_window"}, "the leader must be verified by a follow-up tool"
    assert any(x["phase"] == "follow-up" for x in r["investigation_log"])


def test_confidences_are_computed_from_scores():
    r, _ = run_agent(load_sample("checkout-failures"))
    import math
    z = sum(math.exp(h["score"] / 3) for h in r["hypotheses"]) + math.exp(4 / 3)
    for h in r["hypotheses"]:
        assert abs(h["confidence"] - math.exp(h["score"] / 3) / z) < 0.002
        assert abs(h["score"] - sum(c["weight"] for c in h["checks"])) < 0.02


def test_change_window_refutes_decoy():
    inp = load_sample("checkout-failures")
    events = []
    agent = RCAAgent(events.append, pace=0, use_llm=False)
    agent.run(inp, {})
    res = agent._tool_compare_change_window("CHG-4812")  # inventory feature flag - a red herring
    assert res["testable"] and not res["significant"]
    res = agent._tool_compare_change_window("CHG-4817")
    assert res["significant"] and any(s["metric"] == "db_pool_utilization_pct" for s in res["shifts"])


def test_planner_adapts_to_missing_inputs():
    inp = load_sample("checkout-failures")
    inp.metrics, inp.changes = None, []
    r, events = run_agent(inp)
    assert r["summary"]["service"] == "payment-service"
    assert "metrics_analyzer" not in {e["tool"] for e in events}
    titles = [e["title"] for e in events if e["type"] == "decision"]
    assert "Planner skipped change correlation" in titles
    assert any("change records" in g for g in r["data_gaps"])
    assert_grounded(r)


def test_inconclusive_when_evidence_is_weak():
    csv = "timestamp,service,metric,value\n" + "".join(
        f"2026-01-01T00:{m:02d}:00Z,svc-a,cpu_pct,{30 + (m % 3) + (40 if m >= 40 else 0)}\n" for m in range(60))
    r, events = run_agent(IncidentInputs(metrics=parse_metrics(csv)))
    assert r["summary"]["verdict"] == "inconclusive"
    assert r["summary"]["title"].startswith("Inconclusive")
    assert r["data_gaps"] and events[-1]["title"].startswith("Inconclusive")
    assert_grounded(r)


def test_agent_no_anomalies():
    csv = "timestamp,service,metric,value\n" + "".join(
        f"2026-01-01T00:{m:02d}:00Z,svc,cpu_pct,{30 + (m % 3)}\n" for m in range(60))
    r, _ = run_agent(IncidentInputs(metrics=parse_metrics(csv)))
    assert r["summary"]["verdict"] == "no_anomaly" and r["summary"]["confidence"] is None and r["hypotheses"] == []


# ------------------------------------------------------------------ parsers & validation
def test_parsers_alternate_formats():
    wide = "time,service,cpu_pct,latency_ms\n2026-01-01 00:00:00,a,10,100\n2026-01-01 00:01:00,a,11,110\n"
    assert {p.metric for p in parse_metrics(wide)} == {"cpu_pct", "latency_ms"}
    logs = parse_logs('{"timestamp":"2026-01-01T00:00:00Z","level":"error","service":"a","message":"boom"}\n'
                      "2026-01-01T00:00:01Z a: WARNING slow request\n"
                      "2026-01-01 00:00:02,123 ERROR [b] java.lang.NullPointerException\n    at Foo.bar(Foo.java:1)\n")
    assert [(e.service, e.level) for e in logs] == [("a", "ERROR"), ("a", "WARN"), ("b", "ERROR")]
    assert "Foo.bar" in logs[2].message
    assert parse_changes("timestamp,service,type,description\n2026-01-01T00:00:00Z,a,release,Deploy v1\n")[0].type == "deployment"
    dm = parse_dependencies('{"a": ["b", "c"], "b": "c"}')
    assert ("a", "b") in dm.edges and ("b", "c") in dm.edges and set(dm.services) == {"a", "b", "c"}


def test_validate_endpoint_summarises_files():
    r = client.post("/api/validate", files=sample_files("checkout-failures")).json()
    assert r["ok"] and r["can_analyze"]
    m = r["files"]["metrics"]["summary"]
    assert m["services"] and m["points"] == 6150 and m["sample_interval_s"] == 60.0
    assert r["files"]["logs"]["summary"]["levels"]["ERROR"] > 0
    assert r["files"]["dependencies"]["summary"]["edges"] == 9


def test_validate_endpoint_reports_problems():
    bad = client.post("/api/validate", files={"metrics": ("m.csv", b"foo,bar\n1,2\n")}).json()
    assert not bad["ok"] and "timestamp" in bad["files"]["metrics"]["error"]
    mism = client.post("/api/validate", files={
        "metrics": ("m.csv", "timestamp,service,metric,value\n" + "".join(
            f"2026-01-01T00:{i:02d}:00Z,a,cpu_pct,1\n" for i in range(30))),
        "logs": ("a.log", b"2026-02-01T00:00:00Z ERROR [a] boom\n"),
        "dependencies": ("d.json", b'{"x": ["y"]}')}).json()
    msgs = " ".join(c["message"] for c in mism["cross_checks"])
    assert "do not overlap" in msgs and "missing from the dependency map" in msgs
    assert mism["ok"]  # warnings do not block analysis
    assert not client.post("/api/validate", files={"changes": ("c.json", b"[]")}).json()["can_analyze"]


# ------------------------------------------------------------------ API
def test_health_samples_and_llm_test_without_key():
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["llm"]["enabled"] is False
    assert "key" not in json.dumps(h["llm"]).lower()
    assert {s["id"] for s in client.get("/api/samples").json()} == set(EXPECTED)
    t = client.get("/api/llm/test").json()
    assert t["ok"] is False and "API key" in t["error"]


def test_sample_flow_events_and_reports():
    inc = client.post("/api/incidents/from-sample/product-page-latency").json()
    done = wait_done(inc["id"])
    assert done["status"] == "completed" and done["result"]["summary"]["service"] == "recommendation-service"
    assert done["result"]["evidence_ledger"] and done["result"]["investigation_log"]
    ev = client.get(f"/api/incidents/{inc['id']}/events").json()["events"]
    assert ev[0]["type"] == "plan" and ev[-1]["type"] == "conclusion"
    with client.stream("GET", f"/api/incidents/{inc['id']}/stream") as s:
        body = "".join(s.iter_text())
    assert "event: activity" in body and "event: done" in body
    for fmt, needle in (("html", "Evidence ledger"), ("md", "Appendix B: Investigation log"), ("json", '"evidence_ledger"')):
        r = client.get(f"/api/incidents/{inc['id']}/report?format={fmt}")
        assert r.status_code == 200 and needle in r.text and "attachment" in r.headers["content-disposition"]
    stats = client.get("/api/stats").json()
    assert stats["completed"] >= 1 and stats["avg_confidence"] > 0


def test_upload_flow_rerun_delete_and_validation_stored():
    r = client.post("/api/incidents", data={"title": "Login down", "severity": "SEV-1"}, files=sample_files("login-outage"))
    assert r.status_code == 200, r.text
    iid = r.json()["id"]
    inc = wait_done(iid)
    assert inc["result"]["summary"]["service"] == "auth-service"
    assert inc["files"]["metrics"]["summary"]["points"] > 0 and "validation" in inc
    assert client.post(f"/api/incidents/{iid}/analyze").status_code == 200
    assert wait_done(iid)["status"] == "completed"
    assert client.delete(f"/api/incidents/{iid}").status_code == 200
    assert client.get(f"/api/incidents/{iid}").status_code == 404


def test_upload_validation_errors():
    assert client.post("/api/incidents", data={"title": "x"}).status_code == 400
    bad = client.post("/api/incidents", data={"title": "x"}, files={"metrics": ("m.csv", b"foo,bar\n1,2\n")})
    assert bad.status_code == 400 and "timestamp" in bad.json()["detail"]
    bad = client.post("/api/incidents", data={"title": "x"},
                      files={"logs": ("a.log", b"2026-01-01T00:00:00Z ERROR [a] x\n"), "dependencies": ("d.json", b"{oops")})
    assert bad.status_code == 400
    assert client.post("/api/incidents", data={"title": "x", "severity": "SEV-9"},
                       files={"logs": ("a.log", b"2026-01-01T00:00:00Z ERROR [a] x\n")}).status_code == 400
    assert client.get("/api/incidents/NOPE/report").status_code == 404


# ------------------------------------------------------------------ LLM integration (mocked HTTP)
class FakeLLM:
    """Scripted LLM server. Records every request for assertions."""

    def __init__(self, provider, script):
        self.provider, self.script, self.requests = provider, list(script), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        step = self.script.pop(0)
        if isinstance(step, int):
            return httpx.Response(step, json={"error": "boom"})
        return httpx.Response(200, json=self._anthropic(step) if self.provider == "anthropic" else self._openai(step))

    @staticmethod
    def _anthropic(step):
        content = [{"type": "text", "text": step.get("text", "")}] if step.get("text") else []
        content += [{"type": "tool_use", "id": f"tu_{i}_{n}", "name": n, "input": a} for i, (n, a) in enumerate(step["calls"])]
        return {"content": content, "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 20}}

    @staticmethod
    def _openai(step):
        calls = [{"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                 for i, (n, a) in enumerate(step["calls"])]
        return {"choices": [{"message": {"role": "assistant", "content": step.get("text"), "tool_calls": calls},
                             "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def finish_args(**over):
    args = {"preferred_hypothesis_id": "H1",
            "explanation": "The deploy cut the pool [E3] and errors followed [E14]; also see [E999].",
            "assessments": [{"hypothesis_id": "H1", "stance": "supports", "reasoning": "Timing and pool errors [E29]", "evidence_ids": ["E29", "E3"]},
                            {"hypothesis_id": "H2", "stance": "refutes", "reasoning": "No evidence", "evidence_ids": ["E999"]},
                            {"hypothesis_id": "H77", "stance": "supports", "reasoning": "x", "evidence_ids": ["E1"]}],
            "recommendations": [{"action": "Restore pool size to 50 [E29]", "horizon": "immediate", "evidence_ids": ["E29"]},
                                {"action": "Buy more servers", "horizon": "long_term", "evidence_ids": []}],
            "caveats": "Only one deploy observed [E29]."}
    args.update(over)
    return args


@pytest.fixture
def fake_llm(monkeypatch):
    def install(provider, script, max_steps="8"):
        monkeypatch.setenv("LLM_PROVIDER", provider)
        monkeypatch.setenv("ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY", "sk-test-secret-key-123456")
        monkeypatch.setenv("LLM_MAX_STEPS", max_steps)
        fake = FakeLLM(provider, script)
        monkeypatch.setattr(llm, "_TRANSPORT", httpx.MockTransport(fake))
        return fake
    return install


def test_llm_planner_anthropic_full_loop(fake_llm):
    fake = fake_llm("anthropic", [
        {"text": "Verify the leader first.", "calls": [("inspect_service", {"service": "payment-service", "reason": "check caller lag"})]},
        {"calls": [("compare_change_window", {"change_id": "CHG-4812", "reason": "rule out the flag"}),
                   ("search_logs", {"pattern": "maxSize=10", "service": "payment-service", "reason": "confirm config"})]},
        {"calls": [("add_hypothesis", {"service": "ghost-service", "rationale": "x", "evidence_ids": ["E1"]}),
                   ("add_hypothesis", {"service": "checkout-service", "rationale": "It timed out [E15]", "evidence_ids": ["E15"]})]},
        {"calls": [("finish_investigation", finish_args())]},
    ])
    r, events = run_agent(load_sample("checkout-failures"), use_llm=True)
    s = r["summary"]
    assert s["analysis_mode"] == "llm-assisted" and s["service"] == "payment-service" and s["llm_agrees"] is True
    la = r["llm_analysis"]
    assert "[E3]" in la["explanation"] and "E999" not in la["explanation"]
    assert la["dropped"]["citations"].count("E999") >= 1
    assert [a["hypothesis_id"] for a in la["assessments"]] == ["H1"] and la["dropped"]["assessments"] == 2
    assert len(la["recommendations"]) == 1 and la["dropped"]["recommendations"] == 1
    assert any("LLM, based on E29" in x for x in r["recommendations"]["immediate"])
    # LLM-proposed hypothesis was scored by the evidence checks, the ghost one rejected
    llm_h = [h for h in r["hypotheses"] if h["source"] == "llm"]
    assert len(llm_h) == 1 and llm_h[0]["service"] == "checkout-service" and llm_h[0]["checks"]
    assert any(e["type"] == "warning" and "add_hypothesis" in e["title"] for e in events)
    assert {x["planner"] for x in r["investigation_log"] if x["phase"] == "follow-up"} == {"llm"}
    assert_grounded(r)
    # protocol: tool results are sent back with matching ids, key only in header
    second = fake.requests[1]["body"]["messages"][-1]["content"]
    assert second[0]["type"] == "tool_result" and second[0]["tool_use_id"] == "tu_0_inspect_service"
    assert fake.requests[0]["headers"]["x-api-key"] == "sk-test-secret-key-123456"
    assert all("sk-test-secret" not in json.dumps(q["body"]) for q in fake.requests)
    assert "sk-test-secret" not in json.dumps(r)


def test_llm_planner_openai_with_forced_finish(fake_llm):
    fake = fake_llm("openai", [
        {"calls": [("inspect_service", {"service": "auth-service", "reason": "verify"})]},
        {"calls": [("finish_investigation", finish_args(preferred_hypothesis_id="H3",
                                                        explanation="Cert expired [E13] and gateway blamed auth [E14]."))]},
    ], max_steps="1")
    r, _ = run_agent(load_sample("login-outage"), use_llm=True)
    assert fake.requests[1]["body"]["tool_choice"] == {"type": "function", "function": {"name": "finish_investigation"}}
    assert fake.requests[1]["body"]["messages"][-2]["role"] == "tool"
    assert fake.requests[0]["headers"]["authorization"] == "Bearer sk-test-secret-key-123456"
    assert r["summary"]["service"] == "auth-service"          # the evidence ranking is never overridden by the LLM
    assert r["summary"]["llm_agrees"] is False                 # but its disagreement is reported
    assert r["llm_analysis"]["explanation"].startswith("Cert expired [E13]")


def test_llm_failure_falls_back_to_rule_planner(fake_llm):
    fake_llm("anthropic", [500])
    r, events = run_agent(load_sample("product-page-latency"), use_llm=True)
    assert r["summary"]["analysis_mode"] == "rule-based (LLM fallback)"
    assert r["summary"]["service"] == "recommendation-service" and r["llm_analysis"] is None
    assert any(e["type"] == "warning" and "LLM planner unavailable" in e["title"] for e in events)
    assert "sk-test-secret" not in json.dumps(events)


def test_llm_explanation_without_valid_citations_is_rejected(fake_llm):
    fake_llm("anthropic", [{"calls": [("finish_investigation", finish_args(explanation="It was DNS, trust me [E9999]."))]}])
    r, _ = run_agent(load_sample("checkout-failures"), use_llm=True)
    assert r["llm_analysis"]["explanation"] is None and r["llm_analysis"]["explanation_rejected"]
    assert "llm_explanation" not in r["narrative"]


def test_llm_connection_test_endpoint(fake_llm):
    fake_llm("anthropic", [{"text": "ready", "calls": []}])
    t = client.get("/api/llm/test").json()
    assert t["ok"] and t["provider"] == "anthropic" and "sk-test" not in json.dumps(t)


# ------------------------------------------------------------------ SQLite persistence & investigation history
def test_sqlite_persists_across_restart_and_keeps_run_history(tmp_path):
    from app.storage import Store
    db = tmp_path / "t.db"
    s1 = Store(db)
    inc = s1.create("t", "d", "SEV-2", "upload", {"logs": ("a.log", b"2026-01-01T00:00:00Z ERROR [a] x\n")})
    r1 = s1.start_run(inc["id"], "rule-based")
    s1.add_event(inc["id"], {"type": "plan", "title": "p"})
    s1.finish_run(inc["id"], r1, "completed", {"summary": {"title": "first", "verdict": "confirmed", "confidence": 0.9}})
    r2 = s1.start_run(inc["id"], "rule-based")
    s1.add_event(inc["id"], {"type": "plan", "title": "p2"})
    s1.db.close()
    s2 = Store(db)  # "server restart" while run 2 was in progress
    got = s2.get(inc["id"])
    assert got["status"] == "failed" and "interrupted" in got["error"]
    assert s2.read_input(inc["id"], "logs").startswith(b"2026-01-01")
    runs = s2.list_runs(inc["id"])
    assert [(r["number"], r["status"]) for r in runs] == [(2, "failed"), (1, "completed")]
    assert s2.get_run(inc["id"], r1)["result"]["summary"]["title"] == "first"
    assert [e["title"] for e in s2.get_run(inc["id"], r2)["events"]] == ["p2"]
    assert s2.delete(inc["id"]) and s2.list_runs(inc["id"]) == [] and s2.get_run_events(r1) == []


def test_legacy_json_incidents_are_imported(tmp_path, monkeypatch):
    from app import storage
    legacy = tmp_path / "incidents" / "INC-OLD001"
    legacy.mkdir(parents=True)
    (legacy / "logs.log").write_bytes(b"2026-01-01T00:00:00Z ERROR [a] x\n")
    (legacy / "incident.json").write_text(json.dumps({
        "id": "INC-OLD001", "title": "old", "severity": "SEV-3", "source": "upload", "created_at": "2026-01-01T00:00:00Z",
        "status": "completed", "files": {"logs": {"name": "a.log", "stored_as": "logs.log", "bytes": 33}},
        "result": {"summary": {"title": "legacy cause", "verdict": "confirmed"}}}))
    (legacy / "events.json").write_text(json.dumps([{"type": "plan", "seq": 0}]))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LEGACY_DIR", tmp_path / "incidents")
    s = storage.Store(tmp_path / "rootcause.db")
    assert s.migrated == 1 and (tmp_path / "incidents.migrated-to-sqlite").exists()
    inc = s.get("INC-OLD001")
    assert inc["result"]["summary"]["title"] == "legacy cause" and s.read_input("INC-OLD001", "logs")
    assert len(s.get_events("INC-OLD001")) == 1


def test_api_run_history_and_per_run_reports():
    iid = client.post("/api/incidents/from-sample/checkout-failures").json()["id"]
    wait_done(iid)
    client.post(f"/api/incidents/{iid}/analyze")
    wait_done(iid)
    runs = client.get(f"/api/incidents/{iid}/runs").json()
    assert [r["number"] for r in runs] == [2, 1] and all(r["status"] == "completed" and r["events"] > 10 for r in runs)
    first = client.get(f"/api/incidents/{iid}/runs/{runs[1]['id']}").json()
    assert first["result"]["summary"]["service"] == "payment-service" and first["events"][0]["type"] == "plan"
    rep = client.get(f"/api/incidents/{iid}/report?format=md&run={runs[1]['id']}")
    assert rep.status_code == 200 and "run #1" in rep.text and "run1.md" in rep.headers["content-disposition"]
    assert client.get(f"/api/incidents/{iid}/runs/999999").status_code == 404
    assert client.get("/api/incidents").json()[0]["run_count"] >= 1
    assert client.get("/api/health").json()["storage"]["engine"] == "sqlite"


def test_reports_download_for_every_verdict():
    weak = "timestamp,service,metric,value\n" + "".join(
        f"2026-01-01T00:{m:02d}:00Z,svc-a,cpu_pct,{30 + (m % 3) + (40 if m >= 40 else 0)}\n" for m in range(60))
    flat = "timestamp,service,metric,value\n" + "".join(f"2026-01-01T00:{m:02d}:00Z,svc,cpu_pct,{30 + (m % 3)}\n" for m in range(60))
    for csv, verdict in ((weak, "inconclusive"), (flat, "no_anomaly")):
        iid = client.post("/api/incidents", data={"title": verdict}, files={"metrics": ("m.csv", csv)}).json()["id"]
        assert wait_done(iid)["result"]["summary"]["verdict"] == verdict
        for fmt in ("html", "md", "json"):
            assert client.get(f"/api/incidents/{iid}/report?format={fmt}").status_code == 200


def test_reported_duration_excludes_display_pacing():
    r = RCAAgent(lambda e: None, pace=0.02, use_llm=False).run(load_sample("login-outage"), {})
    s = r["summary"]
    assert s["display_pacing_s"] > 0 and s["duration_s"] < s["wall_clock_s"]


def test_rejected_tool_call_is_flagged_as_error_to_anthropic(fake_llm):
    fake = fake_llm("anthropic", [
        {"calls": [("inspect_service", {"service": "no-such-service", "reason": "x"})]},
        {"calls": [("finish_investigation", finish_args())]},
    ])
    run_agent(load_sample("checkout-failures"), use_llm=True)
    block = fake.requests[1]["body"]["messages"][-1]["content"][0]
    assert block["type"] == "tool_result" and block.get("is_error") is True
